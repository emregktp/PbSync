import os
import subprocess
import time
import shutil
import glob
import json

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
    # Loop cihazını zorla serbest bırak
    run_command(["losetup", "-d", MAIN_LOOP_DEV], check=False) 
    run_command(["proxmox-backup-client", "unmap", MAIN_LOOP_DEV], check=False)
    print("--- Cleanup Finished ---")

def get_mount_candidates():
    """
    SADECE loop0 ve ona bağlı mapper cihazlarını tarar.
    Asla sda, sdb gibi fiziksel disklere bakmaz.
    """
    print(f"-> Analyzing partitions on {MAIN_LOOP_DEV}...")
    
    # 1. Eğer ana cihaz yoksa (Map başarısızsa) direkt çık.
    if not os.path.exists(MAIN_LOOP_DEV):
        raise Exception(f"Device {MAIN_LOOP_DEV} does not exist. Map failed!")

    candidates = []

    # 2. lsblk ile SADECE loop0'ı sorgula
    try:
        # -b: bytes, -J: json, -o: fields
        # loop0 cihazını vererek aramayı kısıtlıyoruz
        cmd = ["lsblk", "-b", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,PKNAME", MAIN_LOOP_DEV]
        res = run_command(cmd)
        data = json.loads(res.stdout)
        
        def extract_candidates(devices):
            found = []
            for dev in devices:
                name = dev.get('name')
                # Tam yolunu bul
                full_path = ""
                if os.path.exists(f"/dev/mapper/{name}"):
                    full_path = f"/dev/mapper/{name}"
                elif os.path.exists(f"/dev/{name}"):
                    full_path = f"/dev/{name}"
                
                if full_path:
                    found.append(full_path)
                
                if 'children' in dev:
                    found.extend(extract_candidates(dev['children']))
            return found

        candidates = extract_candidates(data.get('blockdevices', []))

    except Exception as e:
        print(f"lsblk parsing error: {e}")
    
    # 3. Eğer lsblk bir şey bulamazsa manuel mapper kontrolü yap
    if not candidates:
        mappers = glob.glob("/dev/mapper/loop0*")
        candidates.extend(mappers)

    # 4. Hiçbiri yoksa raw loop0'ı dene
    if not candidates:
        print("-> No partitions found inside loop0. Using raw device.")
        candidates = [MAIN_LOOP_DEV]
    
    # 5. Ana Loop cihazını listenin sonuna at (Önce partitionları denesin)
    if MAIN_LOOP_DEV in candidates and len(candidates) > 1:
        candidates.remove(MAIN_LOOP_DEV)
        candidates.append(MAIN_LOOP_DEV)

    print(f"-> Safe Candidates: {candidates}")
    return candidates

def try_mount(device_path):
    print(f"-> Trying to mount: {device_path}")
    if not os.path.exists(MOUNT_POINT): os.makedirs(MOUNT_POINT)

    strategies = [
        ["mount", "-o", "ro", device_path, MOUNT_POINT], # Auto
        ["mount", "-t", "xfs", "-o", "ro,norecovery", device_path, MOUNT_POINT], # XFS
        ["ntfs-3g", "-o", "ro,remove_hiberfile", device_path, MOUNT_POINT], # NTFS
        ["mount", "-t", "ext4", "-o", "ro", device_path, MOUNT_POINT] # EXT4
    ]

    for cmd in strategies:
        try:
            run_command(cmd)
            print(f"   SUCCESS! Mounted {device_path}")
            return True
        except:
            continue
    return False

def list_files_in_snapshot(config: dict, snapshot: str, path: str = ""):
    print(f"\n--- Exploring Snapshot: {snapshot} ---")
    current_env = os.environ.copy()
    cleanup() 
    run_command(["mkdir", "-p", MOUNT_POINT])

    try:
        # 1. Map
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        # Map hatasını yakala (OS Error 95 burada oluşuyor)
        try:
            run_command(map_cmd, env=current_env)
        except subprocess.CalledProcessError:
            return {"status": "error", "message": "Failed to map snapshot. Check PBS permissions or FUSE support on host."}

        time.sleep(2)

        # 2. Activate
        try: run_command(["kpartx", "-a", "-v", "-s", MAIN_LOOP_DEV])
        except: pass
        try: 
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        time.sleep(1)

        # 3. Mount
        candidates = get_mount_candidates()
        mounted = False
        for dev in candidates:
            if try_mount(dev):
                mounted = True
                break
        
        if not mounted:
            return {"status": "error", "message": "Could not mount partition. Disk might be encrypted or unsupported."}

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

        # 3. Mount
        candidates = get_mount_candidates()
        mounted = False
        for dev in candidates:
            if try_mount(dev):
                mounted = True
                break
        
        if not mounted:
            raise Exception("Failed to mount filesystem from snapshot.")

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