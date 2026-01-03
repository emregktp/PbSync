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
    """Ayarları yükler. Ayar yoksa None döner."""
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
        
        # Environment Variable'ları set et (Core.py ve subprocess'ler için)
        os.environ['RCLONE_CONFIG'] = RCLONE_CONFIG_PATH
        os.environ['PBS_PASSWORD'] = config.get("pbs_password", "")
        # Repository stringini oluştur: user@realm@host:datastore
        os.environ['PBS_REPOSITORY'] = f"{config['pbs_user']}@{config['pbs_host']}:{config['pbs_repo']}"
        
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
        
        # 1. JSON Config Kaydet
        config_data = {
            "pbs_host": pbs_host,
            "pbs_repo": pbs_repo,
            "pbs_user": pbs_user,
            "pbs_password": pbs_password
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)

        # 2. Rclone Config Kaydet
        with open(RCLONE_CONFIG_PATH, 'w') as f:
            f.write(rclone_conf)
        os.chmod(RCLONE_CONFIG_PATH, 0o600)

        return templates.TemplateResponse("setup.html", {
            "request": request, 
            "success_message": "Kurulum Başarılı! Yönlendiriliyorsunuz...",
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
        # Rclone config dosyasını env ile gösteriyoruz
        remotes_raw = subprocess.check_output("rclone listremotes", shell=True, env=os.environ).decode().strip()
        rclone_remotes = [line.strip() for line in remotes_raw.split('\n') if line]
    except Exception as e:
        rclone_remotes = [f"HATA: {str(e)}"]

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "remotes": rclone_remotes,
        "config": config
    })

@app.post("/scan-snapshots")
async def scan_snapshots(vmid: str = Form(...), config: dict = Depends(get_config)):
    if not config: return JSONResponse({"status": "error", "message": "Ayar Yok"}, 401)
    
    cmd = f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} | grep 'vm/{vmid}/' | awk '{{print $2}}' | sort -r"
    try:
        result = subprocess.check_output(cmd, shell=True, env=os.environ).decode().strip().split('\n')
        snapshots = [s for s in result if s]
        if not snapshots: return {"status": "error", "message": "Yedek bulunamadı."}
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
    if not config: return JSONResponse({"status": "error", "message": "Ayar Yok"}, 401)
    
    background_tasks.add_task(run_backup_process, snapshot, remote)
    return {"status": "started", "message": f"İşlem Başlatıldı: {snapshot} -> {remote}"}