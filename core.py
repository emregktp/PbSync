import os
import subprocess
import time
import re
import shutil

# --- Constants ---
DRIVE_NAME = "drive-scsi0.img"
MAIN_LOOP_DEV = "/dev/loop0"
# İkinci bir loop cihazı (Partition için) dinamik olarak atanacak
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

def cleanup():
    print("--- Starting Cleanup ---")
    # 1. Unmount
    run_command(["umount", "-l", MOUNT_POINT], check=False)
    
    # 2. Partition Loop cihazlarını temizle (loop0 haricindekiler)
    try:
        # Tüm loop cihazlarını bul ve temizle
        out = subprocess.check_output(["losetup", "-a"], text=True)
        for line in out.splitlines():
            # Eğer loop0 üzerine kurulu başka bir loop varsa (offsetli) onu sil
            if "loop0)" in line or "loop0 (" in line: 
                parts = line.split(":")
                loop_dev = parts[0]
                if loop_dev != MAIN_LOOP_DEV:
                    print(f"Cleaning up partition loop: {loop_dev}")
                    run_command(["losetup", "-d", loop_dev], check=False)
    except:
        pass

    # 3. Ana PBS Map işlemini kaldır
    run_command(["proxmox-backup-client", "unmap", MAIN_LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

def get_partition_offset_and_setup_loop(device_path):
    """
    1. fdisk ile diski okur.
    2. En büyük partition'ın başlangıç sektörünü bulur.
    3. Offset hesabı yapar (Sektör * 512).
    4. losetup ile o offsete yeni bir loop cihazı bağlar.
    """
    print(f"-> Analyzing partition table for '{device_path}'...")
    
    try:
        # fdisk çıktısını al
        cmd = ["fdisk", "-l", "-o", "Start,Sectors,Type", device_path]
        result = run_command(cmd, check=False)
        
        lines = result.stdout.strip().split('\n')
        partitions = []
        sector_size = 512 # Varsayılan
        
        # Sektör boyutunu teyit et
        for line in lines:
            if "Units:" in line and "bytes" in line:
                match = re.search(r'=\s*(\d+)\s*bytes', line)
                if match: sector_size = int(match.group(1))

        # Partition satırlarını parse et
        for line in lines:
            parts = line.split()
            # Sayı ile başlayan satırlar partition bilgisidir
            if not parts or not parts[0].isdigit(): continue
                
            try:
                start_sector = int(parts[0])
                total_sectors = int(parts[1])
                size_bytes = total_sectors * sector_size
                offset_bytes = start_sector * sector_size
                
                partitions.append({"offset": offset_bytes, "size": size_bytes})
            except: continue

        if not partitions:
            print("-> No partition table found. Assuming RAW filesystem.")
            return device_path # Offset yok, direkt cihazı dön

        # En büyük partition'ı seç
        largest = sorted(partitions, key=lambda x: x["size"], reverse=True)[0]
        offset = largest["offset"]
        print(f"-> Largest partition detected at Offset: {offset} bytes (Size: {largest['size']})")

        # YENİ LOOP CİHAZI OLUŞTUR (Offsetli)
        # losetup -f --show --offset X /dev/loop0
        print(f"-> Creating dedicated loop device for partition...")
        setup_res = run_command(["losetup", "--find", "--show", "--offset", str(offset), device_path])
        new_loop_dev = setup_res.stdout.strip()
        
        return new_loop_dev

    except Exception as e:
        print(f"WARNING: Partition logic failed ({e}). Returning raw device.")
        return device_path

def mount_device(device_path):
    """
    Farklı dosya sistemi türlerini deneyerek mount eder.
    """
    print(f"-> Attempting to mount: {device_path}")
    
    # 1. Deneme: Auto (ext4, xfs vb)
    try:
        run_command(["mount", "-o", "ro", device_path, MOUNT_POINT])
        return
    except: pass

    # 2. Deneme: NTFS (Kirli olsa bile zorla)
    try:
        # remove_hiberfile: Windows düzgün kapanmadıysa açılmasını sağlar
        run_command(["ntfs-3g", "-o", "ro,remove_hiberfile", device_path, MOUNT_POINT])
        return
    except: pass

    # 3. Deneme: XFS (Log replay yapmadan)
    try:
        run_command(["mount", "-t", "xfs", "-o", "ro,norecovery", device_path, MOUNT_POINT])
        return
    except: pass

    raise Exception(f"Could not mount {device_path}. All filesystem drivers failed.")

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
        time.sleep(1)

        # 2. Offset Bul ve Yeni Loop Oluştur
        target_dev = get_partition_offset_and_setup_loop(MAIN_LOOP_DEV)

        # 3. Mount
        mount_device(target_dev)

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
        target_dev = get_partition_offset_and_setup_loop(MAIN_LOOP_DEV)
        mount_device(target_dev)
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