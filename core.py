import os
import subprocess
import time

# --- Constants ---
DRIVE_NAME = "drive-scsi0.img"
LOOP_DEV = "/dev/loop0"
MOUNT_POINT = "/mnt/pbsync_restore"
LOG_FILE_PATH = "/app/data/pbsync_stream.log"

def run_command(command, shell=False, check=True, env=None):
    """
    Runs a command and logs its output. Raises an exception on failure.
    Accepts an 'env' dictionary for the execution environment.
    """
    print(f"COMMAND: {' '.join(command) if isinstance(command, list) else command}")
    try:
        # Ensure the provided environment is used
        process_env = os.environ.copy()
        if env:
            process_env.update(env)

        result = subprocess.run(
            command,
            shell=shell,
            check=check,
            capture_output=True,
            text=True,
            env=process_env
        )
        if result.stdout:
            print(f"STDOUT: {result.stdout.strip()}")
        if result.stderr:
            print(f"STDERR: {result.stderr.strip()}")
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
        print(f"WARNING: Error finding largest partition: {e}. Defaulting to main device: {device_path}")
        return device_path

def cleanup():
    """
    Cleans up all mounts and maps, ignoring errors.
    """
    print("--- Starting Cleanup ---")
    run_command(["umount", "-l", MOUNT_POINT], check=False)
    run_command(["proxmox-backup-client", "unmap", LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

def run_backup_process(config: dict, snapshot: str, remote: str):
    """
    The main synchronous function that executes the backup process.
    Designed to be run in a FastAPI background task.
    Requires root privileges for many of its commands.
    
    Args:
        config (dict): A dictionary containing configuration like 'pbs_repository_path'.
        snapshot (str): The full snapshot identifier to back up.
        remote (str): The name of the rclone remote target.
    """
    print(f"\n{'='*60}\nSTARTING NEW STREAM PROCESS\n{'='*60}")
    print(f"Time: {time.ctime()}")
    print(f"Source Snapshot: {snapshot}")
    print(f"Target Cloud: {remote}\n")

    # The environment is already set by main.py (PBS_PASSWORD, RCLONE_CONFIG)
    # We just need to use it for our commands.
    current_env = os.environ.copy()
    base_dir = current_env.get("PWD", "/") # Get current working dir from env

    try:
        # 1. Ensure mount point exists
        print("-> [Step 1/5] Preparing mount point...")
        run_command(["mkdir", "-p", MOUNT_POINT])

        # 2. Map the snapshot
        print(f"\n-> [Step 2/5] Mapping snapshot: '{snapshot}'...")
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(2)

        # 3. Find and mount the largest partition
        print(f"\n-> [Step 3/5] Mounting device...")
        target_partition = get_largest_partition(LOOP_DEV)
        
        mount_opts = ["-o", "ro,norecovery,noload"]
        try:
            run_command(["mount", *mount_opts, target_partition, MOUNT_POINT])
        except Exception:
            print("-> Mount failed with recovery options, trying without...")
            run_command(["mount", "-o", "ro", target_partition, MOUNT_POINT])

        # 4. Start the Stream & Upload process
        print(f"\n-> [Step 4/5] Starting compression and upload...")
        vmid = snapshot.split('/')[1]
        archive_name = f"{vmid}_{{time.strftime('%Y%m%d-%H%M%S')}}.tar.gz"
        
        print(f"   (Detailed rclone logs will be in: {LOG_FILE_PATH})")
        os.chdir(MOUNT_POINT)
        print(f"   Working directory changed to: {os.getcwd()}")

        # Pipeline: tar -> pigz -> rclone
        tar_cmd = ["tar", "cf", "-"]
        backup_dirs = ["var", "etc", "home", "root", "opt"]
        dirs_to_backup = [d for d in backup_dirs if os.path.exists(d)]
        tar_cmd.extend(dirs_to_backup if dirs_to_backup else ["."])

        pigz_cmd = ["pigz", "-1"]
        rclone_cmd = ["rclone", "rcat", f"{remote}:{archive_name}", "-P", "--buffer-size", "128M"]

        with open(LOG_FILE_PATH, 'a') as log_file:
            p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p2 = subprocess.Popen(pigz_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p1.stdout.close()
            p3 = subprocess.Popen(rclone_cmd, stdin=p2.stdout, stderr=subprocess.PIPE, text=True, env=current_env)
            p2.stdout.close()

            for line in iter(p3.stderr.readline, ''):
                print(f"   {line.strip()}")
            
            p3.wait()

        if p1.wait() != 0: raise Exception("'tar' command failed.")
        if p2.wait() != 0: raise Exception("'pigz' command failed.")
        if p3.returncode != 0: raise Exception("'rclone' command failed.")

        print(f"\n-> [Step 5/5] Process completed successfully!")
        print(f"   Archive Name: {archive_name}")

    except Exception as e:
        print(f"\n{'!'*20} AN ERROR OCCURRED {'!'*20}\n{e}\n{'!'*60}")
    
    finally:
        os.chdir(base_dir)
        cleanup()
        print(f"\n{'='*60}\nPROCESS FINISHED\n{'='*60}\n")
