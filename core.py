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

def run_host_command(command, env=None):
    cmd_str = ' '.join(command) if isinstance(command, list) else command
    # append_log(f"HOST_EXEC: {cmd_str}") # Çok kirletmesin diye kapalı
    print(f"HOST_EXEC: {cmd_str}")

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
        append_log(f"ERROR on HOST: {e.returncode} | {e.stderr.strip()}")
        raise e

def cleanup():
    print("--- Cleaning up ---")
    try: run_host_command(f"umount -l {MOUNT_POINT}")
    except: pass
    try: run_host_command("vgchange -an")
    except: pass
    try: run_host_command("dmsetup remove_all")
    except: pass
    try: run_host_command(f"proxmox-backup-client unmap {DRIVE_NAME}")
    except: pass
    try: run_host_command("losetup -D") 
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
    candidates = []
    try:
        loop_name = os.path.basename(loop_dev)
        res = run_host_command(f"lsblk -r -n -o NAME,SIZE,FSTYPE {loop_dev}")
        for line in res.stdout.splitlines():
            parts = line.split()
            name = parts[0]
            size = parts[1] if len(parts) > 1 else "Unknown"
            fstype = parts[2] if len(parts) > 2 else "Raw"
            if name == loop_name: continue 
            full_path = f"/dev/{name}"
            if os.path.exists(f"/dev/mapper/{name}"): full_path = f"/dev/mapper/{name}"
            candidates.append({"device": full_path, "size": size, "type": fstype})
    except: pass

    if not candidates:
        try:
            res = run_host_command(f"fdisk -l {loop_dev}")
            for line in res.stdout.splitlines():
                parts = line.split()
                if parts and parts[0].startswith(loop_dev) and parts[0] != loop_dev:
                    size_str = parts[-1] if len(parts) > 4 else "?"
                    candidates.append({"device": "OFFSET_PARTITION", "size": size_str, "type": "Partition", "fdisk_line": line})
        except: pass

    if not candidates:
        candidates.append({"device": loop_dev, "size": "Disk Image", "type": "Raw"})
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
    
    if device_to_mount == "OFFSET_PARTITION":
        line = target['fdisk_line']
        parts = line.split()
        start_idx = 1
        if parts[1] == "*": start_idx = 2
        start_sector = int(parts[start_idx])
        offset = start_sector * 512
        res = run_host_command(f"losetup -f --show --offset {offset} {active_loop}")
        device_to_mount = res.stdout.strip()

    run_host_command(f"mkdir -p {MOUNT_POINT}")
    cmds = [
        f"mount -o ro {device_to_mount} {MOUNT_POINT}",
        f"mount -t xfs -o ro,norecovery {device_to_mount} {MOUNT_POINT}",
        f"ntfs-3g -o ro,remove_hiberfile {device_to_mount} {MOUNT_POINT}",
        f"mount -t ext4 -o ro {device_to_mount} {MOUNT_POINT}"
    ]
    
    for cmd in cmds:
        try:
            run_host_command(cmd)
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
        time.sleep(1)
        active_loop = find_loop_on_host()
        if not active_loop: return {"status": "error", "message": "Loop device not found."}

        if partition_id is None:
            candidates = get_candidates(active_loop)
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
            else: return {"status": "error", "message": "Mount failed."}
    except Exception as e: return {"status": "error", "message": str(e)}
    finally: cleanup()

def run_backup_process(config: dict, snapshot: str, remote: str, target_folder: str = "", source_paths: str = ""):
    # Log dosyasını sıfırla
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
        time.sleep(2)

        active_loop = find_loop_on_host()
        if not active_loop: raise Exception("Loop device not found on host.")
        append_log(f"-> Active Loop: {active_loop}")

        # Otomatik en mantıklı bölümü bul ve bağla (Smart Mount Logic)
        append_log("-> Scanning for partitions...")
        candidates = get_candidates(active_loop)
        
        mounted = False
        # Sondan başa dene (Genelde büyük partition sondadır)
        for idx in range(len(candidates)-1, -1, -1):
            if mount_partition_by_index(active_loop, idx):
                mounted = True
                append_log(f"-> Mounted partition index {idx}")
                break
        
        if not mounted: raise Exception("Mount failed on Host.")

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
        # -P progress verir, stderr'e basar
        rclone_cmd = ["rclone", "rcat", full_remote_path, "-P", "--stats", "1s", "--buffer-size", "128M"]

        append_log(f"-> Streaming to {full_remote_path}...")
        
        current_env = os.environ.copy()
        # Process zinciri
        p1 = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=current_env)
        p2 = subprocess.Popen(pigz_cmd, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=current_env)
        p1.stdout.close()
        
        # Rclone'un stderr çıktısını (progress) okuyacağız
        p3 = subprocess.Popen(rclone_cmd, stdin=p2.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=current_env)
        p2.stdout.close()
        
        # Anlık okuma döngüsü
        while True:
            line = p3.stderr.readline()
            if not line and p3.poll() is not None:
                break
            if line:
                # Logu dosyaya yaz ama konsolu boğma
                clean_line = line.strip()
                if "Transferred" in clean_line or "%" in clean_line:
                     # Sadece progress satırlarını yaz
                     # Dosyaya her satırı yazmak yerine üzerine yazabiliriz veya append ederiz
                     # Şimdilik append edelim, arayüz son satırları okur.
                     append_log(f"[Cloud] {clean_line}")
        
        p3.wait()
        if p3.returncode != 0: raise Exception("Upload failed.")
        append_log("-> SUCCESS: Stream complete.")

    except Exception as e:
        append_log(f"CRITICAL ERROR: {e}")
    finally:
        if os.getcwd() != "/": os.chdir("/")
        cleanup()