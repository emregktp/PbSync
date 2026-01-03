import os
import subprocess
import time
import shutil
import glob
import json

# --- Constants ---
DRIVE_NAME = "drive-scsi0.img"
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
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Command failed. Return Code: {e.returncode}")
        print(f"STDERR: {e.stderr.strip()}")
        raise e

def find_active_loop_device():
    """
    Finds the loop device associated with our backup mapping.
    """
    try:
        # List all loop devices and look for our image name
        res = run_command(["losetup", "-a", "-J"], check=False)
        data = json.loads(res.stdout)
        
        for dev in data.get('loopdevices', []):
            if DRIVE_NAME in dev.get('back-file', ''):
                return dev['name']
        
        # Fallback: look for the most recently created loop device
        loop_devs = glob.glob("/dev/loop*")
        # Filter only numeric ones (loop0, loop1...)
        loop_devs = [d for d in loop_devs if d[9:].isdigit()]
        if loop_devs:
            loop_devs.sort(key=lambda x: int(x.replace("/dev/loop", "")))
            return loop_devs[-1]
            
    except Exception as e:
        print(f"Loop finding error: {e}")
    return None

def cleanup_loop(loop_dev=None):
    print("--- Starting Cleanup ---")
    try: run_command(["umount", "-l", MOUNT_POINT], check=False)
    except: pass
    
    if loop_dev:
        try: run_command(["kpartx", "-d", loop_dev], check=False)
        except: pass
        
    try: run_command(["vgchange", "-an"], check=False)
    except: pass
    try: run_command(["dmsetup", "remove_all"], check=False)
    except: pass
    
    # Unmap via client
    try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
    except: pass
    print("--- Cleanup Finished ---")

def get_mount_candidates(loop_dev):
    print(f"-> Analyzing partitions on {loop_dev}...")
    candidates = []

    # 1. Try lsblk JSON first
    try:
        cmd = ["lsblk", "-b", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,PKNAME", loop_dev]
        res = run_command(cmd)
        data = json.loads(res.stdout)
        
        def walk(devices):
            found = []
            for d in devices:
                # Construct path
                path = f"/dev/{d['name']}"
                if os.path.exists(f"/dev/mapper/{d['name']}"):
                    path = f"/dev/mapper/{d['name']}"
                
                # We want partitions, LVMs, or the raw disk itself if it has a filesystem
                if d.get('fstype') or d.get('type') in ['part', 'lvm', 'loop']:
                    found.append(path)
                
                if 'children' in d:
                    found.extend(walk(d['children']))
            return found

        candidates = walk(data.get('blockdevices', []))
    except: pass

    # 2. Fallback to glob if lsblk failed
    if not candidates:
        base = os.path.basename(loop_dev)
        candidates.extend(glob.glob(f"/dev/mapper/{base}p*"))
        candidates.append(loop_dev)

    # Dedup and prioritize
    candidates = list(dict.fromkeys(candidates))
    if loop_dev in candidates and len(candidates) > 1:
        candidates.remove(loop_dev)
        candidates.append(loop_dev) # Try raw device last

    print(f"-> Candidates: {candidates}")
    return candidates

def try_mount_device(device):
    print(f"-> Attempting to mount: {device}")
    if not os.path.exists(MOUNT_POINT): os.makedirs(MOUNT_POINT)
    
    # Try different filesystems
    cmds = [
        ["mount", "-o", "ro", device, MOUNT_POINT],
        ["mount", "-t", "xfs", "-o", "ro,norecovery", device, MOUNT_POINT],
        ["ntfs-3g", "-o", "ro,remove_hiberfile", device, MOUNT_POINT],
        ["mount", "-t", "ext4", "-o", "ro", device, MOUNT_POINT]
    ]
    
    for cmd in cmds:
        try:
            run_command(cmd)
            print(f"   SUCCESS! Mounted with: {cmd}")
            return True
        except: continue
    return False

def list_files_in_snapshot(config: dict, snapshot: str, path: str = ""):
    print(f"\n--- Exploring: {snapshot} ---")
    current_env = os.environ.copy()
    
    # Force cleanup before starting
    try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
    except: pass
    
    run_command(["mkdir", "-p", MOUNT_POINT])

    active_loop = None

    try:
        # 1. Map
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        
        try:
            run_command(map_cmd, env=current_env)
        except subprocess.CalledProcessError as e:
            return {"status": "error", "message": f"Map Failed (OS Error 95?). Check Docker AppArmor settings. Log: {e.stderr}"}

        # Give it a moment to settle
        time.sleep(2)

        # 2. Identify Loop Device
        active_loop = find_active_loop_device()
        if not active_loop:
            return {"status": "error", "message": "Map command finished but no loop device found."}
        
        print(f"-> Mapped to: {active_loop}")

        # 3. Scan Partitions
        try: run_command(["kpartx", "-a", "-v", "-s", active_loop])
        except: pass
        try: 
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        time.sleep(2)

        # 4. Mount
        candidates = get_mount_candidates(active_loop)
        mounted = False
        for dev in candidates:
            if try_mount_device(dev):
                mounted = True
                break
        
        if not mounted:
            return {"status": "error", "message": "Could not mount any partition. Unsupported filesystem or encrypted disk."}

        # 5. List Files
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
        print(f"Global Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        cleanup_loop(active_loop)

def run_backup_process(config: dict, snapshot: str, remote: str, target_folder: str = "", source_paths: str = ""):
    print(f"\n--- Starting Stream: {snapshot} ---")
    current_env = os.environ.copy()
    active_loop = None

    try:
        # Cleanup
        try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
        except: pass
        run_command(["mkdir", "-p", MOUNT_POINT])

        # 1. Map
        print("-> Mapping...")
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(2)

        # 2. Identify
        active_loop = find_active_loop_device()
        if not active_loop: raise Exception("Loop device not found after mapping.")
        print(f"-> Active Loop: {active_loop}")

        # 3. Activate
        print("-> Activating partitions...")
        try: run_command(["kpartx", "-a", "-v", "-s", active_loop])
        except: pass
        try: 
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        time.sleep(2)

        # 4. Mount
        candidates = get_mount_candidates(active_loop)
        mounted = False
        for dev in candidates:
            if try_mount_device(dev):
                mounted = True
                break
        
        if not mounted: raise Exception("Mount failed for all candidates.")

        # 5. Stream
        vmid = snapshot.split('/')[1]
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        archive_name = f"{vmid}_{timestamp}.tar.gz"
        
        full_remote_path = f"{remote}:{archive_name}"
        if target_folder.strip():
            full_remote_path = f"{remote}:{target_folder.strip().strip('/')}/{archive_name}"

        os.chdir(MOUNT_POINT)
        
        dirs = ["."]
        if source_paths.strip():
            dirs = [p.strip() for p in source_paths.split(',') if p.strip()]

        tar_cmd = ["tar", "cf", "-"] + dirs
        pigz_cmd = ["pigz", "-1"]
        rclone_cmd = ["rclone", "rcat", full_remote_path, "-P", "--buffer-size", "128M"]

        print("-> Streaming...")
        with open(LOG_FILE_PATH, 'a') as log_file:
            p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p2 = subprocess.Popen(pigz_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p1.stdout.close()
            p3 = subprocess.Popen(rclone_cmd, stdin=p2.stdout, stderr=subprocess.PIPE, text=True, env=current_env)
            p2.stdout.close()
            
            # Read rclone progress
            for line in iter(p3.stderr.readline, ''):
                print(f"   [Cloud] {line.strip()}")
            
            p3.wait()

        if p3.returncode != 0: raise Exception("Rclone upload failed.")
        print("-> SUCCESS: Stream complete.")

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
    finally:
        cleanup_loop(active_loop)