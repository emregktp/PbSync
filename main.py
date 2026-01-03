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
    except Exception as e:
        print(f"CONFIG LOAD ERROR: {e}")
        # Hata durumunda boş dön ki setup'a yönlendirsin
        return None

    # Environment Variable'ları set et
    os.environ['RCLONE_CONFIG'] = RCLONE_CONFIG_PATH
    
    pbs_user = config.get('pbs_user', 'root@pam')
    pbs_host = config.get('pbs_host', 'localhost')
    pbs_repo = config.get('pbs_repo', 'backup')
    pbs_pass = config.get('pbs_password', '')
    pbs_fingerprint = config.get('pbs_fingerprint', '') # Yeni eklenen alan

    os.environ['PBS_PASSWORD'] = pbs_pass
    
    # Fingerprint varsa environment'a ekle (SSL hatasını çözer)
    if pbs_fingerprint and pbs_fingerprint.strip():
        os.environ['PBS_FINGERPRINT'] = pbs_fingerprint.strip()
    
    repo = f"{pbs_user}@{pbs_host}:{pbs_repo}"
    os.environ['PBS_REPOSITORY'] = repo
    config['pbs_repository_path'] = repo
    
    return config

# --- SETUP ENDPOINTS ---
@app.get("/setup", response_class=HTMLResponse)
async def get_setup_page(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request})

@app.post("/setup")
async def handle_setup_form(
    pbs_host: str = Form(...),
    pbs_repo: str = Form(...),
    pbs_user: str = Form(...),
    pbs_password: str = Form(...),
    pbs_fingerprint: str = Form(None), # Opsiyonel yeni alan
    rclone_conf: str = Form(...)
):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        
        config_data = {
            "pbs_host": pbs_host.strip(),
            "pbs_repo": pbs_repo.strip(),
            "pbs_user": pbs_user.strip(),
            "pbs_password": pbs_password.strip(),
            "pbs_fingerprint": pbs_fingerprint.strip() if pbs_fingerprint else ""
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)

        with open(RCLONE_CONFIG_PATH, 'w') as f:
            f.write(rclone_conf.strip())
        os.chmod(RCLONE_CONFIG_PATH, 0o600)

        return RedirectResponse(url="/", status_code=303)
        
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

# --- DASHBOARD ENDPOINTS ---
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

# --- STATUS CHECK ---
@app.get("/check-status")
async def check_status(config: dict = Depends(get_config)):
    if not config: 
        return {
            "pbs": {"status": False, "msg": "No Config"}, 
            "rclone": {"status": False, "msg": "No Config"}
        }
    
    response = {}
    
    # 1. PBS Kontrolü
    try:
        subprocess.run(
            f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} --output-format json-pretty", 
            shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=os.environ
        )
        response["pbs"] = {"status": True, "msg": "Connected"}
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode().strip() or "Connection Failed"
        # SSL Hatasını yakala ve kullanıcı dostu mesaj ver
        if "fingerprint" in err_msg.lower():
            err_msg = "SSL Error: Fingerprint mismatch or missing!"
        response["pbs"] = {"status": False, "msg": err_msg}
    except Exception as e:
        response["pbs"] = {"status": False, "msg": str(e)}

    # 2. Rclone Kontrolü
    try:
        subprocess.run("rclone listremotes", shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=os.environ)
        response["rclone"] = {"status": True, "msg": "Ready"}
    except subprocess.CalledProcessError as e:
        response["rclone"] = {"status": False, "msg": "Config Error"}
    except Exception as e:
        response["rclone"] = {"status": False, "msg": str(e)}
        
    return response

# --- VM SCAN ---
@app.post("/scan-vms")
async def scan_vms(config: dict = Depends(get_config)):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    
    cmd = f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} --output-format json-pretty"
    try:
        output = subprocess.check_output(cmd, shell=True, env=os.environ).decode().strip()
        data = json.loads(output)
        
        vms = set()
        for item in data:
            if 'backup-type' in item and 'backup-id' in item:
                vms.add(f"{item['backup-type']}/{item['backup-id']}")
        
        sorted_vms = sorted(list(vms))
        if not sorted_vms: return {"status": "error", "message": "No backups found in repository."}
        return {"status": "success", "vms": sorted_vms}
        
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"PBS Error: {e.output.decode() if e.output else 'Check Logs'}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/scan-snapshots")
async def scan_snapshots(vmid: str = Form(...), config: dict = Depends(get_config)):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    
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
    background_tasks.add_task(run_backup_process, config, snapshot, remote)
    return {"status": "started", "message": f"Stream Started: {snapshot} -> {remote}"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)