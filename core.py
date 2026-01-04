import os
import subprocess
import time
import shutil
import glob
import json
import re

# --- Constants ---
DRIVE_NAME = "drive-scsi0.img"
# Host ve Docker'da aynı yol olmalı (docker-compose'da bind edildi)
MOUNT_POINT = "/mnt/pbsync_restore"
LOG_FILE_PATH = "/app/data/pbsync_stream.log"

def run_host_command(command, env=None):
    """
    Komutu Docker içinde değil, doğrudan HOST makinede çalıştırır (nsenter).
    """
    # Komut listesini string'e çevir
    cmd_str = ' '.join(command) if isinstance(command, list) else command
    print(f"HOST_EXEC: {cmd_str}")

    # Ortam değişkenlerini komutun başına ekle (Çünkü nsenter env aktarmaz)
    env_prefix = ""
    if env:
        for k, v in env.items():
            env_prefix += f"export {k}='{v}'; "
    
    # nsenter ile host namespace'ine girip komutu çalıştır
    # -t 1: Host PID 1 (init/systemd) namespace'ini hedefle
    full_cmd = f"nsenter -t 1 -m -u -n -i bash -c \"{env_prefix}{cmd_str}\""
    
    try:
        result = subprocess.run(
            full_cmd, shell=True, check=True, capture_output=True, text=True
        )
        if result.stdout: print(f"STDOUT: {result.stdout.strip()[:200]}...")
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR on HOST: {e.returncode}")
        print(f"STDERR: {e.stderr.strip()}")
        raise e

def run_local_command(command, env=None):
    """
    Komutu Docker konteyneri içinde çalıştırır (Örn: Rclone, Tar).
    """
    cmd_str = ' '.join(command) if isinstance(command, list) else command
    print(f"LOCAL_EXEC: {cmd_str}")
    try:
        current_env = os.environ.copy()
        if env: current_env.update(env)
        
        result = subprocess.run(
            command, shell=True if isinstance(command, str) else False, 
            check=True, capture_output=True, text=True, env=current_env
        )
        return result
    except subprocess.CalledProcessError as e:
        print(f"ERROR on LOCAL: {e.returncode}")
        print(f"STDERR: {e.stderr.strip()}")
        raise e

def cleanup():
    print("--- Starting Cleanup (On Host) ---")
    # Tüm temizlik işlemleri Host üzerinde yapılmalı
    try: run_host_command(f"umount -l {MOUNT_POINT}")
    except: pass
    try: run_host_command("vgchange -an")
    except: pass
    try: run_host_command("dmsetup remove_all")
    except: pass
    try: run_host_command(f"proxmox-backup-client unmap {DRIVE_NAME}")
    except: pass
    # Olası kilitli loop cihazlarını temizle
    try: run_host_command("losetup -D") 
    except: pass
    print("--- Cleanup Finished ---")

def find_loop_on_host():
    """
    Host üzerindeki loop cihazlarını tarar.
    """
    try:
        res = run_host_command("losetup -a")
        output = res.stdout.strip()
        for line in output.splitlines():
            if DRIVE_NAME in line or "proxmox" in line:
                return line.split(":")[0].strip()
    except: pass
    return None

def get_partitions_on_host(loop_dev):
    """
    Host üzerinde fdisk çalıştırarak partitionları bulur.
    """
    print(f"-> Scanning partitions on Host: {loop_dev}...")
    partitions = []
    try:
        res = run_host_command(f"fdisk -l -o Start,Sectors,Type,Size {loop_dev}")
        lines = res.stdout.splitlines()
        
        sector_size = 512
        for line in lines:
            if "Units:" in line and "bytes" in line:
                match = re.search(r'=\s*(\d+)\s*bytes', line)
                if match: sector_size = int(match.group(1))

        for line in lines:
            parts = line.split()
            if not parts or not parts[0].isdigit(): continue
            try:
                start_sector = int(parts[0])
                offset = start_sector * sector_size
                partitions.append(offset)
            except: continue
    except Exception as e:
        print(f"Fdisk error: {e}")
    return partitions

def try_mount_on_host(device):
    print(f"-> Host Mounting: {device}")
    # Klasör yoksa host üzerinde oluştur
    run_host_command(f"mkdir -p {MOUNT_POINT}")
    
    cmds = [
        f"mount -o ro {device} {MOUNT_POINT}",
        f"mount -t xfs -o ro,norecovery {device} {MOUNT_POINT}",
        f"ntfs-3g -o ro,remove_hiberfile {device} {MOUNT_POINT}",
        f"mount -t ext4 -o ro {device} {MOUNT_POINT}"
    ]
    
    for cmd in cmds:
        try:
            run_host_command(cmd)
            print(f"   SUCCESS! Mounted on Host.")
            return True
        except: continue
    return False

def list_files_in_snapshot(config: dict, snapshot: str, path: str = ""):
    print(f"\n--- Exploring Snapshot (HOST MODE) ---")
    current_env = os.environ.copy()
    
    # PBS Environment Variables for Host
    host_env = {
        'PBS_PASSWORD': config['pbs_password'],
        'PBS_REPOSITORY': config['pbs_repository_path']
    }
    if 'pbs_fingerprint' in config and config['pbs_fingerprint']:
        host_env['PBS_FINGERPRINT'] = config['pbs_fingerprint']

    try: cleanup() 
    except: pass

    try:
        # 1. MAP (Host Üzerinde)
        print("-> Mapping on Host...")
        map_cmd = f"proxmox-backup-client map {snapshot} {DRIVE_NAME} --repository {config['pbs_repository_path']}"
        run_host_command(map_cmd, env=host_env)
        time.sleep(2)

        # 2. Identify Loop
        active_loop = find_loop_on_host()
        if not active_loop:
            # Fallback: En son oluşturulan loop
            try:
                res = run_host_command("ls -t /dev/loop* | head -n 1")
                active_loop = res.stdout.strip()
            except: pass
        
        if not active_loop:
            return {"status": "error", "message": "Map failed or loop device not found on host."}
        
        print(f"-> Active Loop: {active_loop}")

        # 3. Activate (Host)
        try: run_host_command(f"kpartx -a -v -s {active_loop}")
        except: pass
        try: 
            run_host_command("vgscan --mknodes")
            run_host_command("vgchange -ay")
        except: pass
        time.sleep(1)

        # 4. Find Candidates & Mount (Host)
        # Mapper cihazlarını host üzerinde ara
        candidates = []
        try:
            res = run_host_command(f"ls /dev/mapper/loop*p*")
            candidates.extend(res.stdout.split())
        except: pass
        
        try:
            res = run_host_command(f"ls /dev/mapper/*-root") # LVM genelde root diye biter
            candidates.extend(res.stdout.split())
        except: pass

        # Fdisk Offset (Fallback)
        if not candidates:
            offsets = get_partitions_on_host(active_loop)
            for off in offsets:
                # Offset loop oluştur
                try:
                    res = run_host_command(f"losetup -f --show --offset {off} {active_loop}")
                    candidates.append(res.stdout.strip())
                except: pass
        
        # Raw Device ekle
        candidates.append(active_loop)

        mounted = False
        for dev in reversed(candidates):
            if try_mount_on_host(dev):
                mounted = True
                break
        
        if not mounted:
            return {"status": "error", "message": "Could not mount filesystem on Host."}

        # 5. List Files (Docker içinden okuyoruz, çünkü bind mount var)
        # Docker /mnt/pbsync_restore klasörünü görebiliyor
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
            return {"status": "error", "message": "Path not found (Mount seems empty?)"}

    except Exception as e:
        print(f"Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        cleanup()

def run_backup_process(config: dict, snapshot: str, remote: str, target_folder: str = "", source_paths: str = ""):
    print(f"\n--- Starting Stream (HOST MODE) ---")
    
    # PBS Env vars for Host
    host_env = {
        'PBS_PASSWORD': config['pbs_password'],
        'PBS_REPOSITORY': config['pbs_repository_path']
    }
    if 'pbs_fingerprint' in config and config['pbs_fingerprint']:
        host_env['PBS_FINGERPRINT'] = config['pbs_fingerprint']

    try:
        cleanup()
        
        # 1. MAP (Host)
        print("-> Mapping on Host...")
        run_host_command(f"proxmox-backup-client map {snapshot} {DRIVE_NAME} --repository {config['pbs_repository_path']}", env=host_env)
        time.sleep(2)

        active_loop = find_loop_on_host()
        if not active_loop:
            # Basit fallback
            res = run_host_command("losetup -a | tail -n 1") # Sonuncuyu al
            if res.stdout: active_loop = res.stdout.split(":")[0]
        
        if not active_loop: raise Exception("Loop device not found on host.")
        print(f"-> Active Loop: {active_loop}")

        # 2. Mount (Host)
        try: run_host_command(f"kpartx -a -v -s {active_loop}")
        except: pass
        try: 
            run_host_command("vgscan --mknodes")
            run_host_command("vgchange -ay")
        except: pass
        
        candidates = []
        try: 
            res = run_host_command("ls /dev/mapper/loop*p*")
            candidates.extend(res.stdout.split())
        except: pass
        
        if not candidates:
            offsets = get_partitions_on_host(active_loop)
            for off in offsets:
                try:
                    res = run_host_command(f"losetup -f --show --offset {off} {active_loop}")
                    candidates.append(res.stdout.strip())
                except: pass
        candidates.append(active_loop)

        mounted = False
        for dev in reversed(candidates):
            if try_mount_on_host(dev):
                mounted = True
                break
        
        if not mounted: raise Exception("Mount failed on Host.")

        # 3. Stream (Docker - LOCAL)
        # Dosyalar artık /mnt/pbsync_restore altında ve Docker bunu görüyor.
        # Tar ve Rclone Docker içinde çalışmaya devam edebilir!
        
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

        # Local execution (Docker Container)
        tar_cmd = ["tar", "cf", "-"] + dirs
        pigz_cmd = ["pigz", "-1"]
        rclone_cmd = ["rclone", "rcat", full_remote_path, "-P", "--buffer-size", "128M"]

        print("-> Streaming from Docker...")
        with open(LOG_FILE_PATH, 'a') as log_file:
            current_env = os.environ.copy()
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
        if os.getcwd() != "/": os.chdir("/")
        cleanup()