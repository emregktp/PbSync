# PbSync 🚀

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Proxmox yedeklerini diske hiç indirmeden, doğrudan S3, Google Drive, Dropbox gibi bulut depolama hedeflerine aktarın.**

PbSync, Proxmox Backup Server (PBS) üzerinde duran yedeklerinizi, yerel diskinizde hiç yer kaplamadan, "stream" (akış) yöntemiyle sıkıştırıp `rclone` aracılığıyla dilediğiniz bulut hedefine gönderen, web arayüzlü, modern bir araçtır.

---

### 🌟 Temel Özellikler

-   **Tam Web Arayüzü:** PBS bağlantısı, Rclone ayarları ve yedekleme işlemleri dahil her şeyi tarayıcıdan yönetin.
-   **Sıfır Yerel Disk Kullanımı:** Yedek dosyalarını önce sunucuya indirme derdi yok. Veri, RAM üzerinden işlenir ve doğrudan buluta akar.
-   **Geniş Bulut Desteği:** `rclone` entegrasyonu sayesinde 100'den fazla bulut depolama servisini (S3, Google Drive, FTP, WebDAV vb.) destekler.
-   **Docker ile Kolay Kurulum:** Bağımlılıklarla uğraşmadan, izole ve güvenli bir ortamda çalışır.
-   **Kalıcı Ayarlar:** Yapılandırmalarınız Docker volume sayesinde korunur.

### 🚀 Kurulum

Projeyi GitHub'dan sunucunuza çekin ve Docker Compose ile başlatın.

```bash
# Projeyi klonlayın
git clone https://github.com/emregktp/PbSync.git
cd PbSync

# Servisi başlatın
docker-compose up -d --build
```

Kurulum tamamlandığında, `pbsync.conf` dosyanızı yapılandırmanız istenecektir.

### 🛠️ Yapılandırma

Kurulum sonrası, ayarlarınızı `~/.config/pbsync/pbsync.conf` dosyasında yapmanız gerekmektedir.

```ini
[PBS]
# Proxmox Backup Server repository adresiniz
repository = kullanici@pam@pbs-sunucusu:verideposu

[PBSYNC]
# Yedeklerin geçici olarak bağlanacağı dizin
mount_point = /mnt/pbsync_restore
```

### 🚀 Kullanım

1.  **Servisi Başlatma:**
    Aşağıdaki komutla web sunucusunu başlatın. `sudo` gereklidir çünkü `mount` gibi yetki isteyen işlemler yapılacaktır.
    ```bash
    sudo pbsync
    ```

2.  **Arayüze Erişin:**
    Tarayıcınızı açın ve `http://127.0.0.1:8000` adresine gidin.

3.  **Yedeklemeyi Başlatın:**
    -   Arayüzden VM ID'sini girip "Yedekleri Tara" butonuna tıklayın.
    -   Açılan listeden istediğiniz yedeği (snapshot) seçin.
    -   Hedef `rclone` bulut hesabınızı seçin.
    -   "AKTARIMI BAŞLAT" butonuna tıklayın.

İşlemin başladığına dair bir bildirim alacaksınız. Detaylı ilerlemeyi (şimdilik) `pbsync` komutunu çalıştırdığınız terminal ekranından takip edebilirsiniz.

---

### 💡 Gelecek Geliştirmeler

-   [ ] Web-socket ile canlı log ve ilerleme çubuğunu arayüze taşıma.
-   [ ] Otomasyon için komut satırı argümanları (`pbsync --vmid 101 --latest --remote s3`).
-   [ ] LVM partisyon yapısına sahip yedekler için otomatik `lvscan` ve mount desteği.
-   [ ] Yedek içinden tek tek dosya/klasör seçerek geri yükleme.

### Lisans

Bu proje MIT Lisansı altında dağıtılmaktadır. Detaylar için `LICENSE` dosyasına göz atın.
