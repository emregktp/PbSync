import os
import subprocess
import time

# Docker içinde bu pathler sabittir
MOUNT_POINT = "/mnt/pbsync_restore"
DRIVE_NAME = "drive-scsi0.img"
LOOP_DEV = "/dev/loop0"
LOG_FILE = "/app/data/pbsync.log"

def run_command(cmd, shell=False, env=None):
    """Komut çalıştırır ve loglar."""
    process_env = os.environ.copy()
    if env: process_env.update(env)
    
    print(f"CMD: {cmd}")
    try:
        return subprocess.run(cmd, shell=shell, check=True, capture_output=True, text=True, env=process_env)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: {e.stderr}")
        raise e

def cleanup():
    """Mount ve map işlemlerini temizler."""
    print("-> Temizlik yapılıyor...")
    subprocess.run(["umount", "-l", MOUNT_POINT], stderr=subprocess.DEVNULL)
    subprocess.run(["proxmox-backup-client", "unmap", LOOP_DEV], stderr=subprocess.DEVNULL)

def get_largest_partition():
    """Loop device içindeki en büyük partition'ı bulur."""
    try:
        cmd = f"lsblk -nr -o NAME,SIZE -b {LOOP_DEV} | grep 'loop0p' | sort -rn -k2 | head -n1 | cut -d' ' -f1"
        part = subprocess.check_output(cmd, shell=True).decode().strip()
        return f"/dev/{part}" if part else LOOP_DEV
    except:
        return LOOP_DEV

def run_backup_process(snapshot: str, remote: str):
    """Arka plan yedekleme süreci."""
    with open(LOG_FILE, "a") as log:
        log.write(f"\n--- NEW JOB: {snapshot} -> {remote} ({time.ctime()}) ---\n")
    
    repo = os.environ.get('PBS_REPOSITORY')
    
    try:
        cleanup()
        os.makedirs(MOUNT_POINT, exist_ok=True)
        
        # 1. Map
        print(f"Mapping {snapshot}...")
        run_command(["proxmox-backup-client", "map", snapshot, DRIVE_NAME, "--repository", repo])
        
        # 2. Mount
        target_part = get_largest_partition()
        print(f"Mounting {target_part}...")
        
        # Dirty FS hatasını önlemek için norecovery
        try:
            run_command(["mount", "-o", "ro,norecovery,noload", target_part, MOUNT_POINT])
        except:
            # Fallback
            run_command(["mount", "-o", "ro", target_part, MOUNT_POINT])
            
        # 3. Stream
        vmid = snapshot.split('/')[1]
        archive_name = f"{vmid}_{time.strftime('%Y%m%d_%H%M')}.tar.gz"
        remote_path = f"{remote}:{archive_name}"
        
        print("Streaming started...")
        
        # Pipe komutu: tar -> pigz -> rclone
        # Shell=True kullanarak pipe işlemini tek satırda hallediyoruz (Daha stabil)
        stream_cmd = (
            f"cd {MOUNT_POINT} && "
            f"tar cf - var etc home root opt 2>> {LOG_FILE} | "
            f"pigz -1 -p 4 | "
            f"rclone rcat {remote_path} -P --buffer-size 128M >> {LOG_FILE} 2>&1"
        )
        
        subprocess.run(stream_cmd, shell=True, check=True, executable='/bin/bash')
        
        print("SUCCESS.")
        
    except Exception as e:
        print(f"FAILURE: {e}")
        with open(LOG_FILE, "a") as log:
            log.write(f"CRITICAL ERROR: {e}\n")
    finally:
        cleanup()