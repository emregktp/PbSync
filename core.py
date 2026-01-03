import os
import subprocess
import time
import shutil
import glob

# --- Constants ---
DRIVE_NAME = "drive-scsi0.img"
MAIN_LOOP_DEV = "/dev/loop0"
MOUNT_POINT = "/mnt/pbsync_restore"
LOG_FILE_PATH = "/app/data/pbsync_stream.log"

def run_command(command, shell=False, check=True, env=None):
    cmd_str = ' '.join(command) if isinstance(command, list) else command
    print(f"COMMAND: {cmd_str}")
    try:
        process_env = os.environ.copy()
        if env: process_env.update(env)

        result = subprocess.run(
            command, shell=shell, check=check, capture_output=True, text=True, env=process_env
        )
        if result.stdout and not "lsblk" in cmd_str: 
            print(f"STDOUT: {result.stdout.strip()[:200]}...")
        if result.stderr: 
            print(f"STDERR: {result.stderr.strip()}")
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Command failed. Return Code: {e.returncode}")
        print(f"STDOUT: {e.stdout}")
        print(f"STDERR: {e.stderr}")
        raise

def cleanup():
    print("--- Starting Cleanup ---")
    run_command(["umount", "-l", MOUNT_POINT], check=False)
    run_command(["vgchange", "-an"], check=False)
    run_command(["kpartx", "-d", MAIN_LOOP_DEV], check=False)
    run_command(["dmsetup", "remove_all"], check=False)
    run_command(["proxmox-backup-client", "unmap", MAIN_LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

def get_device_size(device_path):
    """
    Returns the size of a block device in bytes using blockdev.
    """
    try:
        res = run_command(["blockdev", "--getsize64", device_path])
        return int(res.stdout.strip())
    except:
        return 0

def find_all_candidates():
    """
    Finds ALL potential mount candidates by scanning /dev/mapper and /dev/loop*
    Sorts them by size (Largest first).
    This bypasses lsblk tree complexity and finds LVMs directly.
    """
    candidates = set()
    
    # 1. Always include the raw device (for raw filesystems)
    candidates.add(MAIN_LOOP_DEV)

    # 2. Find all mapper devices (LVM Volumes + kpartx partitions)
    # They usually live in /dev/mapper/
    mappers = glob.glob("/dev/mapper/*")
    for dev in mappers:
        if "control" not in dev: # Skip control device
            candidates.add(dev)

    # 3. Find standard partitions if created (loop0p1 etc)
    partitions = glob.glob("/dev/loop0p*")
    for dev in partitions:
        candidates.add(dev)

    # Convert to list of tuples (path, size)
    candidate_list = []
    for dev in candidates:
        size = get_device_size(dev)
        if size > 0:
            candidate_list.append((dev, size))
    
    # Sort: Largest first
    candidate_list.sort(key=lambda x: x[1], reverse=True)
    
    # Return just paths
    sorted_paths = [x[0] for x in candidate_list]
    print(f"-> Mount candidates (sorted by size): {sorted_paths}")
    return sorted_paths

def try_mount(device_path):
    """
    Attempts to mount a specific device using multiple filesystem types.
    """
    print(f"-> Trying to mount: {device_path}")
    if not os.path.exists(MOUNT_POINT): os.makedirs(MOUNT_POINT)

    # Strategies ordered by likelihood
    strategies = [
        ["mount", "-o", "ro", device_path, MOUNT_POINT], # Auto-detect
        ["mount", "-t", "xfs", "-o", "ro,norecovery", device_path, MOUNT_POINT], # XFS (Linux)
        ["ntfs-3g", "-o", "ro,remove_hiberfile", device_path, MOUNT_POINT], # NTFS (Windows)
        ["mount", "-t", "ext4", "-o", "ro", device_path, MOUNT_POINT] # EXT4
    ]

    for cmd in strategies:
        try:
            run_command(cmd)
            print(f"   SUCCESS! Mounted {device_path} using: {' '.join(cmd)}")
            return True
        except:
            continue
            
    return False

def list_files_in_snapshot(config: dict, snapshot: str, path: str = ""):
    print(f"\n--- Exploring Snapshot: {snapshot} Path: {path} ---")
    current_env = os.environ.copy()
    cleanup() 
    run_command(["mkdir", "-p", MOUNT_POINT])

    try:
        # 1. Map
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(2)

        # 2. Activate Everything (Partition Table + LVM)
        try: run_command(["kpartx", "-a", "-v", "-s", MAIN_LOOP_DEV])
        except: pass
        
        try: 
            # Critical for LVM detection
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        
        time.sleep(1)

        # 3. Find & Mount (Brute Force Strategy)
        candidates = find_all_candidates()
        mounted = False
        
        for dev in candidates:
            if try_mount(dev):
                mounted = True
                break
        
        if not mounted:
            return {"status": "error", "message": "Could not mount any partition/volume in backup."}

        # 4. List Files
        safe_path = os.path.normpath(os.path.join(MOUNT_POINT, path.strip('/')))
        if not safe_path.startswith(MOUNT_POINT): safe_path = MOUNT_POINT

        if not os.path.exists(safe_path):
            return {"status": "error", "message": "Path not found inside backup"}

        items = []
        with os.scandir(safe_path) as entries:
            for entry in entries:
                items.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "path": os.path.relpath(entry.path, MOUNT_POINT)
                })
        
        items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        return {"status": "success", "items": items, "current_path": path}

    except Exception as e:
        print(f"Explore Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        cleanup()

def run_backup_process(config: dict, snapshot: str, remote: str, target_folder: str = "", source_paths: str = ""):
    print(f"\n{'='*60}\nSTARTING NEW STREAM PROCESS\n{'='*60}")
    print(f"Source: {snapshot}")
    
    current_env = os.environ.copy()
    base_dir = current_env.get("PWD", "/")

    try:
        run_command(["mkdir", "-p", MOUNT_POINT])
        cleanup()

        # 1. Map
        print(f"\n-> Mapping snapshot...")
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(2)

        # 2. Activate
        print("-> Activating partitions/LVM...")
        try: run_command(["kpartx", "-a", "-v", "-s", MAIN_LOOP_DEV])
        except: pass
        try: 
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        time.sleep(1)

        # 3. Mount (Brute Force)
        candidates = find_all_candidates()
        mounted = False
        for dev in candidates:
            if try_mount(dev):
                mounted = True
                break
        
        if not mounted:
            raise Exception("Failed to mount any filesystem from the snapshot.")

        # 4. Stream
        vmid = snapshot.split('/')[1]
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        archive_name = f"{vmid}_{timestamp}.tar.gz"
        
        full_remote_path = f"{remote}:{archive_name}"
        if target_folder and target_folder.strip():
            full_remote_path = f"{remote}:{target_folder.strip().strip('/')}/{archive_name}"

        os.chdir(MOUNT_POINT)
        
        dirs_to_backup = ["."]
        if source_paths and source_paths.strip():
            dirs_to_backup = [p.strip() for p in source_paths.split(',') if p.strip()]

        tar_cmd = ["tar", "cf", "-"] + dirs_to_backup
        pigz_cmd = ["pigz", "-1"]
        rclone_cmd = ["rclone", "rcat", full_remote_path, "-P", "--buffer-size", "128M"]

        print("\n-> Streaming data...")
        with open(LOG_FILE_PATH, 'a') as log_file:
            p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p2 = subprocess.Popen(pigz_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p1.stdout.close() 
            p3 = subprocess.Popen(rclone_cmd, stdin=p2.stdout, stderr=subprocess.PIPE, text=True, env=current_env)
            p2.stdout.close()

            for line in iter(p3.stderr.readline, ''):
                print(f"   [Cloud] {line.strip()}")
            
            p3.wait()

        if p3.returncode != 0: raise Exception("Upload failed.")
        print(f"\n-> SUCCESS! Backup uploaded to {full_remote_path}")

    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
    finally:
        if os.getcwd() != base_dir: os.chdir(base_dir)
        cleanup()