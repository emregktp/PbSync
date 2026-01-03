import os
import subprocess
import time
import shutil

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
        raise # Hata fırlat

def cleanup():
    print("--- Starting Cleanup ---")
    # 1. Unmount
    run_command(["umount", "-l", MOUNT_POINT], check=False)
    
    # 2. Deactivate LVM
    run_command(["vgchange", "-an"], check=False)
    
    # 3. Clean up partition mappings
    run_command(["kpartx", "-d", MAIN_LOOP_DEV], check=False)
    run_command(["dmsetup", "remove_all"], check=False)

    # 4. Unmap main drive
    run_command(["proxmox-backup-client", "unmap", MAIN_LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

def detect_and_mount():
    """
    1. Haritalama (kpartx)
    2. LVM Aktivasyonu (vgchange)
    3. Mount edilebilir alanları (ext4, xfs, ntfs) bulma
    4. En büyüğünü mount etme
    """
    print(f"-> Scanning block devices on {MAIN_LOOP_DEV}...")
    
    # A. Standart Partitionları Tanıt
    try:
        run_command(["kpartx", "-a", "-v", "-s", MAIN_LOOP_DEV])
    except: pass
    
    # B. LVM Yapılarını Tanıt (Linux VM'ler için kritik)
    try:
        run_command(["vgscan", "--mknodes"], check=False)
        run_command(["vgchange", "-ay"], check=False)
    except: pass

    time.sleep(2) # Kernel'in cihazları oluşturmasını bekle

    # C. Mount Edilebilir Dosya Sistemlerini Bul
    # KRİTİK DÜZELTME: Sadece MAIN_LOOP_DEV içindeki cihazları listele
    # Ana disk (sda) karışmasın diye parametre eklendi.
    cmd = ["lsblk", "-r", "-n", "-o", "NAME,FSTYPE,SIZE", MAIN_LOOP_DEV]
    result = run_command(cmd)
    
    candidates = []
    
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2: continue # FSTYPE yoksa atla
        
        name = parts[0]
        fstype = parts[1]
        size = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        
        # Gereksizleri filtrele
        if fstype in ['LVM2_member', 'swap', 'iso9660', 'linux_raid_member']: 
            continue
        if not fstype: continue

        # Tam yolu oluştur
        dev_path = f"/dev/{name}"
        # Eğer /dev/mapper altında ise (LVM veya kpartx) orayı kullan
        if os.path.exists(f"/dev/mapper/{name}"):
            dev_path = f"/dev/mapper/{name}"
            
        candidates.append({"path": dev_path, "fstype": fstype, "size": size})

    if not candidates:
        # Aday bulunamadıysa loglara daha detaylı bilgi bas
        print("DEBUG: lsblk output was:")
        print(result.stdout)
        raise Exception(f"No mountable filesystems (ext4/xfs/ntfs) detected inside {MAIN_LOOP_DEV}! Disk might be encrypted or raw.")

    # En büyüğünü seç (Genellikle data partition'ıdır)
    target = sorted(candidates, key=lambda x: x["size"], reverse=True)[0]
    target_dev = target["path"]
    target_fs = target["fstype"]
    
    print(f"-> Detected Filesystem: {target_fs} on {target_dev} ({target['size']} bytes)")
    
    # D. Mount İşlemi
    if not os.path.exists(MOUNT_POINT): os.makedirs(MOUNT_POINT)
    
    mount_opts = ["mount", "-o", "ro"] # Read-only varsayılan
    
    if target_fs == "xfs":
        mount_opts = ["mount", "-t", "xfs", "-o", "ro,norecovery"]
    elif target_fs == "ntfs" or target_fs == "ntfs-3g":
        mount_opts = ["ntfs-3g", "-o", "ro,remove_hiberfile"]
    
    print(f"-> Mounting with: {' '.join(mount_opts)} {target_dev}")
    run_command(mount_opts + [target_dev, MOUNT_POINT])

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

        # 2. Detect & Mount
        detect_and_mount()

        # 3. List Files
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

        # 2. Detect & Mount
        detect_and_mount()
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