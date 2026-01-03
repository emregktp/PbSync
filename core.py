import os
import subprocess
import time
import shutil
import glob
import json
import re

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
    PBS map işlemi sonrası oluşan loop cihazını bulur.
    """
    try:
        res = run_command(["losetup", "-a"], check=False)
        output = res.stdout.strip()
        for line in output.splitlines():
            if DRIVE_NAME in line or "proxmox" in line:
                return line.split(":")[0].strip()
        
        # Fallback: En son oluşturulan loop cihazı
        loop_devs = glob.glob("/dev/loop*")
        numeric_loops = [d for d in loop_devs if d[9:].isdigit()]
        if numeric_loops:
            numeric_loops.sort(key=lambda x: int(x.replace("/dev/loop", "")))
            return numeric_loops[-1]
    except Exception as e:
        print(f"Loop detection error: {e}")
    return None

def get_partitions_via_fdisk(loop_dev):
    """
    Diski fdisk ile okur ve partition başlangıç noktalarını (offset) hesaplar.
    Bu, kpartx/lsblk çalışmadığında hayat kurtarır.
    """
    print(f"-> Scanning partitions via fdisk on {loop_dev}...")
    partitions = []
    try:
        # Sektör boyutunu ve partitionları al
        cmd = ["fdisk", "-l", "-o", "Start,Sectors,Type,Size", loop_dev]
        res = run_command(cmd, check=False)
        lines = res.stdout.splitlines()
        
        sector_size = 512 # Varsayılan
        # Sektör boyutunu loglardan yakala
        for line in lines:
            if "Units:" in line and "bytes" in line:
                match = re.search(r'=\s*(\d+)\s*bytes', line)
                if match: sector_size = int(match.group(1))

        # Tabloyu parse et
        for line in lines:
            parts = line.split()
            # Sayı ile başlayan satırlar partition verisidir
            if not parts or not parts[0].isdigit(): continue
            
            try:
                start_sector = int(parts[0])
                offset = start_sector * sector_size
                partitions.append({
                    "offset": offset,
                    "info": f"Offset: {offset} (Start: {start_sector})"
                })
            except: continue
            
    except Exception as e:
        print(f"Fdisk error: {e}")
    
    return partitions

def create_offset_loop_devices(parent_loop, partitions):
    """
    Partition offsetlerine göre yeni, geçici loop cihazları yaratır.
    """
    created_loops = []
    for p in partitions:
        offset = p['offset']
        print(f"-> Creating loop for partition at offset {offset}...")
        try:
            # losetup -f --show --offset X /dev/loopY
            res = run_command(["losetup", "-f", "--show", "--offset", str(offset), parent_loop])
            new_dev = res.stdout.strip()
            if new_dev:
                created_loops.append(new_dev)
                print(f"   Mapped partition to: {new_dev}")
        except Exception as e:
            print(f"   Failed to map offset {offset}: {e}")
    return created_loops

def cleanup_loop(loop_dev=None, extra_loops=[]):
    print("--- Starting Cleanup ---")
    try: run_command(["umount", "-l", MOUNT_POINT], check=False)
    except: pass
    
    # Ekstra oluşturduğumuz offset looplarını temizle
    for l in extra_loops:
        try: run_command(["losetup", "-d", l], check=False)
        except: pass

    if loop_dev:
        try: run_command(["kpartx", "-d", loop_dev], check=False)
        except: pass
        
    try: run_command(["vgchange", "-an"], check=False)
    except: pass
    try: run_command(["dmsetup", "remove_all"], check=False)
    except: pass
    
    try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
    except: pass
    print("--- Cleanup Finished ---")

def try_mount_device(device):
    print(f"-> Attempting to mount: {device}")
    if not os.path.exists(MOUNT_POINT): os.makedirs(MOUNT_POINT)
    
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
    
    try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
    except: pass
    run_command(["mkdir", "-p", MOUNT_POINT])

    active_loop = None
    offset_loops = []

    try:
        # 1. Map
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        try:
            run_command(map_cmd, env=current_env)
        except subprocess.CalledProcessError as e:
            return {"status": "error", "message": f"Map Failed. Check Privileges. Log: {e.stderr}"}

        time.sleep(2)

        # 2. Identify Loop Device
        active_loop = find_active_loop_device()
        if not active_loop:
            return {"status": "error", "message": "Map successful but loop device not found."}
        print(f"-> Main Loop Device: {active_loop}")

        # 3. Mount Candidates Strategy
        candidates = []

        # Strateji A: Mapper cihazlarını kontrol et
        try: 
            run_command(["kpartx", "-a", "-v", "-s", active_loop])
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        
        base_name = os.path.basename(active_loop)
        mappers = glob.glob(f"/dev/mapper/{base_name}p*") + glob.glob("/dev/mapper/*-root") + glob.glob("/dev/mapper/*-data")
        mappers = [m for m in mappers if "control" not in m]
        candidates.extend(mappers)

        # Strateji B: Fdisk ile Offset Bul ve Manuel Loop Yarat
        if not candidates:
            print("-> No mapper devices found. Trying fdisk offset calculation...")
            partitions = get_partitions_via_fdisk(active_loop)
            if partitions:
                offset_loops = create_offset_loop_devices(active_loop, partitions)
                candidates.extend(offset_loops)
            else:
                print("-> No partitions found in fdisk. Assuming raw filesystem.")
                candidates.append(active_loop)

        # 4. Mount
        mounted = False
        # Ters çevir (Genelde en büyük partition sondadır)
        for dev in reversed(candidates):
            if try_mount_device(dev):
                mounted = True
                break
        
        if not mounted:
            return {"status": "error", "message": "Could not mount any partition. (Tried kpartx & fdisk offset)"}

        # 5. List Files
        safe_path = os.path.normpath(os.path.join(MOUNT_POINT, path.strip('/')))
        if not safe_path.startswith(MOUNT_POINT): safe_path = MOUNT_POINT

        items = []
        if os.path.exists(safe_path):
            with os.scandir(safe_path) as entries:
                for entry in entries:
                    items.append({
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "path": os.path.relpath(entry.path, MOUNT_POINT)
                    })
            items.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
            return {"status": "success", "items": items, "current_path": path}
        else:
            return {"status": "error", "message": "Path not found"}

    except Exception as e:
        print(f"Global Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        cleanup_loop(active_loop, offset_loops)

def run_backup_process(config: dict, snapshot: str, remote: str, target_folder: str = "", source_paths: str = ""):
    print(f"\n--- Starting Stream: {snapshot} ---")
    current_env = os.environ.copy()
    active_loop = None
    offset_loops = []

    try:
        try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
        except: pass
        run_command(["mkdir", "-p", MOUNT_POINT])

        print("-> Mapping...")
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(2)

        active_loop = find_active_loop_device()
        if not active_loop: raise Exception("Loop device not found.")
        print(f"-> Active Loop: {active_loop}")

        # Activate & Find Candidates (Copy of Explorer Logic)
        candidates = []
        try: 
            run_command(["kpartx", "-a", "-v", "-s", active_loop])
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        
        base_name = os.path.basename(active_loop)
        mappers = glob.glob(f"/dev/mapper/{base_name}p*") + glob.glob("/dev/mapper/*-root") + glob.glob("/dev/mapper/*-data")
        mappers = [m for m in mappers if "control" not in m]
        candidates.extend(mappers)

        if not candidates:
            partitions = get_partitions_via_fdisk(active_loop)
            if partitions:
                offset_loops = create_offset_loop_devices(active_loop, partitions)
                candidates.extend(offset_loops)
            else:
                candidates.append(active_loop)

        # Mount
        mounted = False
        for dev in reversed(candidates):
            if try_mount_device(dev):
                mounted = True
                break
        
        if not mounted: raise Exception("Mount failed.")

        # Stream
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

            for line in iter(p3.stderr.readline, ''):
                print(f"   [Cloud] {line.strip()}")
            
            p3.wait()

        if p3.returncode != 0: raise Exception("Upload failed.")
        print("-> SUCCESS: Stream complete.")

    except Exception as e:
        print(f"CRITICAL ERROR: {e}")
    finally:
        if os.getcwd() != current_env.get("PWD", "/"): os.chdir(current_env.get("PWD", "/"))
        cleanup_loop(active_loop, offset_loops)