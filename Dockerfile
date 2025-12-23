# ==============================================================================
# PbSync Dockerfile
# ==============================================================================
# Bu Dockerfile, PbSync uygulamasını ve tüm bağımlılıklarını içeren bir
# Docker imajı oluşturur.
#
# ÖNEMLİ GÜVENLİK NOTU:
# Bu imajın düzgün çalışabilmesi için --privileged veya --cap-add=SYS_ADMIN
# ve --device=/dev/fuse gibi yetkilerle çalıştırılması gerekir. Bu, 
# container'a geniş sistem yetkileri verir. Lütfen bu imajı güvendiğiniz
# bir ortamda çalıştırın.
# ==============================================================================

# Adım 1: Temel imaj ve bağımlılıkların kurulumu
FROM python:3.9-slim-bullseye AS base

# APT'nin interaktif diyaloglar sormasını engelle
ENV DEBIAN_FRONTEND=noninteractive

# Gerekli sistem araçlarını ve Proxmox repo anahtarını almak için wget'i kur
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    lsb-release \
    # Temizlik
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Proxmox Backup Client deposunu ekle
# Not: Proxmox resmi olarak 'bookworm' (Debian 12) için anahtar sunuyor, 
# 'bullseye' (Debian 11) ile de genellikle uyumlu çalışır.
RUN echo "deb http://download.proxmox.com/debian/pbs-client $(lsb_release -sc) main" > /etc/apt/sources.list.d/pbs-client.list && \
    wget https://enterprise.proxmox.com/debian/proxmox-release-bullseye.gpg -O /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg && \
    # GPG anahtarının izinlerini ayarla
    chmod 644 /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg

# Uygulama bağımlılıklarını kur
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    proxmox-backup-client \
    rclone \
    pigz \
    # Temizlik
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Adım 2: Python ortamının hazırlanması
FROM python:3.9-slim-bullseye

# Önceki adımdan kopyalanan APT kaynak listelerini ve anahtarları temizle
# ve sadece gerekli olanları yeniden kur. Bu imaj boyutunu küçültür.
COPY --from=base /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg /etc/apt/trusted.gpg.d/proxmox-release-bullseye.gpg
COPY --from=base /etc/apt/sources.list.d/pbs-client.list /etc/apt/sources.list.d/pbs-client.list
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    proxmox-backup-client \
    rclone \
    pigz \
    fuse3 \
    # Temizlik
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Çalışma dizini oluştur
WORKDIR /app

# Python bağımlılıklarını kur (önbellekleme için önce sadece requirements.txt)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Uygulama kodunu kopyala
COPY . .

# Web sunucusu için port'u dışarı aç
EXPOSE 8000

# Container başladığında çalıştırılacak komut
# Not: PBS_PASSWORD gibi hassas veriler docker-compose.yml'deki 'environment'
# bölümünden veya Docker secrets'tan sağlanmalıdır.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]