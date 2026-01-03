import os
import subprocess
import time
import json
import shutil

# --- Constants ---
DRIVE_NAME = "drive-scsi0.img"
MAIN_LOOP_DEV = "/dev/loop0"
MOUNT_POINT = "/mnt/pbsync_restore"
LOG_FILE_PATH = "/app/data/pbsync_stream.log"

def run_command(command, shell=False, check=True, env=None):
    """
    Runs a command and logs its output.
    """
    cmd_str = ' '.join(command) if isinstance(command, list) else command
    print(f"COMMAND: {cmd_str}")
    try:
        process_env = os.environ.copy()
        if env: process_env.update(env)

        result = subprocess.run(
            command, shell=shell, check=check, capture_output=True, text=True, env=process_env
        )
        # Log stdout only if useful (keep logs clean)
        if result.stdout and not cmd_str.startswith("lsblk"): 
            print(f"STDOUT: {result.stdout.strip()[:200]}...") # Truncate long logs
        if result.stderr: 
            print(f"STDERR: {result.stderr.strip()}")
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Command failed. Return Code: {e.returncode}")
        print(f"STDOUT: {e.stdout}")
        print(f"STDERR: {e.stderr}")
        raise

def cleanup():
    """
    Cleans up all mounts and maps.
    """
    print("--- Starting Cleanup ---")
    
    # 1. Unmount
    run_command(["umount", "-l", MOUNT_POINT], check=False)
    
    # 2. Clean up partition mappings (kpartx)
    run_command(["kpartx", "-d", MAIN_LOOP_DEV], check=False)
    run_command(["dmsetup", "remove_all"], check=False) # Force remove mappers

    # 3. Unmap the main drive
    run_command(["proxmox-backup-client", "unmap", MAIN_LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

def get_mount_target():
    """
    Uses kpartx to map partitions and lsblk (JSON) to find the largest partition.
    Returns the path to the device node to be mounted.
    """
    print(f"-> analyzing block device structure for {MAIN_LOOP_DEV}...")
    
    # 1. Force kernel to scan partitions using kpartx
    # -a: add, -v: verbose, -s: sync (wait)
    try:
        run_command(["kpartx", "-a", "-v", "-s", MAIN_LOOP_DEV])
    except:
        print("Warning: kpartx failed, trying to proceed...")

    time.sleep(2) # Wait for device nodes to settle

    # 2. Use lsblk with JSON output for reliable parsing
    # This avoids "text parsing" errors with column widths
    try:
        cmd = ["lsblk", "-b", "-J", "-o", "NAME,SIZE,TYPE,KNAME,PKNAME"]
        result = run_command(cmd)
        data = json.loads(result.stdout)
        
        candidates = []

        # Helper to find loop0 and its children
        def find_loop0_children(devices):
            found_parts = []
            for dev in devices:
                # If this is loop0, check its children
                if dev.get("name") == "loop0" or dev.get("kname") == "loop0":
                    if "children" in dev:
                        return dev["children"]
                # Recursive search
                if "children" in dev:
                    child_res = find_loop0_children(dev["children"])
                    if child_res: return child_res
            return []

        children = find_loop0_children(data.get("blockdevices", []))
        
        # If logical partitions found via lsblk structure
        for child in children:
            # We construct full path. Usually /dev/mapper/loop0pX or /dev/loop0pX
            dev_name = child.get("name")
            size = int(child.get("size", 0))
            
            # Try to resolve actual path
            possible_paths = [
                f"/dev/mapper/{dev_name}",
                f"/dev/{dev_name}"
            ]
            
            final_path = None
            for p in possible_paths:
                if os.path.exists(p):
                    final_path = p
                    break
            
            if final_path:
                candidates.append({"path": final_path, "size": size})

        # Fallback: Check /dev/mapper manually if lsblk didn't link them
        if not candidates:
            if os.path.exists("/dev/mapper/loop0p1"):
                # Basic guessing for single partition
                return "/dev/mapper/loop0p1"
            elif os.path.exists("/dev/mapper/loop0p2"):
                return "/dev/mapper/loop0p2"

        if candidates:
            # Return the largest partition
            largest = sorted(candidates, key=lambda x: x["size"], reverse=True)[0]
            print(f"-> Largest partition found: {largest['path']} ({largest['size']} bytes)")
            return largest["path"]
        else:
            print("-> No partitions detected. Using raw device.")
            return MAIN_LOOP_DEV

    except Exception as e:
        print(f"WARNING: Partition detection failed ({e}). Defaulting to {MAIN_LOOP_DEV}")
        return MAIN_LOOP_DEV

def mount_filesystem(device_path):
    """
    Tries multiple mount methods (Auto, NTFS, XFS).
    """
    print(f"-> Attempting to mount: {device_path}")
    
    # Ensure directory exists
    if not os.path.exists(MOUNT_POINT):
        os.makedirs(MOUNT_POINT)

    errors = []

    # Strategy 1: Standard Linux Mount (ext4, xfs, etc.)
    try:
        # ro: read-only, norecovery: don't replay journals (safe for backup)
        run_command(["mount", "-o", "ro,norecovery", device_path, MOUNT_POINT])
        return
    except Exception as e: errors.append(str(e))

    # Strategy 2: NTFS-3G (Windows)
    try:
        # remove_hiberfile: clears hibernation flag if needed to read
        run_command(["ntfs-3g", "-o", "ro,remove_hiberfile", device_path, MOUNT_POINT])
        return
    except Exception as e: errors.append(str(e))

    # Strategy 3: XFS Explicit
    try:
        run_command(["mount", "-t", "xfs", "-o", "ro,norecovery", device_path, MOUNT_POINT])
        return
    except Exception as e: errors.append(str(e))

    # Strategy 4: Fallback simple mount
    try:
        run_command(["mount", "-o", "ro", device_path, MOUNT_POINT])
        return
    except Exception as e: errors.append(str(e))

    print("ALL MOUNT ATTEMPTS FAILED.")
    print(f"Errors: {errors}")
    raise Exception(f"Could not mount {device_path}. Check if filesystem is supported.")

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

        # 2. Find Partition & Mount
        target_dev = get_mount_target()
        mount_filesystem(target_dev)

        # 3. List
        safe_path = os.path.normpath(os.path.join(MOUNT_POINT, path.strip('/')))
        if not safe_path.startswith(MOUNT_POINT): safe_path = MOUNT_POINT

        if not os.path.exists(safe_path):
            return {"status": "error", "message": "Path not found"}

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

        # 2. Find Partition & Mount
        target_dev = get_mount_target()
        mount_filesystem(target_dev)
        print("-> Mount successful.")

        # 3. Stream Setup
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