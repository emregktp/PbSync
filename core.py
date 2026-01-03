import os
import subprocess
import time
import re

# --- Constants ---
DRIVE_NAME = "drive-scsi0.img"
MAIN_LOOP_DEV = "/dev/loop0"
PARTITION_LOOP_DEV = None # Dinamik olarak atanacak
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

def get_largest_partition_offset(device_path):
    """
    Diskin partition tablosunu okur ve en büyük bölümün
    BAŞLANGIÇ OFFSET'ini (byte cinsinden) döner.
    """
    print(f"-> Analyzing partition table for '{device_path}'...")
    try:
        # fdisk ile sektörleri listele
        cmd = ["fdisk", "-l", "-o", "Start,Sectors,Type", device_path]
        result = run_command(cmd, check=False)
        
        lines = result.stdout.strip().split('\n')
        partitions = []
        
        sector_size = 512 # Varsayılan
        
        # Sektör boyutunu bul (Units: sectors of 1 * 512 = 512 bytes)
        for line in lines:
            if "Units:" in line and "bytes" in line:
                match = re.search(r'=\s*(\d+)\s*bytes', line)
                if match:
                    sector_size = int(match.group(1))
                    print(f"   Detected Sector Size: {sector_size}")

        # Partition satırlarını oku
        for line in lines:
            # Sadece sayı ile başlayan satırları al (Start sütunu)
            parts = line.split()
            if not parts or not parts[0].isdigit():
                continue
                
            try:
                start_sector = int(parts[0])
                total_sectors = int(parts[1])
                size_bytes = total_sectors * sector_size
                offset_bytes = start_sector * sector_size
                
                partitions.append({
                    "offset": offset_bytes,
                    "size": size_bytes
                })
            except:
                continue

        if not partitions:
            print("-> No partitions found. Assuming raw filesystem (Offset 0).")
            return 0
            
        # En büyük partition'ı bul
        largest = sorted(partitions, key=lambda x: x["size"], reverse=True)[0]
        print(f"-> Largest partition detected at Offset: {largest['offset']} (Size: {largest['size']})")
        return largest["offset"]

    except Exception as e:
        print(f"WARNING: Partition analysis failed: {e}. Defaulting to Offset 0.")
        return 0

def cleanup():
    print("--- Starting Cleanup ---")
    global PARTITION_LOOP_DEV
    
    # 1. Mount'u kaldır
    run_command(["umount", "-l", MOUNT_POINT], check=False)
    
    # 2. Partition Loop'u serbest bırak
    if PARTITION_LOOP_DEV:
        run_command(["losetup", "-d", PARTITION_LOOP_DEV], check=False)
        PARTITION_LOOP_DEV = None
    else:
        # Garanti temizlik: /dev/loop0 üzerine kurulu diğer loopları bul ve sil
        try:
            out = subprocess.check_output(["losetup", "-a"], text=True)
            for line in out.splitlines():
                if MAIN_LOOP_DEV in line:
                    loop_dev = line.split(':')[0]
                    if loop_dev != MAIN_LOOP_DEV:
                        run_command(["losetup", "-d", loop_dev], check=False)
        except: pass

    # 3. Ana Loop'u (PBS Map) serbest bırak
    run_command(["proxmox-backup-client", "unmap", MAIN_LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

def setup_partition_device(offset):
    """
    Ana disk üzerindeki belirli bir offset'e yeni bir loop cihazı bağlar.
    Bu sayede partition'ı ayrı bir disk gibi görürüz.
    """
    global PARTITION_LOOP_DEV
    if offset == 0:
        return MAIN_LOOP_DEV
    
    print(f"-> Creating loop device for partition at offset {offset}...")
    # losetup ile offset vererek yeni bir loop oluştur
    result = run_command(["losetup", "--find", "--show", "--offset", str(offset), MAIN_LOOP_DEV])
    PARTITION_LOOP_DEV = result.stdout.strip()
    print(f"-> Partition mapped to: {PARTITION_LOOP_DEV}")
    return PARTITION_LOOP_DEV

def list_files_in_snapshot(config: dict, snapshot: str, path: str = ""):
    print(f"\n--- Exploring Snapshot: {snapshot} Path: {path} ---")
    current_env = os.environ.copy()
    cleanup() 
    run_command(["mkdir", "-p", MOUNT_POINT])

    try:
        # 1. Map (Diski getir)
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(1)

        # 2. Offset Bul ve Bağla
        offset = get_largest_partition_offset(MAIN_LOOP_DEV)
        target_dev = setup_partition_device(offset)

        # 3. Mount Et (Hata toleranslı)
        try:
            # Önce auto detect ile dene
            run_command(["mount", "-o", "ro", target_dev, MOUNT_POINT])
        except:
            try:
                # NTFS ise
                run_command(["ntfs-3g", "-o", "ro", target_dev, MOUNT_POINT])
            except:
                # XFS/LVM ise (Genellikle norecovery gerekir)
                run_command(["mount", "-o", "ro,norecovery", target_dev, MOUNT_POINT])

        # 4. Listele
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

        # 2. Partition Offset & Mount
        offset = get_largest_partition_offset(MAIN_LOOP_DEV)
        target_dev = setup_partition_device(offset)
        
        print(f"\n-> Mounting {target_dev}...")
        mounted = False
        
        # Mount stratejileri (Sırayla dener)
        mount_attempts = [
            ["mount", "-o", "ro", target_dev, MOUNT_POINT], # Auto (ext4 vb)
            ["ntfs-3g", "-o", "ro", target_dev, MOUNT_POINT], # NTFS (Windows)
            ["mount", "-t", "xfs", "-o", "ro,norecovery", target_dev, MOUNT_POINT] # XFS (CentOS/RHEL)
        ]

        last_error = ""
        for cmd in mount_attempts:
            try:
                run_command(cmd)
                mounted = True
                print("-> Mount successful.")
                break
            except Exception as e:
                last_error = str(e)
                continue
        
        if not mounted:
            raise Exception(f"Failed to mount filesystem. Last error: {last_error}")

        # 3. Stream
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