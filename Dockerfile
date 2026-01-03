FROM python:3.9-slim-bullseye AS base
ENV DEBIAN_FRONTEND=noninteractive

# Proxmox Reposunu ekle ve araçları kur
RUN apt-get update && apt-get install -y wget gnupg lsb-release && \
    echo "deb http://download.proxmox.com/debian/pbs-client $(lsb_release -sc) main" > /etc/apt/sources.list.d/pbs-client.list && \
    wget https://enterprise.proxmox.com/debian/proxmox-release-bullseye.gpg -O /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg && \
    chmod 644 /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg

RUN apt-get update && apt-get install -y --no-install-recommends \
    proxmox-backup-client rclone pigz fuse3 && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

FROM python:3.9-slim-bullseye
COPY --from=base /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg /etc/apt/trusted.gpg.d/
COPY --from=base /etc/apt/sources.list.d/pbs-client.list /etc/apt/sources.list.d/
COPY --from=base /usr/bin/proxmox-backup-client /usr/bin/
COPY --from=base /usr/bin/rclone /usr/bin/
COPY --from=base /usr/bin/pigz /usr/bin/
# Fuse kütüphaneleri önemli
COPY --from=base /usr/lib/x86_64-linux-gnu/libfuse3.so.3 /usr/lib/x86_64-linux-gnu/

RUN apt-get update && apt-get install -y --no-install-recommends fuse3 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]