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

def append_log(msg):
    """Log hem konsola hem dosyaya yazar"""
    print(msg)
    try:
        with open(LOG_FILE_PATH, "a") as f:
            f.write(msg + "\n")
    except: pass

def run_host_command(command, env=None, suppress_errors=False):
    cmd_str = ' '.join(command) if isinstance(command, list) else command
    # print(f"HOST_EXEC: {cmd_str}") 

    env_prefix = ""
    if env:
        for k, v in env.items():
            env_prefix += f"export {k}='{v}'; "
    
    full_cmd = f"nsenter -t 1 -m -u -n -i bash -c \"{env_prefix}{cmd_str}\""
    
    try:
        result = subprocess.run(
            full_cmd, shell=True, check=True, capture_output=True, text=True
        )
        return result
    except subprocess.CalledProcessError as e:
        if not suppress_errors:
            error_msg = f"ERROR on HOST: {e.returncode} | {e.stderr.strip()}"
            print(error_msg)
        raise e

def cleanup():
    try: run_host_command(f"umount -l {MOUNT_POINT}", suppress_errors=True)
    except: pass
    try: run_host_command("vgchange -an", suppress_errors=True)
    except: pass
    try: run_host_command("dmsetup remove_all", suppress_errors=True)
    except: pass
    try: run_host_command(f"proxmox-backup-client unmap {DRIVE_NAME}", suppress_errors=True)
    except: pass
    try: run_host_command("losetup -D", suppress_errors=True) 
    except: pass

def find_loop_on_host():
    try:
        res = run_host_command("losetup -a")
        for line in res.stdout.strip().splitlines():
            if DRIVE_NAME in line or "proxmox" in line:
                return line.split(":")[0].strip()
        res = run_host_command("ls -t /dev/loop* | head -n 1")
        if res.stdout and "/dev/loop" in res.stdout:
            return res.stdout.strip()
    except: pass
    return None

def get_candidates(loop_dev):
    """
    Diskleri bulur, etiketlerini okur ve BOYUTLARINA göre sıralar.
    En büyük disk en başa gelir.
    """
    candidates = []
    loop_name = os.path.basename(loop_dev)

    # Yöntem 1: lsblk JSON çıktısı (Daha güvenilir ve detaylı)
    try:
        # JSON formatında çıktı al: NAME, SIZE (byte), FSTYPE, LABEL, PARTLABEL
        # -b: byte cinsinden boyut, -J: json formatı
        res = run_host_command(f"lsblk -b -J -o NAME,SIZE,FSTYPE,LABEL,PARTLABEL {loop_dev}")
        data = json.loads(res.stdout)
        
        blockdevices = data.get("blockdevices", [])
        
        # Bazen lsblk partitionları 'children' altında listeler
        items_to_process = []
        for dev in blockdevices:
            if dev["name"] == loop_name:
                if "children" in dev:
                    items_to_process.extend(dev["children"])
            else:
                items_to_process.append(dev)

        for dev in items_to_process:
            fstype = dev.get("fstype")
            
            # Filtreler: Swap, LVM Member veya boş fstype'ları atla
            if not fstype: continue
            if "swap" in fstype.lower(): continue
            if "LVM2_member" in fstype: continue

            name = dev["name"]
            size_bytes = int(dev.get("size", 0))
            
            # Boyutu okunabilir yap (GB/MB)
            size_human = f"{size_bytes / (1024**3):.2f} GB" if size_bytes > 1024**3 else f"{size_bytes / (1024**2):.2f} MB"
            
            # Etiket oluştur (Label veya PartLabel varsa ekle)
            label = dev.get("label") or dev.get("partlabel") or ""
            desc = f"{fstype.upper()}"
            if label:
                desc += f" - {label}"
            
            full_path = f"/dev/{name}"
            if os.path.exists(f"/dev/mapper/{name}"): full_path = f"/dev/mapper/{name}"

            candidates.append({
                "device": full_path,
                "size_bytes": size_bytes, # Sıralama için ham veri
                "size": size_human,       # Gösterim için
                "type": desc
            })

    except Exception as e:
        print(f"lsblk json failed, falling back: {e}")
        # Fallback: Eski basit yöntem (Eğer JSON çalışmazsa)
        try:
            res = run_host_command(f"lsblk -r -n -o NAME,SIZE,FSTYPE {loop_dev}")
            for line in res.stdout.splitlines():
                parts = line.split()
                name = parts[0]
                size = parts[1] if len(parts) > 1 else "Unknown"
                fstype = parts[2] if len(parts) > 2 else ""
                
                if name == loop_name: continue 
                if not fstype or "swap" in fstype.lower(): continue
                
                full_path = f"/dev/{name}"
                if os.path.exists(f"/dev/mapper/{name}"): full_path = f"/dev/mapper/{name}"
                
                # Fallback modunda size string olduğu için sıralama düzgün çalışmayabilir
                # ama en azından listelenir.
                candidates.append({
                    "device": full_path, 
                    "size_bytes": 0, 
                    "size": size, 
                    "type": fstype
                })
        except: pass

    # LVM Logical Volume Taraması (Ekstra)
    try:
        mapper_res = run_host_command("ls -1 /dev/mapper/")
        for line in mapper_res.stdout.splitlines():
            dev_name = line.strip()
            if "control" in dev_name or "loop" in dev_name: continue
            try:
                # Byte cinsinden boyut al (-b)
                info = run_host_command(f"lsblk -b -r -n -o SIZE,FSTYPE /dev/mapper/{dev_name}")
                parts = info.stdout.split()
                if len(parts) >= 2:
                    size_bytes = int(parts[0])
                    fstype = parts[1]
                    full_path = f"/dev/mapper/{dev_name}"
                    
                    # Listede zaten yoksa ekle
                    if not any(c['device'] == full_path for c in candidates):
                        if fstype and "swap" not in fstype:
                            size_human = f"{size_bytes / (1024**3):.2f} GB"
                            candidates.append({
                                "device": full_path, 
                                "size_bytes": size_bytes, 
                                "size": size_human, 
                                "type": fstype
                            })
            except: pass
    except: pass

    # --- KRİTİK NOKTA: SIRALAMA ---
    # Diskleri boyutlarına göre (Büyükten Küçüğe) sırala.
    # Böylece Windows C: veya Linux Root en üste gelir.
    candidates.sort(key=lambda x: x['size_bytes'], reverse=True)

    if not candidates:
        candidates.append({"device": loop_dev, "size": "Disk Image", "size_bytes": 0, "type": "Raw/Unknown"})
    
    return candidates

def mount_partition_by_index(active_loop, index):
    try: run_host_command(f"kpartx -a -v -s {active_loop}")
    except: pass
    try: 
        run_host_command("vgscan --mknodes")
        run_host_command("vgchange -ay")
    except: pass
    time.sleep(1)

    candidates = get_candidates(active_loop)
    if index >= len(candidates): raise Exception("Invalid partition index")
    
    target = candidates[index]
    device_to_mount = target['device']
    
    run_host_command(f"mkdir -p {MOUNT_POINT}")
    
    cmds = [
        f"mount -o ro {device_to_mount} {MOUNT_POINT}",
        # Windows için force mount ve hiberfile temizliği
        f"ntfs-3g -o ro,remove_hiberfile,force {device_to_mount} {MOUNT_POINT}",
        f"mount -t xfs -o ro,norecovery {device_to_mount} {MOUNT_POINT}",
        f"mount -t ext4 -o ro {device_to_mount} {MOUNT_POINT}"
    ]
    
    for cmd in cmds:
        try:
            run_host_command(cmd, suppress_errors=True)
            return True
        except: continue
    return False

def list_files_or_partitions(config: dict, snapshot: str, partition_id: str = None, path: str = ""):
    print(f"\n--- Exploring: {snapshot} ---")
    host_env = {
        'PBS_PASSWORD': config['pbs_password'],
        'PBS_REPOSITORY': config['pbs_repository_path']
    }
    if 'pbs_fingerprint' in config and config['pbs_fingerprint']:
        host_env['PBS_FINGERPRINT'] = config['pbs_fingerprint']

    try: cleanup() 
    except: pass

    try:
        run_host_command(f"proxmox-backup-client map {snapshot} {DRIVE_NAME} --repository {config['pbs_repository_path']}", env=host_env)
        time.sleep(2) 
        
        active_loop = find_loop_on_host()
        if not active_loop: return {"status": "error", "message": "Loop device not found."}

        try: run_host_command(f"kpartx -a -v -s {active_loop}")
        except: pass
        try: 
            run_host_command("vgscan --mknodes")
            run_host_command("vgchange -ay")
        except: pass
        time.sleep(1)

        if partition_id is None:
            # PARTITION LISTELEME (Boyuta göre sıralı gelir)
            candidates = get_candidates(active_loop)
            
            if not candidates:
                return {"status": "error", "message": "No mountable partitions found."}

            partitions_list = []
            for idx, c in enumerate(candidates):
                partitions_list.append({
                    "id": str(idx),
                    "name": f"Partition {idx+1} ({c['type']})",
                    "size": c['size'],
                    "desc": c.get('device', 'Unknown')
                })
            return {"status": "success", "type": "partitions", "items": partitions_list}
        else:
            idx = int(partition_id)
            if mount_partition_by_index(active_loop, idx):
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
                    return {"status": "success", "type": "files", "items": items, "current_path": path}
                else: return {"status": "error", "message": "Path not found"}
            else: return {"status": "error", "message": "Mount failed. (Filesystem corrupted or unsupported)"}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: cleanup()

def run_backup_process(config: dict, snapshot: str, remote: str, target_folder: str = "", source_paths: str = ""):
    with open(LOG_FILE_PATH, 'w') as f: f.write(f"--- Starting Stream for {snapshot} ---\n")
    
    append_log(f"Snapshot: {snapshot}")
    host_env = {
        'PBS_PASSWORD': config['pbs_password'],
        'PBS_REPOSITORY': config['pbs_repository_path']
    }
    if 'pbs_fingerprint' in config and config['pbs_fingerprint']:
        host_env['PBS_FINGERPRINT'] = config['pbs_fingerprint']

    try:
        cleanup()
        append_log("-> Mapping snapshot on Host...")
        run_host_command(f"proxmox-backup-client map {snapshot} {DRIVE_NAME} --repository {config['pbs_repository_path']}", env=host_env)
        time.sleep(3)

        active_loop = find_loop_on_host()
        if not active_loop: raise Exception("Loop device not found on host.")
        append_log(f"-> Active Loop: {active_loop}")

        append_log("-> Scanning partitions...")
        try: run_host_command(f"kpartx -a -v -s {active_loop}")
        except: pass
        try: 
            run_host_command("vgscan --mknodes")
            run_host_command("vgchange -ay")
        except: pass
        time.sleep(2)

        candidates = get_candidates(active_loop)
        
        mounted = False
        # Otomatik modda: İlk sıradaki (En BÜYÜK) partition'ı dene
        # Bu, EFI veya Recovery'nin seçilmesini engeller.
        for idx in range(len(candidates)):
            if mount_partition_by_index(active_loop, idx):
                mounted = True
                append_log(f"-> Mounted partition index {idx} ({candidates[idx]['type']} - {candidates[idx]['size']})")
                break
        
        if not mounted: raise Exception("Mount failed. No mountable partitions found.")

        vmid = snapshot.split('/')[1]
        timestamp = time.strftime('%Y%m%d-%H%M%S')
        archive_name = f"{vmid}_{timestamp}.tar.gz"
        
        clean_remote = remote.rstrip(":")
        if target_folder.strip():
            full_remote_path = f"{clean_remote}:{target_folder.strip().strip('/')}/{archive_name}"
        else:
            full_remote_path = f"{clean_remote}:{archive_name}"

        os.chdir(MOUNT_POINT)
        
        dirs = ["."]
        if source_paths.strip():
            dirs = [p.strip() for p in source_paths.split(',') if p.strip()]

        # Progress Hesaplama
        append_log("-> Calculating total size for progress stats...")
        total_size = 0
        try:
            du_cmd = ["du", "-sb"] + dirs
            res = subprocess.run(du_cmd, capture_output=True, text=True, check=True)
            for line in res.stdout.splitlines():
                parts = line.split()
                if parts and parts[0].isdigit():
                    total_size += int(parts[0])
            size_mb = total_size / (1024*1024)
            append_log(f"-> Total Size: {size_mb:.2f} MB")
        except: 
            total_size = 0

        tar_cmd = ["tar", "cf", "-"] + dirs
        pigz_cmd = ["pigz", "-1"]
        rclone_cmd = ["rclone", "rcat", full_remote_path, "-P", "--stats", "2s", "--buffer-size", "128M"]
        
        if total_size > 0:
            rclone_cmd.extend(["--size", str(total_size)])

        append_log(f"-> Streaming to {full_remote_path}...")
        
        current_env = os.environ.copy()
        p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=current_env)
        p2 = subprocess.Popen(pigz_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=current_env)
        p1.stdout.close()
        
        p3 = subprocess.Popen(rclone_cmd, stdin=p2.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=current_env)
        p2.stdout.close()
        
        while True:
            line = p3.stderr.readline()
            if not line and p3.poll() is not None:
                break
            if line:
                clean_line = line.strip()
                if "Transferred" in clean_line or "%" in clean_line:
                     append_log(f"[Cloud] {clean_line}")
        
        p3.wait()
        if p3.returncode != 0: raise Exception("Upload failed.")
        append_log("-> SUCCESS: Stream complete.")

    except Exception as e:
        append_log(f"CRITICAL ERROR: {e}")
    finally:
        if os.getcwd() != "/": os.chdir("/")
        cleanup()