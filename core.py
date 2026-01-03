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
    print(f"-> Searching for the largest partition in '{device_path}'...")
    try:
        # 1. Partition tablosunu okumaya zorla (kpartx)
        run_command(["kpartx", "-a", "-v", device_path], check=False)
        run_command(["partprobe", device_path], check=False)
        time.sleep(2) # Cihazların oluşması için bekle

        # 2. Partitionları listele
        cmd = ["lsblk", "-nr", "-o", "NAME,SIZE,TYPE", "-b", device_path]
        result = run_command(cmd)
        partitions = []
        
        base_name = os.path.basename(device_path) # loop0
        
        for line in result.stdout.strip().split('\n'):
            parts = line.split()
            if len(parts) < 2: continue
            
            name = parts[0]
            size_str = parts[1]
            p_type = parts[2] if len(parts) > 2 else "part"

            # Sadece partition olanları al (disk'in kendisini atla)
            # lsblk çıktısında partitionlar loop0p1 veya loop0-part1 gibi görünür
            # Veya /dev/mapper altında oluşmuş olabilirler
            
            full_path = ""
            if name == base_name: continue # Ana cihazı atla

            # Device Path Kontrolü
            if os.path.exists(f"/dev/mapper/{name}"):
                full_path = f"/dev/mapper/{name}"
            elif os.path.exists(f"/dev/{name}"):
                full_path = f"/dev/{name}"
            
            if full_path:
                partitions.append((full_path, int(size_str)))

        if not partitions:
            print("-> No partitions found via lsblk. Trying direct mapper check...")
            # Fallback: /dev/mapper kontrolü
            if os.path.exists(f"/dev/mapper/{base_name}p1"):
                return f"/dev/mapper/{base_name}p1"
            if os.path.exists(f"/dev/mapper/{base_name}p2"): # Genelde p2 daha büyüktür (rootfs)
                return f"/dev/mapper/{base_name}p2"
            
            print("-> Still no partitions. Assuming raw filesystem on main device.")
            return device_path
            
        # En büyük partition'ı seç
        largest_part = sorted(partitions, key=lambda x: x[1], reverse=True)[0]
        print(f"-> Largest partition found: {largest_part[0]} (Size: {largest_part[1]} bytes)")
        return largest_part[0]

    except Exception as e:
        print(f"WARNING: Partition check failed: {e}. Defaulting to raw device.")
        return device_path

def cleanup():
    print("--- Starting Cleanup ---")
    # Önce mount'u kaldır
    run_command(["umount", "-l", MOUNT_POINT], check=False)
    time.sleep(1)
    # Mapper bağlantılarını temizle
    run_command(["kpartx", "-d", LOOP_DEV], check=False)
    run_command(["dmsetup", "remove_all"], check=False)
    # Loop cihazını serbest bırak
    run_command(["proxmox-backup-client", "unmap", LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

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
        time.sleep(2) # Map işleminin tamamlanmasını bekle

        # 2. Mount
        target_partition = get_largest_partition(LOOP_DEV)
        print(f"-> Attempting to mount: {target_partition}")
        
        try:
            # Standart mount
            run_command(["mount", "-o", "ro", target_partition, MOUNT_POINT])
        except Exception as e:
            print(f"Standard mount failed ({e}). Trying with explicit types...")
            # XFS veya NTFS ise
            try:
                run_command(["mount", "-t", "xfs", "-o", "ro,norecovery", target_partition, MOUNT_POINT])
            except:
                try:
                    run_command(["ntfs-3g", "-o", "ro", target_partition, MOUNT_POINT])
                except Exception as final_e:
                    raise Exception(f"Failed to mount {target_partition}. Error: {final_e}")

        # 3. List Files
        safe_path = os.path.normpath(os.path.join(MOUNT_POINT, path.strip('/')))
        if not safe_path.startswith(MOUNT_POINT):
            safe_path = MOUNT_POINT

        if not os.path.exists(safe_path):
            return {"status": "error", "message": f"Path '{path}' not found inside backup."}

        items = []
        with os.scandir(safe_path) as entries:
            for entry in entries:
                rel_path = os.path.relpath(entry.path, MOUNT_POINT)
                items.append({
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "path": rel_path
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
    print(f"Target: {remote} (Folder: {target_folder})")

    current_env = os.environ.copy()
    base_dir = current_env.get("PWD", "/")

    try:
        run_command(["mkdir", "-p", MOUNT_POINT])
        cleanup()

        print(f"\n-> Mapping snapshot...")
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(2)

        print(f"\n-> Mounting filesystem...")
        target_partition = get_largest_partition(LOOP_DEV)
        
        # Mount Logic (Retry Mechanism)
        mounted = False
        for opt in [["-o", "ro"], ["-t", "xfs", "-o", "ro,norecovery"], ["-t", "ext4", "-o", "ro"]]:
            try:
                run_command(["mount"] + opt + [target_partition, MOUNT_POINT])
                mounted = True
                break
            except:
                continue
        
        if not mounted:
            # Last resort: NTFS
            try:
                run_command(["ntfs-3g", "-o", "ro", target_partition, MOUNT_POINT])
                mounted = True
            except:
                raise Exception(f"Could not mount {target_partition} with any known filesystem.")

        print(f"\n-> Streaming data...")
        
        vmid = snapshot.split('/')[1]
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        archive_name = f"{vmid}_{timestamp}.tar.gz"
        
        if target_folder and target_folder.strip():
            clean_folder = target_folder.strip().strip('/')
            full_remote_path = f"{remote}:{clean_folder}/{archive_name}"
        else:
            full_remote_path = f"{remote}:{archive_name}"

        os.chdir(MOUNT_POINT)

        if source_paths and source_paths.strip():
            dirs_to_backup = [p.strip() for p in source_paths.split(',') if p.strip()]
        else:
            dirs_to_backup = ["."]

        tar_cmd = ["tar", "cf", "-"] + dirs_to_backup
        pigz_cmd = ["pigz", "-1"]
        rclone_cmd = ["rclone", "rcat", full_remote_path, "-P", "--buffer-size", "128M"]

        with open(LOG_FILE_PATH, 'a') as log_file:
            p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p2 = subprocess.Popen(pigz_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=log_file, env=current_env)
            p1.stdout.close() 
            p3 = subprocess.Popen(rclone_cmd, stdin=p2.stdout, stderr=subprocess.PIPE, text=True, env=current_env)
            p2.stdout.close()

            for line in iter(p3.stderr.readline, ''):
                print(f"   [Cloud] {line.strip()}")
            
            p3.wait()

        if p1.wait() != 0: raise Exception("TAR failed.")
        if p3.returncode != 0: raise Exception("RCLONE failed.")

        print(f"\n-> SUCCESS! Backup uploaded to {full_remote_path}")

    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
    
    finally:
        if os.getcwd() != base_dir:
            os.chdir(base_dir)
        cleanup()