# Adım 1: Hazırlık
FROM python:3.11-slim-bookworm AS base
ENV DEBIAN_FRONTEND=noninteractive

# Repo anahtarları ve gerekli araçlar
RUN apt-get update && apt-get install -y wget gnupg lsb-release && \
    wget https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg -O /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg && \
    echo "deb http://download.proxmox.com/debian/pbs-client bookworm main" > /etc/apt/sources.list.d/pbs-client.list

# Adım 2: Ana İmaj
FROM python:3.11-slim-bookworm

# Anahtarları kopyala
COPY --from=base /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg /etc/apt/trusted.gpg.d/
COPY --from=base /etc/apt/sources.list.d/pbs-client.list /etc/apt/sources.list.d/

# Paket kurulumu (fuse3, ntfs-3g, kpartx vb.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    proxmox-backup-client \
    rclone \
    pigz \
    fuse3 \
    ntfs-3g \
    xfsprogs \
    lvm2 \
    kpartx \
    fdisk \
    dmsetup \
    file \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]