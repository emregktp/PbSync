import os
import json
import subprocess
from fastapi import FastAPI, Form, Request, BackgroundTasks, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# Bizim modüllerimiz
from core import run_backup_process

# --- Configuration Paths ---
# We expect the data volume to be mounted at /app/data in the Docker container.
CONFIG_DIR = "/app/data"
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
RCLONE_CONFIG_PATH = os.path.join(CONFIG_DIR, "rclone.conf")

app = FastAPI(title="PbSync")
templates = Jinja2Templates(directory="templates")

# --- Configuration Handling ---

def get_config():
    """Dependency to load config. Redirects to setup if not configured."""
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    
    # Set the environment variable for rclone to find our config
    os.environ['RCLONE_CONFIG'] = RCLONE_CONFIG_PATH
    # Set the environment variable for proxmox-backup-client password
    os.environ['PBS_PASSWORD'] = config.get("pbs_password", "")
    
    return config

# --- Setup Endpoints ---

@app.get("/setup", response_class=HTMLResponse)
async def get_setup_page(request: Request):
    """Displays the initial setup page."""
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
    """Saves the configuration from the setup form."""
    error_message = None
    try:
        # Create the data directory if it doesn't exist
        os.makedirs(CONFIG_DIR, exist_ok=True)

        # Save PBS details to config.json
        config_data = {
            "pbs_host": pbs_host,
            "pbs_repo": pbs_repo,
            "pbs_user": pbs_user,
            "pbs_password": pbs_password,
            "pbs_repository_path": f"{pbs_user}@{pbs_host}:{pbs_repo}"
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)

        # Save rclone content to rclone.conf
        with open(RCLONE_CONFIG_PATH, 'w') as f:
            f.write(rclone_conf)
        
        # Set restrictive permissions on the rclone config file
        os.chmod(RCLONE_CONFIG_PATH, 0o600)

    except Exception as e:
        error_message = f"Ayarlar kaydedilirken bir hata oluştu: {e}"
        return templates.TemplateResponse("setup.html", {"request": request, "error_message": error_message})

    return templates.TemplateResponse("setup.html", {"request": request, "success_message": "Ayarlar başarıyla kaydedildi! Uygulama şimdi hazır."})

# --- Main Application Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, config: dict = Depends(get_config)):
    """
    Main page. If not configured, redirects to setup. Otherwise, lists rclone remotes.
    """
    if config is None:
        return RedirectResponse(url="/setup")

    rclone_remotes = []
    try:
        remotes_raw = subprocess.check_output("rclone listremotes", shell=True, env=os.environ).decode().strip()
        rclone_remotes = [line.replace(":", "") for line in remotes_raw.split('\n') if line]
    except Exception as e:
        rclone_remotes = [f"HATA: rclone hedefleri okunamadı: {e}"]
        
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "remotes": rclone_remotes,
        "config": config
    })

@app.post("/scan-snapshots", response_class=JSONResponse)
async def scan_snapshots(vmid: str = Form(...), config: dict = Depends(get_config)):
    """Scans for snapshots for a given VM ID using the saved configuration."""
    if config is None:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Uygulama yapılandırılmamış."})
    if not vmid.isdigit():
        return JSONResponse(status_code=400, content={"status": "error", "message": "Geçersiz VM ID formatı."})

    cmd = f"proxmox-backup-client snapshot list --repository {config['pbs_repository_path']} | grep 'vm/{vmid}/' | awk '{{print $2}}' | sort -r"
    try:
        # Pass the environment with PBS_PASSWORD to the subprocess
        result = subprocess.check_output(cmd, shell=True, stderr=subprocess.PIPE, env=os.environ).decode().strip().split('\n')
        snapshots = [snap for snap in result if snap]
        if not snapshots:
            return {"status": "error", "message": f"VM ID {vmid} için yedek bulunamadı."}
        return {"status": "success", "snapshots": snapshots}
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"Yedekler taranırken hata: {e.stderr.decode()}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/start-stream", response_class=JSONResponse)
async def start_stream(
    background_tasks: BackgroundTasks, 
    snapshot: str = Form(...), 
    remote: str = Form(...),
    config: dict = Depends(get_config)
):
    """Starts the background task to stream the selected backup."""
    if config is None:
        return JSONResponse(status_code=401, content={"status": "error", "message": "Uygulama yapılandırılmamış."})

    background_tasks.add_task(run_backup_process, config, snapshot, remote)
    
    print(f"Arka plan görevi başlatıldı: {snapshot} -> {remote}")
    return {"status": "started", "message": f"Stream işlemi başlatıldı: '{snapshot}' -> '{remote}'. Logları sunucu konsolundan takip edebilirsiniz."}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)