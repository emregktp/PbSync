# PbSync 🚀

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Proxmox yedeklerini diske hiç indirmeden, doğrudan S3, Google Drive, Dropbox gibi bulut depolama hedeflerine aktarın.**

PbSync, Proxmox Backup Server (PBS) üzerinde duran yedeklerinizi, yerel diskinizde hiç yer kaplamadan, "stream" (akış) yöntemiyle sıkıştırıp `rclone` aracılığıyla dilediğiniz bulut hedefine gönderen, web arayüzlü, modern bir araçtır.

"Agentless File-Level Restore" mantığıyla çalışır; yedeğin tamamını değil, içindeki dosyaları canlı olarak buluta aktarmanızı sağlar.

---

### 🌟 Temel Özellikler

-   **Web Arayüzü:** Tüm işlemleri tarayıcınız üzerinden, kolay ve şık bir arayüzle yönetin.
-   **Sıfır Yerel Disk Kullanımı:** Yedek dosyalarını önce sunucuya indirme derdi yok. Veri, RAM üzerinden işlenir ve doğrudan buluta akar.
-   **Geniş Bulut Desteği:** `rclone` entegrasyonu sayesinde 100'den fazla bulut depolama servisini (S3, Google Drive, FTP, WebDAV vb.) destekler.
-   **Akıllı Tarama:** VM ID'sini girdiğinizde, mevcut tüm yedek (snapshot) listesini otomatik olarak PBS'ten çeker.
-   **Kolay Kurulum:** Tek satırlık `curl | bash` komutu ile sisteme hızlıca kurun.
-   **Esnek Yapılandırma:** Tüm ayarları basit bir `.conf` dosyası üzerinden yönetin.
-   **Arka Plan İşlemleri:** Yedekleme işlemleri arka planda çalışır, bu sırada siz arayüzden başka işlemler yapabilirsiniz (ileride eklenecek log ekranı ile).

### ⚙️ Nasıl Çalışır?

PbSync, Linux'un güçlü araçlarını modern bir Python/FastAPI arayüzü arkasında birleştirir:
1.  **Map:** `proxmox-backup-client` ile seçilen yedek, bir "loop device" olarak sisteme tanıtılır (diske yazılmaz).
2.  **Mount:** Bu sanal disk, `salt okunur (read-only)` olarak geçici bir dizine bağlanır.
3.  **Stream & Pipe:**
    -   `tar` komutu, bağlanan dizindeki dosyaları okuyup standart çıktıya (stdout) bir arşiv akışı olarak gönderir.
    -   `pigz` (paralel çalışan gzip), bu akışı anında yakalar ve sıkıştırır.
    -   `rclone rcat`, sıkıştırılmış veri akışını alır ve doğrudan bulut hedefine yükler.

Tüm bu süreç, bir boru hattı (`|` pipe) gibi çalışır ve verinin diskle teması olmaz.

###  kurulum

Aşağıdaki komutu **root yetkileriyle** (`sudo`) çalıştırarak PbSync'i sisteminize kurabilirsiniz. Script, gerekli dizinleri oluşturacak, Python bağımlılıklarını kuracak ve `pbsync` komutunu sistem genelinde kullanılabilir hale getirecektir.

```bash
# DEĞİŞTİR: URL'yi kendi GitHub reponuzla güncelleyin
curl -sL https://raw.githubusercontent.com/KULLANICI_ADINIZ/PbSync/main/install.sh | sudo bash
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
