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
        
        os.environ['RCLONE_CONFIG'] = RCLONE_CONFIG_PATH
        os.environ['PBS_PASSWORD'] = config.get("pbs_password", "")
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
        remotes_raw = subprocess.check_output("rclone listremotes", shell=True, env=os.environ).decode().strip()
        rclone_remotes = [line.strip() for line in remotes_raw.split('\n') if line]
    except Exception as e:
        rclone_remotes = [f"ERROR: {str(e)}"]

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "remotes": rclone_remotes,
        "config": config
    })

# --- YENİ EKLENEN FONKSİYON: TÜM VM'LERİ TARA ---
@app.post("/scan-vms")
async def scan_vms(config: dict = Depends(get_config)):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    
    # Tüm snapshotları listele
    cmd = f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} | awk '{{print $2}}'"
    try:
        result = subprocess.check_output(cmd, shell=True, env=os.environ).decode().strip().split('\n')
        # Sadece "vm/" veya "ct/" ile başlayanları al ve grupla
        vms = set()
        for line in result:
            if line.startswith("vm/") or line.startswith("ct/"):
                # Örn: vm/100/2023... -> vm/100
                parts = line.split('/')
                if len(parts) >= 2:
                    vms.add(f"{parts[0]}/{parts[1]}") # vm/100
        
        sorted_vms = sorted(list(vms))
        if not sorted_vms: return {"status": "error", "message": "No VMs found in repository."}
        return {"status": "success", "vms": sorted_vms}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/scan-snapshots")
async def scan_snapshots(vmid: str = Form(...), config: dict = Depends(get_config)):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    
    # Gelen vmid "vm/100" formatındaysa sadece ID'yi almayalım, direkt filtreleyelim
    # Kullanıcı "100" yazarsa da destekleyelim
    filter_str = vmid if "/" in vmid else f"vm/{vmid}"
    
    cmd = f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} | grep '{filter_str}' | awk '{{print $2}}' | sort -r"
    try:
        result = subprocess.check_output(cmd, shell=True, env=os.environ).decode().strip().split('\n')
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