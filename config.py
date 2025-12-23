import configparser
import os

# Ayar dosyasının adı
CONFIG_FILENAME = "pbsync.conf"

# Proje ana dizini
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Aranacak yollar: önce proje klasörü, sonra kullanıcı'nın config dizini
config_paths = [
    os.path.join(BASE_DIR, CONFIG_FILENAME),
    os.path.expanduser(f'~/.config/pbsync/{CONFIG_FILENAME}')
]

config = configparser.ConfigParser()
found_path = None

for path in config_paths:
    if os.path.exists(path):
        config.read(path)
        found_path = path
        print(f"Yapılandırma dosyası bulundu ve yüklendi: {found_path}")
        break

def get_setting(section, key, fallback=None):
    """Yapılandırma dosyasından bir ayarı güvenli bir şekilde alır."""
    # config.get'in fallback parametresi zaten bizim için bu işi yapıyor.
    value = config.get(section, key, fallback=fallback)
    
    if value is None and fallback is None:
        # Bu durum ancak section yoksa ve fallback belirtilmemişse oluşur.
        raise ValueError(f"'{section}.{key}' ayarı yapılandırma dosyasında bulunamadı ve varsayılan değer (fallback) sağlanmadı.")
        
    return value

# Ayarları global değişkenler olarak dışa aktar
try:
    PBS_REPO = get_setting('PBS', 'repository')
    MOUNT_POINT = get_setting('PBSYNC', 'mount_point')
    # Rclone ayarı isteğe bağlı olduğu için fallback olarak boş bir string veriyoruz.
    GDRIVE_FOLDER_ID = get_setting('RCLONE', 'gdrive_root_folder_id', fallback='')

except (ValueError, configparser.NoSectionError) as e:
    print(f"UYARI: Yapılandırma hatası - {e}. Uygulama düzgün çalışmayabilir.")
    # Fallback değerleri burada tanımlayarak uygulamanın çökmesini engelleyebiliriz
    # ancak kullanıcıyı uyarmak daha doğru.
    PBS_REPO = 'LUTFEN_pbsync.conf_DOSYASINI_YAPILANDIRIN'
    MOUNT_POINT = '/mnt/pbsync_restore'
    GDRIVE_FOLDER_ID = ''

if __name__ == '__main__':
    print("Yapılandırma modülü testi:")
    print(f"  PBS Deposu: {PBS_REPO}")
    print(f"  Bağlantı Noktası: {MOUNT_POINT}")
    print(f"  Google Drive Folder ID: {GDRIVE_FOLDER_ID if GDRIVE_FOLDER_ID else '(Belirtilmemiş)'}")

    if not found_path:
        print("\n-> 'pbsync.example.conf' dosyasını 'pbsync.conf' olarak kopyalayıp,")
        print("   içeriğini kendi sisteminize göre düzenlemeyi unutmayın.")