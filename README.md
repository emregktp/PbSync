# PbSync 🚀

**Proxmox yedeklerini (PBS) diske indirmeden, doğrudan Google Drive, S3 veya Dropbox'a stream edin.**

PbSync, Docker üzerinde çalışan, web arayüzlü (UI) bir yedekleme aracıdır. Proxmox Backup Server üzerindeki snapshot'ları sanal olarak mount eder, sıkıştırır ve rclone aracılığıyla buluta gönderir.

## 🌟 Özellikler
* **Web Arayüzü:** Tüm konfigürasyon ve yönetim tarayıcı üzerinden.
* **Disk Dostu:** Yedeği önce diske indirmez (Zero Local Storage). RAM üzerinden akıtır.
* **Dockerize:** `docker-compose up` ile tek komutla çalışır.

## 🚀 Kurulum

1. Repoyu klonlayın:
   ```bash
   git clone [https://github.com/KULLANICI_ADIN/PbSync.git](https://github.com/KULLANICI_ADIN/PbSync.git)
   cd PbSync