#!/bin/bash

# ==============================================================================
# PbSync Kurulum Script'i
# ==============================================================================
# Bu script, PbSync'i ve bağımlılıklarını sisteme kurar.
# Kök (root) yetkileriyle çalıştırılması gerekmektedir.
# ==============================================================================

# --- Ayarlar ---
# DEĞİŞTİR: Proje GitHub'a yüklendiğinde bu adresi güncelleyin.
REPO_URL="https://github.com/KULLANICI_ADINIZ/PbSync.git" 
INSTALL_DIR="/opt/pbsync"
BIN_DIR="/usr/local/bin"
CONFIG_DIR_TEMPLATE="$HOME/.config/pbsync"

# Script'in gerçek kullanıcının ev dizinini bulması için (sudo ile çalıştırıldığında $HOME root olur)
REAL_HOME=$(eval echo ~$SUDO_USER)
CONFIG_DIR=$(eval echo $CONFIG_DIR_TEMPLATE)


# --- Renkler ---
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

# --- Fonksiyonlar ---

# Hata durumunda çıkış yap
handle_error() {
    echo -e "\n${RED}HATA: $1. Kurulum iptal edildi.${NC}"
    exit 1
}

# Kök yetkisi kontrolü
check_root() {
    if [ "$EUID" -ne 0 ]; then
        handle_error "Bu script'in kök (root) yetkileriyle çalıştırılması gerekiyor. Lütfen 'sudo ./install.sh' komutunu kullanın."
    fi
}

# Bağımlılık kontrolü
check_deps() {
    echo -e "${BLUE}-> Bağımlılıklar kontrol ediliyor...${NC}"
    local missing_deps=()
    # proxmox-backup-client kontrol dışı bırakıldı, çünkü bu zaten bir Proxmox Backup sunucusuyla
    # entegre olacak bir sistemde manuel olarak kurulmalıdır.
    local deps=("git" "python3" "python3-venv" "rclone" "pigz")
    
    for cmd in "${deps[@]}"; do
        if ! command -v $cmd &> /dev/null; then
            missing_deps+=("$cmd")
        fi
    done

    if [ ${#missing_deps[@]} -ne 0 ]; then
        echo -e "${YELLOW}UYARI: Lütfen önce aşağıdaki eksik paketleri kurun:${NC}"
        for dep in "${missing_deps[@]}"; do echo " - $dep"; done
        # Örnek kurulum komutu
        echo -e "\nÖrnek Debian/Ubuntu komutu:"
        echo -e "sudo apt-get update && sudo apt-get install -y ${missing_deps[*]}"
        handle_error "Eksik bağımlılıklar bulundu."
    fi
    echo -e "${GREEN}Tüm temel bağımlılıklar mevcut.${NC}"
}


# --- Ana Kurulum Akışı ---

echo -e "${BLUE}#####################################${NC}"
echo -e "${BLUE}#      PbSync Kurulum Sihirbazı      #${NC}"
echo -e "${BLUE}#####################################${NC}"

check_root
check_deps

# 1. Mevcut kurulumu temizle (varsa)
if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}- Mevcut kurulum bulundu. Kaldırılıyor...${NC}"
    rm -rf "$INSTALL_DIR"
    rm -f "$BIN_DIR/pbsync"
fi

# 2. Projeyi GitHub'dan indir
echo -e "\n${BLUE}-> Proje dosyaları indiriliyor... (${REPO_URL})${NC}"
git clone --depth 1 "$REPO_URL" "$INSTALL_DIR/app" || handle_error "Proje deposu indirilemedi. URL doğru mu?"

# 3. Sanal ortam (venv) oluştur ve kütüphaneleri kur
echo -e "${BLUE}-> Python sanal ortamı hazırlanıyor...${NC}"
python3 -m venv "$INSTALL_DIR/venv" || handle_error "Python sanal ortamı oluşturulamadı."
source "$INSTALL_DIR/venv/bin/activate"
pip install --upgrade pip &> /dev/null
pip install -r "$INSTALL_DIR/app/requirements.txt" || handle_error "Python kütüphaneleri kurulamadı."
deactivate

# 4. Yapılandırma klasörü ve dosyası oluştur
echo -e "${BLUE}-> Yapılandırma dosyaları oluşturuluyor... (${CONFIG_DIR})${NC}"
mkdir -p "$CONFIG_DIR" || handle_error "Yapılandırma dizini oluşturulamadı."
# Eğer kullanıcı zaten bir ayar dosyası oluşturduysa üzerine yazma
if [ ! -f "$CONFIG_DIR/pbsync.conf" ]; then
    cp "$INSTALL_DIR/app/pbsync.example.conf" "$CONFIG_DIR/pbsync.conf"
    # Dosyanın sahibi olarak script'i çalıştıran kullanıcıyı ata
    chown -R $SUDO_USER:$SUDO_USER "$REAL_HOME/.config"
    echo -e "${YELLOW}ÖNEMLİ: Lütfen '$CONFIG_DIR/pbsync.conf' dosyasını kendi ayarlarınızla düzenleyin.${NC}"
else
    echo -e "${GREEN}- Mevcut 'pbsync.conf' dosyası korundu.${NC}"
fi

# 5. Çalıştırılabilir script'i oluştur
echo -e "${BLUE}-> 'pbsync' komutu sisteme ekleniyor...${NC}"
cat << EOF > "$BIN_DIR/pbsync"
#!/bin/bash
# PbSync Başlatıcı

# Gerekli işlemler root yetkisiyle çalışmalı
if [ "\$EUID" -ne 0 ]; then
    echo "Lütfen 'sudo pbsync' olarak çalıştırın."
    exit 1
fi

export PYTHONUNBUFFERED=1
cd "$INSTALL_DIR/app"
"$INSTALL_DIR/venv/bin/python3" main.py "\$@"
EOF

chmod +x "$BIN_DIR/pbsync" || handle_error "'pbsync' komutu çalıştırılabilir yapılamadı."

# 6. Bitiş
echo -e "\n${GREEN}✔ Kurulum başarıyla tamamlandı!${NC}"
echo -e "Uygulamayı çalıştırmak için terminale aşağıdaki komutu yazmanız yeterli:"
echo -e "\n  ${YELLOW}sudo pbsync${NC}\n"
echo -e "Tarayıcınızda ${BLUE}http://127.0.0.1:8000${NC} adresini açarak arayüze erişebilirsiniz."
echo -e "Kurulumu kaldırmak isterseniz 'sudo rm -rf $INSTALL_DIR $BIN_DIR/pbsync' komutlarını çalıştırabilirsiniz."

exit 0
