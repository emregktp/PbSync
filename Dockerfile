# Adım 1: Hazırlık (Repo anahtarlarını almak için)
FROM python:3.9-slim-bullseye AS base
ENV DEBIAN_FRONTEND=noninteractive

# Gerekli araçları kur ve Proxmox repo anahtarlarını indir
RUN apt-get update && apt-get install -y wget gnupg lsb-release && \
    echo "deb http://download.proxmox.com/debian/pbs-client $(lsb_release -sc) main" > /etc/apt/sources.list.d/pbs-client.list && \
    wget https://enterprise.proxmox.com/debian/proxmox-release-bullseye.gpg -O /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg && \
    chmod 644 /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg

# Adım 2: Ana İmajın Oluşturulması
FROM python:3.9-slim-bullseye

# Repo listesini ve GPG anahtarını önceki aşamadan kopyala
COPY --from=base /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg /etc/apt/trusted.gpg.d/
COPY --from=base /etc/apt/sources.list.d/pbs-client.list /etc/apt/sources.list.d/

# Paketleri kur (fuse3, proxmox-client, rclone, pigz ve dosya sistemi araçları)
# EKLENENLER: ntfs-3g, xfsprogs, lvm2, kpartx, fdisk
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
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Çalışma dizini ve Python bağımlılıkları
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodlarını kopyala
COPY . .

# Portu aç ve başlat
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]