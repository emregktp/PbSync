import os
import json
import subprocess
from fastapi import FastAPI, Form, Request, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
from core import run_backup_process

# --- AYARLAR ---
CONFIG_DIR = "/app/data"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
RCLONE_CONFIG_PATH = os.path.join(CONFIG_DIR, "rclone.conf")

app = FastAPI(title="PbSync")
templates = Jinja2Templates(directory="templates")

# --- KONFIGURASYON YÖNETİMİ ---
def get_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        
        # Environment Variable'ları set et
        os.environ['RCLONE_CONFIG'] = RCLONE_CONFIG_PATH
        os.environ['PBS_PASSWORD'] = config.get("pbs_password", "")
        
        # Repository stringini oluştur (user@realm@host:datastore)
        # Eğer kullanıcı zaten tam format girdiyse bozmayalım, ama genelde user@realm ayrıdır.
        # Basitlik için config'deki hazır path'i kullanıyoruz.
        repo = f"{config['pbs_user']}@{config['pbs_host']}:{config['pbs_repo']}"
        os.environ['PBS_REPOSITORY'] = repo
        config['pbs_repository_path'] = repo # Template için
        
        return config
    except:
        return None

# --- SETUP SAYFALARI ---
@app.get("/setup", response_class=HTMLResponse)
async def get_setup_page(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request})

@app.post("/setup")
async def handle_setup_form(
    request: Request,
    pbs_host: str = Form(...),
    pbs_repo: str = Form(...),
    pbs_user: str = Form(...),
    pbs_password: str = Form(...),
    rclone_conf: str = Form(...)
):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        
        config_data = {
            "pbs_host": pbs_host,
            "pbs_repo": pbs_repo,
            "pbs_user": pbs_user,
            "pbs_password": pbs_password
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)

        with open(RCLONE_CONFIG_PATH, 'w') as f:
            f.write(rclone_conf)
        os.chmod(RCLONE_CONFIG_PATH, 0o600)

        return templates.TemplateResponse("setup.html", {
            "request": request, 
            "success_message": "Configuration Saved Successfully! Redirecting...",
            "redirect": True
        })
    except Exception as e:
        return templates.TemplateResponse("setup.html", {"request": request, "error_message": str(e)})

# --- ANA UYGULAMA ---
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, config: dict = Depends(get_config)):
    if config is None:
        return RedirectResponse(url="/setup")

    rclone_remotes = []
    try:
        # Rclone remote listesini al
        remotes_raw = subprocess.check_output("rclone listremotes", shell=True, env=os.environ).decode().strip()
        rclone_remotes = [line.strip() for line in remotes_raw.split('\n') if line]
    except Exception as e:
        rclone_remotes = [f"ERROR: {str(e)}"]

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "remotes": rclone_remotes,
        "config": config
    })

# --- DURUM KONTROLÜ (YENİ) ---
@app.get("/check-status")
async def check_status(config: dict = Depends(get_config)):
    if not config: return {"pbs": False, "rclone": False}
    
    status = {"pbs": False, "rclone": False}
    
    # 1. PBS Kontrolü (Hızlıca versiyon sorarak veya snapshot listesi isteyerek)
    try:
        # Repository bağlantısını test etmek için basit bir komut
        subprocess.run(
            f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} --output-format json-pretty", 
            shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ
        )
        status["pbs"] = True
    except:
        status["pbs"] = False
        
    # 2. Rclone Kontrolü
    try:
        subprocess.run("rclone listremotes", shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ)
        status["rclone"] = True
    except:
        status["rclone"] = False
        
    return status

# --- VM TARAMA (GÜNCELLENDİ: JSON FORMATI) ---
@app.post("/scan-vms")
async def scan_vms(config: dict = Depends(get_config)):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    
    # JSON çıktısı alarak parsing hatasını önlüyoruz
    cmd = f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} --output-format json-pretty"
    
    try:
        output = subprocess.check_output(cmd, shell=True, env=os.environ).decode().strip()
        data = json.loads(output)
        
        vms = set()
        for item in data:
            # item['backup-id'] -> "100"
            # item['backup-type'] -> "vm" veya "ct"
            if 'backup-type' in item and 'backup-id' in item:
                # Format: vm/100
                vms.add(f"{item['backup-type']}/{item['backup-id']}")
        
        sorted_vms = sorted(list(vms))
        if not sorted_vms: return {"status": "error", "message": "No VMs found (List is empty)."}
        return {"status": "success", "vms": sorted_vms}
        
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"PBS Connection Failed: {e}"}
    except json.JSONDecodeError:
        return {"status": "error", "message": "Invalid JSON response from PBS Client."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/scan-snapshots")
async def scan_snapshots(vmid: str = Form(...), config: dict = Depends(get_config)):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    
    # JSON ile snapshotları çek
    cmd = f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} --output-format json-pretty"
    try:
        output = subprocess.check_output(cmd, shell=True, env=os.environ).decode().strip()
        data = json.loads(output)
        
        snapshots = []
        # vmid formatı "vm/100" veya sadece "100" olabilir.
        target_id = vmid.split('/')[-1] # "100"
        target_type = vmid.split('/')[0] if '/' in vmid else None # "vm"
        
        for item in data:
            if str(item.get('backup-id')) == target_id:
                if target_type and item.get('backup-type') != target_type:
                    continue
                # Snapshot path oluştur: vm/100/2023-01-01T12:00:00Z
                snap_path = f"{item['backup-type']}/{item['backup-id']}/{item['backup-time-string']}" if 'backup-time-string' in item else None
                # Alternatif: PBS bazen backup-time epoch döner, json çıktısına göre değişebilir.
                # json-pretty çıktısında genelde "backup-time" (epoch) olur.
                # Biz listeleme komutunun ham string çıktısını kullanmak yerine,
                # güvenli olsun diye tekrar basit listelemeye dönebiliriz ya da
                # client'ın anladığı formatı oluşturabiliriz. 
                # En güvenlisi, client'ın beklediği formatı üretmektir.
                # Ancak json çıktısında "path" alanı olmayabilir.
                # Basitlik için burada filtreleme yapıp standart list komutunu kullanalım:
                pass 

        # YUKARIDAKİ JSON MANTIĞI SNAPSHOT İÇİN KARMAŞIK OLABİLİR (Time conversion vs).
        # Snapshot listesi için GREP yöntemi daha güvenli çünkü ID'yi zaten biliyoruz.
        # Sadece VM ID'yi bulurken JSON kullandık.
        
        filter_str = vmid if "/" in vmid else f"vm/{vmid}"
        cmd_grep = f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} | grep '{filter_str}' | awk '{{print $2}}' | sort -r"
        result = subprocess.check_output(cmd_grep, shell=True, env=os.environ).decode().strip().split('\n')
        snapshots = [s for s in result if s]
        
        if not snapshots: return {"status": "error", "message": "No snapshots found."}
        return {"status": "success", "snapshots": snapshots}

    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/start-stream")
async def start_stream(
    background_tasks: BackgroundTasks, 
    snapshot: str = Form(...), 
    remote: str = Form(...),
    config: dict = Depends(get_config)
):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    
    background_tasks.add_task(run_backup_process, snapshot, remote)
    return {"status": "started", "message": f"Stream Started: {snapshot} -> {remote}"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)