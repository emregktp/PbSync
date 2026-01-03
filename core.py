import os
import subprocess
import time
import json
import shutil

# --- Constants ---
DRIVE_NAME = "drive-scsi0.img"
LOOP_DEV = "/dev/loop0"
MOUNT_POINT = "/mnt/pbsync_restore"
LOG_FILE_PATH = "/app/data/pbsync_stream.log"

def run_command(command, shell=False, check=True, env=None):
    """
    Runs a command and logs its output.
    """
    print(f"COMMAND: {' '.join(command) if isinstance(command, list) else command}")
    try:
        process_env = os.environ.copy()
        if env: process_env.update(env)

        result = subprocess.run(
            command, shell=shell, check=check, capture_output=True, text=True, env=process_env
        )
        if result.stdout: print(f"STDOUT: {result.stdout.strip()}")
        if result.stderr: print(f"STDERR: {result.stderr.strip()}")
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Command failed. Return Code: {e.returncode}")
        print(f"STDOUT: {e.stdout}")
        print(f"STDERR: {e.stderr}")
        raise

def get_largest_partition(device_path):
    """
    Finds the largest partition within a given block device.
    """
    print(f"-> Searching for the largest partition in '{device_path}'...")
    try:
        cmd = ["lsblk", "-nr", "-o", "NAME,SIZE", "-b", device_path]
        result = run_command(cmd)
        partitions = []
        for line in result.stdout.strip().split('\n'):
            parts = line.split()
            # lsblk output usually looks like 'loop0p1'
            if len(parts) == 2 and parts[0].startswith(os.path.basename(device_path) + 'p'):
                name, size_str = parts
                partitions.append((f"/dev/{name}", int(size_str)))

        if not partitions:
            print("-> No partitions found, using the main device.")
            return device_path
            
        largest_part = sorted(partitions, key=lambda x: x[1], reverse=True)[0]
        print(f"-> Largest partition found: {largest_part[0]} (Size: {largest_part[1]} bytes)")
        return largest_part[0]

    except Exception as e:
        print(f"WARNING: Partition check failed: {e}. Defaulting to raw device.")
        return device_path

def cleanup():
    """
    Cleans up all mounts and maps.
    """
    print("--- Starting Cleanup ---")
    run_command(["umount", "-l", MOUNT_POINT], check=False)
    run_command(["proxmox-backup-client", "unmap", LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

def list_files_in_snapshot(config: dict, snapshot: str, path: str = ""):
    """
    Mounts the snapshot, lists directories in the specified path, and unmounts.
    Returns a dict with items.
    """
    print(f"\n--- Exploring Snapshot: {snapshot} Path: {path} ---")
    current_env = os.environ.copy()
    
    # Ensure clean state
    cleanup() 
    run_command(["mkdir", "-p", MOUNT_POINT])

    try:
        # 1. Map
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(1) # Wait for device mapping

        # 2. Mount
        target_partition = get_largest_partition(LOOP_DEV)
        try:
            # Try read-only with norecovery (faster/safer for backups)
            run_command(["mount", "-o", "ro,norecovery", target_partition, MOUNT_POINT])
        except:
            # Fallback
            run_command(["mount", "-o", "ro", target_partition, MOUNT_POINT])

        # 3. List Files
        # Prevent directory traversal
        safe_path = os.path.normpath(os.path.join(MOUNT_POINT, path.strip('/')))
        if not safe_path.startswith(MOUNT_POINT):
            safe_path = MOUNT_POINT

        if not os.path.exists(safe_path):
            return {"status": "error", "message": "Path not found inside backup"}

        items = []
        with os.scandir(safe_path) as entries:
            for entry in entries:
                # Get relative path for the API
                rel_path = os.path.relpath(entry.path, MOUNT_POINT)
                
                items.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "path": rel_path
                })
        
        # Sort: Directories first, then files (alphabetical)
        items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
        
        return {"status": "success", "items": items, "current_path": path}

    except Exception as e:
        print(f"Explore Error: {e}")
        return {"status": "error", "message": str(e)}
    
    finally:
        # Always cleanup immediately after listing to free resources
        cleanup()

def run_backup_process(config: dict, snapshot: str, remote: str, target_folder: str = "", source_paths: str = ""):
    """
    Main backup logic with Folder & Path selection support.
    """
    print(f"\n{'='*60}\nSTARTING NEW STREAM PROCESS\n{'='*60}")
    print(f"Time: {time.ctime()}")
    print(f"Source Snapshot: {snapshot}")
    print(f"Target Remote: {remote}")
    print(f"Target Folder: {target_folder if target_folder else '(Root)'}")
    print(f"Source Paths: {source_paths if source_paths else '(Full Disk)'}\n")

    current_env = os.environ.copy()
    base_dir = current_env.get("PWD", "/")

    try:
        # 1. Prepare
        run_command(["mkdir", "-p", MOUNT_POINT])
        cleanup() # Ensure clean state

        # 2. Map Snapshot
        print(f"\n-> [Step 1/4] Mapping snapshot...")
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(2) # Wait for device to settle

        # 3. Mount Filesystem
        print(f"\n-> [Step 2/4] Mounting filesystem...")
        target_partition = get_largest_partition(LOOP_DEV)
        
        try:
            run_command(["mount", "-o", "ro,norecovery,noload", target_partition, MOUNT_POINT])
        except Exception:
            print("-> Standard mount failed, trying fallback options...")
            run_command(["mount", "-o", "ro", target_partition, MOUNT_POINT])

        # 4. Stream & Upload
        print(f"\n-> [Step 3/4] Preparing stream pipeline...")
        
        # File Name: vmid_Date.tar.gz
        vmid = snapshot.split('/')[1]
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        archive_name = f"{vmid}_{timestamp}.tar.gz"
        
        # Construct Remote Path
        if target_folder and target_folder.strip():
            clean_folder = target_folder.strip().strip('/')
            full_remote_path = f"{remote}:{clean_folder}/{archive_name}"
        else:
            full_remote_path = f"{remote}:{archive_name}"

        # Enter Mount Point
        os.chdir(MOUNT_POINT)
        print(f"   Working directory: {os.getcwd()}")

        # Process Source Paths
        if source_paths and source_paths.strip():
            # Convert comma-separated string to list
            dirs_to_backup = [p.strip() for p in source_paths.split(',') if p.strip()]
        else:
            # Backup everything
            dirs_to_backup = ["."]

        print(f"   Backup Source Paths: {dirs_to_backup}")
        print(f"   Upload Target: {full_remote_path}")

        # Pipeline: tar -> pigz -> rclone
        tar_cmd = ["tar", "cf", "-"] + dirs_to_backup
        pigz_cmd = ["pigz", "-1"]
        rclone_cmd = ["rclone", "rcat", full_remote_path, "-P", "--buffer-size", "128M"]

        print("\n-> [Step 4/4] Streaming data...")
        with open(LOG_FILE_PATH, 'a') as log_file:
            # P1: Tar
            p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            # P2: Pigz (Reads P1)
            p2 = subprocess.Popen(pigz_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p1.stdout.close() 
            # P3: Rclone (Reads P2)
            p3 = subprocess.Popen(rclone_cmd, stdin=p2.stdout, stderr=subprocess.PIPE, text=True, env=current_env)
            p2.stdout.close()

            # Read Rclone Progress
            for line in iter(p3.stderr.readline, ''):
                print(f"   [Cloud] {line.strip()}")
            
            p3.wait()

        if p1.wait() != 0: raise Exception("TAR failed. Check if paths exist.")
        if p2.wait() != 0: raise Exception("PIGZ compression failed.")
        if p3.returncode != 0: raise Exception("RCLONE upload failed.")

        print(f"\n-> SUCCESS! Backup successfully uploaded to {full_remote_path}")

    except Exception as e:
        print(f"\n{'!'*20} CRITICAL ERROR {'!'*20}")
        print(f"{e}")
        print(f"{'!'*60}")
    
    finally:
        if os.getcwd() != base_dir:
            os.chdir(base_dir)
        cleanup()
        print(f"\n{'='*60}\nPROCESS FINISHED\n{'='*60}\n")