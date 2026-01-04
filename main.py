import os
import json
import subprocess
from fastapi import FastAPI, Form, Request, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn
from core import run_backup_process, list_files_or_partitions

# --- AYARLAR ---
CONFIG_DIR = "/app/data"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
RCLONE_CONFIG_PATH = os.path.join(CONFIG_DIR, "rclone.conf")

app = FastAPI(title="PbSync")
templates = Jinja2Templates(directory="templates")

def get_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
    except Exception as e:
        return None

    os.environ['RCLONE_CONFIG'] = RCLONE_CONFIG_PATH
    
    pbs_user = config.get('pbs_user', 'root@pam')
    pbs_host = config.get('pbs_host', 'localhost')
    pbs_repo = config.get('pbs_repo', 'backup')
    pbs_pass = config.get('pbs_password', '')
    pbs_fingerprint = config.get('pbs_fingerprint', '')

    os.environ['PBS_PASSWORD'] = pbs_pass
    if pbs_fingerprint and pbs_fingerprint.strip():
        os.environ['PBS_FINGERPRINT'] = pbs_fingerprint.strip()
    
    repo = f"{pbs_user}@{pbs_host}:{pbs_repo}"
    os.environ['PBS_REPOSITORY'] = repo
    config['pbs_repository_path'] = repo
    return config

@app.get("/setup", response_class=HTMLResponse)
async def get_setup_page(request: Request):
    return templates.TemplateResponse("setup.html", {"request": request})

@app.post("/setup")
async def handle_setup_form(
    pbs_host: str = Form(...),
    pbs_repo: str = Form(...),
    pbs_user: str = Form(...),
    pbs_password: str = Form(...),
    pbs_fingerprint: str = Form(None),
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

@app.get("/check-status")
async def check_status(config: dict = Depends(get_config)):
    if not config: 
        return {"pbs": {"status": False, "msg": "No Config"}, "rclone": {"status": False, "msg": "No Config"}}
    response = {}
    try:
        subprocess.run(
            f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} --output-format json-pretty", 
            shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=os.environ
        )
        response["pbs"] = {"status": True, "msg": "Connected"}
    except Exception as e:
        response["pbs"] = {"status": False, "msg": str(e)}
    try:
        subprocess.run("rclone listremotes", shell=True, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=os.environ)
        response["rclone"] = {"status": True, "msg": "Ready"}
    except Exception as e:
        response["rclone"] = {"status": False, "msg": str(e)}
    return response

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
        return {"status": "success", "vms": sorted(list(vms))}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/scan-snapshots")
async def scan_snapshots(vmid: str = Form(...), config: dict = Depends(get_config)):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    filter_str = vmid if "/" in vmid else f"vm/{vmid}"
    cmd = f"proxmox-backup-client snapshot list --repository {os.environ['PBS_REPOSITORY']} | grep '{filter_str}' | awk '{{print $2}}' | sort -r"
    try:
        result = subprocess.check_output(cmd, shell=True, env=os.environ).decode().strip().split('\n')
        return {"status": "success", "snapshots": [s for s in result if s]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# --- EXPLORER (UPDATED) ---
@app.post("/explore")
async def explore_snapshot(
    snapshot: str = Form(...), 
    path: str = Form(""),
    partition_id: str = Form(None), # YENİ: Kullanıcının seçtiği partition indexi
    config: dict = Depends(get_config)
):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    
    # Core fonksiyon artık hem partition listelemeyi hem dosya listelemeyi yönetiyor
    result = list_files_or_partitions(config, snapshot, partition_id, path)
    return result

@app.post("/start-stream")
async def start_stream(
    background_tasks: BackgroundTasks, 
    snapshot: str = Form(...), 
    remote: str = Form(...),
    target_folder: str = Form(""),
    source_paths: str = Form(""), 
    config: dict = Depends(get_config)
):
    if not config: return JSONResponse({"status": "error", "message": "No Config"}, 401)
    background_tasks.add_task(run_backup_process, config, snapshot, remote, target_folder, source_paths)
    return {"status": "started", "message": f"Stream Started: {snapshot} -> {remote}"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)