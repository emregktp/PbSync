import os
import subprocess
import time
import shutil
import glob
import json
import re

# --- Constants ---
# Sadece dosya adı referansı, fiziksel path değil.
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
        if result.stderr: 
            print(f"STDERR: {result.stderr.strip()}")
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Command failed. Return Code: {e.returncode}")
        print(f"STDOUT: {e.stdout}")
        print(f"STDERR: {e.stderr}")
        raise

def find_mapped_loop_device():
    """
    PBS tarafından oluşturulan loop cihazını bulur.
    proxmox-backup-client map işlemi arka planda bir loop cihazı oluşturur.
    Bunu losetup ile tarayarak bulacağız.
    """
    try:
        # losetup -a tüm loop cihazlarını listeler.
        # PBS map işlemi genellikle dosya yolunda 'proxmox-backup' veya repository adını içerir.
        res = run_command(["losetup", "-a"], check=False)
        output = res.stdout.strip()
        
        # En son oluşturulan loop cihazını bulmaya çalışalım veya backup ismine göre filtreleyelim.
        # Genellikle PBS client map ettiğinde backing file olarak garip bir FUSE path'i görünür.
        
        # En basit yöntem: En son loop cihazını kontrol etmektir ama riskli.
        # Daha güvenli: lsblk ile 'loop' tipindeki ve boyutu 0 olmayan cihazları alıp,
        # map işleminden hemen sonra hangisinin belirdiğine bakmak.
        
        # Ancak PBS Client map komutu nereye map ettiğini çıktı olarak vermez.
        # Bu yüzden sistemdeki tüm loop cihazlarını tarayıp partitions/LVM arayacağız.
        
        # Alternatif: Map işlemi /dev/loopX oluşturur. 
        # Biz map öncesi ve sonrası loop listesini karşılaştırabiliriz ama bu stateless olmaz.
        
        # Şimdilik en son (highest number) loop cihazını aday olarak alalım.
        loop_devs = glob.glob("/dev/loop*")
        # Sadece sayı ile bitenleri al (loop-control vb hariç)
        loop_devs = [d for d in loop_devs if d[9:].isdigit()]
        
        if not loop_devs: return None
        
        # Sort by number
        loop_devs.sort(key=lambda x: int(x.replace("/dev/loop", "")))
        
        # En sonuncuyu döndür (Genellikle yeni map edilen sondadır)
        # Ama emin olmak için 'losetup' çıktısında 'drive-scsi0' arayabiliriz.
        for line in output.splitlines():
            if DRIVE_NAME in line or "pbs" in line.lower() or "backup" in line.lower():
                # /dev/loopX: [0034]:...
                return line.split(":")[0].strip()
                
        # Bulamazsak en sonuncuyu deneyelim
        return loop_devs[-1]

    except Exception as e:
        print(f"Loop detection error: {e}")
        return None

def cleanup_loop(loop_dev):
    if not loop_dev: return
    print(f"--- Cleaning up {loop_dev} ---")
    try: run_command(["umount", "-l", MOUNT_POINT], check=False)
    except: pass
    try: run_command(["vgchange", "-an"], check=False)
    except: pass
    try: run_command(["kpartx", "-d", loop_dev], check=False)
    except: pass
    try: run_command(["dmsetup", "remove_all"], check=False)
    except: pass
    # Unmap işlemi spesifik loop dev üzerinden değil repository üzerinden yapılır ama
    # burada manuel map yaptığımız için unmap'i client üzerinden çağırmalıyız.
    # Ancak client unmap komutu 'name' ister (drive-scsi0.img).
    try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
    except: pass

def get_mount_candidates(loop_dev):
    print(f"-> Analyzing partitions on {loop_dev}...")
    candidates = []

    # 1. lsblk ile JSON al
    try:
        cmd = ["lsblk", "-b", "-J", "-o", "NAME,SIZE,TYPE,FSTYPE,PKNAME", loop_dev]
        res = run_command(cmd)
        data = json.loads(res.stdout)
        
        def extract_candidates(devices):
            found = []
            for dev in devices:
                name = dev.get('name')
                full_path = ""
                
                if os.path.exists(f"/dev/mapper/{name}"): full_path = f"/dev/mapper/{name}"
                elif os.path.exists(f"/dev/{name}"): full_path = f"/dev/{name}"
                
                if full_path: found.append(full_path)
                if 'children' in dev: found.extend(extract_candidates(dev['children']))
            return found

        candidates = extract_candidates(data.get('blockdevices', []))
    except: pass
    
    # 2. Fallback Glob (loopXp1, loopXp2...)
    if not candidates and loop_dev:
        # /dev/loop0 -> /dev/mapper/loop0p*
        base_name = os.path.basename(loop_dev) # loop0
        mappers = glob.glob(f"/dev/mapper/{base_name}p*")
        candidates.extend(mappers)

    # 3. Raw Device
    if not candidates and loop_dev:
        print(f"-> No partitions found. Using raw device {loop_dev}.")
        candidates = [loop_dev]
    
    # Ana cihazı sona at
    if loop_dev in candidates and len(candidates) > 1:
        candidates.remove(loop_dev)
        candidates.append(loop_dev)

    print(f"-> Candidates: {candidates}")
    return candidates

def try_mount(device_path):
    print(f"-> Trying to mount: {device_path}")
    if not os.path.exists(MOUNT_POINT): os.makedirs(MOUNT_POINT)

    strategies = [
        ["mount", "-o", "ro", device_path, MOUNT_POINT], 
        ["mount", "-t", "xfs", "-o", "ro,norecovery", device_path, MOUNT_POINT],
        ["ntfs-3g", "-o", "ro,remove_hiberfile", device_path, MOUNT_POINT],
        ["mount", "-t", "ext4", "-o", "ro", device_path, MOUNT_POINT]
    ]

    for cmd in strategies:
        try:
            run_command(cmd)
            print(f"   SUCCESS! Mounted {device_path}")
            return True
        except: continue
    return False

def list_files_in_snapshot(config: dict, snapshot: str, path: str = ""):
    print(f"\n--- Exploring Snapshot: {snapshot} ---")
    current_env = os.environ.copy()
    
    # Ön temizlik (Genel)
    try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
    except: pass
    
    run_command(["mkdir", "-p", MOUNT_POINT])

    active_loop = None

    try:
        # 1. Map (Doğru komut: map <snap> <name>)
        # Loop device argümanı VERMİYORUZ. Sistem atıyor.
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        try:
            run_command(map_cmd, env=current_env)
        except subprocess.CalledProcessError as e:
            return {"status": "error", "message": f"Mapping Failed: {e}. Check Docker privileges."}

        time.sleep(1) # Cihazın oluşmasını bekle

        # 2. Hangi loop cihazı atandı bul
        active_loop = find_mapped_loop_device()
        if not active_loop:
            return {"status": "error", "message": "Map successful but could not identify loop device."}
        
        print(f"-> Identified Active Loop Device: {active_loop}")

        # 3. Activate
        try: run_command(["kpartx", "-a", "-v", "-s", active_loop])
        except: pass
        try: 
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        time.sleep(1)

        # 4. Mount
        candidates = get_mount_candidates(active_loop)
        mounted = False
        for dev in candidates:
            if try_mount(dev):
                mounted = True
                break
        
        if not mounted:
            cleanup_loop(active_loop)
            return {"status": "error", "message": "Could not mount partition. Encrypted or unsupported filesystem."}

        # 5. List Files
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
        if active_loop: cleanup_loop(active_loop)

def run_backup_process(config: dict, snapshot: str, remote: str, target_folder: str = "", source_paths: str = ""):
    print(f"\n{'='*60}\nSTARTING NEW STREAM PROCESS\n{'='*60}")
    print(f"Source: {snapshot}")
    
    current_env = os.environ.copy()
    base_dir = current_env.get("PWD", "/")
    active_loop = None

    try:
        # Temizlik
        try: run_command(["proxmox-backup-client", "unmap", DRIVE_NAME], check=False)
        except: pass
        run_command(["mkdir", "-p", MOUNT_POINT])

        # 1. Map
        print(f"\n-> Mapping snapshot...")
        map_cmd = [
            "proxmox-backup-client", "map", snapshot, DRIVE_NAME,
            "--repository", config['pbs_repository_path']
        ]
        run_command(map_cmd, env=current_env)
        time.sleep(2)

        # 2. Find Loop
        active_loop = find_mapped_loop_device()
        if not active_loop:
            raise Exception("Could not find mapped loop device.")
        print(f"-> Mapped to: {active_loop}")

        # 3. Activate
        print("-> Activating partitions/LVM...")
        try: run_command(["kpartx", "-a", "-v", "-s", active_loop])
        except: pass
        try: 
            run_command(["vgscan", "--mknodes"], check=False)
            run_command(["vgchange", "-ay"], check=False)
        except: pass
        time.sleep(1)

        # 4. Mount
        candidates = get_mount_candidates(active_loop)
        mounted = False
        for dev in candidates:
            if try_mount(dev):
                mounted = True
                break
        
        if not mounted: raise Exception("Failed to mount filesystem.")

        # 5. Stream
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
        if active_loop: cleanup_loop(active_loop)