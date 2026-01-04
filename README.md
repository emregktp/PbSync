# PbSync 🚀

**Proxmox Backup Server (PBS) yedeklerini diske indirmeden, doğrudan Google Drive, S3 veya Dropbox'a stream edin.**

PbSync, Docker üzerinde çalışan, kullanıcı dostu web arayüzüne sahip bir yedekleme köprüsüdür. PBS üzerindeki snapshot'ları sunucu üzerinde sanal olarak bağlar (mount), seçtiğiniz dosyaları veya tüm diski anlık olarak sıkıştırıp (`.tar.gz`) buluta gönderir.

**En önemli özelliği:** Yedeği önce yerel diske indirmez (**Zero Local Storage**). Veriyi RAM üzerinden akıtarak (stream) doğrudan buluta yazar.

## 🌟 Özellikler

* **Web Arayüzü:** Kolay yapılandırma, VM/Snapshot tarama ve yedekleme başlatma.
* **Zero Local Storage:** Yerel disk alanınızı doldurmaz.
* **Akıllı Dosya Gezgini:** Snapshot içeriğini (klasör/dosya) yedeklemeden önce gezin ve sadece istediklerinizi seçin.
* **Host Mode:** Docker kısıtlamalarını aşarak doğrudan sunucu kernel'ı üzerinden yüksek performanslı mount işlemi yapar.
* **Rclone Gücü:** Google Drive, AWS S3, Dropbox, OneDrive ve Rclone'un desteklediği tüm bulut sağlayıcıları destekler.

---

## 🛠️ Kurulum

PbSync, disk işlemlerini (mount, map) yönetebilmek için **Host (Ana Sunucu)** üzerinde bazı araçlara ihtiyaç duyar.

### 1. Host Hazırlığı (Ubuntu / Debian)

PbSync'in çalışacağı sunucuya SSH ile bağlanın ve `proxmox-backup-client` aracını kurun. Bu adım **zorunludur**, aksi halde diskler bağlanamaz.

**Ubuntu 24.04 / Debian 12 için:**

```bash
# 1. Proxmox GPG Anahtarını indirin
wget [https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg](https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg) -O /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg

# 2. Depoyu ekleyin
echo "deb [http://download.proxmox.com/debian/pbs-client](http://download.proxmox.com/debian/pbs-client) bookworm main" > /etc/apt/sources.list.d/pbs-client.list

# 3. Paket listesini güncelleyin ve gerekli araçları kurun
apt update
apt install -y proxmox-backup-client kpartx lvm2 ntfs-3g fdisk
```

### 2. Projeyi İndirin ve Çalıştırın

```bash
# Repoyu klonlayın
git clone [https://github.com/KULLANICI_ADIN/PbSync.git](https://github.com/KULLANICI_ADIN/PbSync.git)
cd PbSync

# Uygulamayı başlatın
docker-compose up -d --build
```

Uygulama **`http://SUNUCU_IP:8000`** adresinde çalışacaktır. İlk açılışta sizi **Setup** ekranına yönlendirecektir.

---

## ⚙️ Yapılandırma

### 1. PBS Bağlantısı

Web arayüzündeki **Setup** ekranında Proxmox Backup Server bilgilerinizi girin:

* **Host:** PBS IP adresi (örn: `192.168.1.50`)
* **Datastore:** Yedeklerin olduğu datastore ismi (örn: `backup-disk`)
* **User/Pass:** PBS kullanıcı bilgileri (örn: `root@pam`).
* **Fingerprint:** Eğer Self-Signed sertifika kullanıyorsanız PBS Dashboard'dan alacağınız parmak izini buraya yapıştırın.

### 2. Google Drive (Rclone) Ayarı Nasıl Alınır?

PbSync, bulut bağlantısı için **Rclone** kullanır. Google Drive veya başka bir bulut servisini bağlamak için geçerli bir `rclone.conf` içeriğine ihtiyacınız vardır.

Bu içeriği oluşturmak için **kendi bilgisayarınızda (Windows/Mac/Linux)** terminali açın ve şu adımları izleyin:

1.  Bilgisayarınıza [Rclone indirin](https://rclone.org/downloads/) ve kurun.
2.  Terminali açın ve `rclone config` yazın.
3.  `n` tuşuna basarak **New Remote** oluşturun.
4.  İsim olarak `gdrive` verin.
5.  Storage türü listesinden **Google Drive**'ı bulun (genelde 18 numara) ve numarasını yazın.
6.  `client_id` ve `client_secret` kısımlarını boş geçin (Enter).
7.  `scope` olarak **1 (Full access)** seçin.
8.  `root_folder_id` ve `service_account_file` kısımlarını boş geçin (Enter).
9.  `Edit advanced config?` sorusuna `n` (Hayır) deyin.
10. `Use auto config?` sorusuna `y` (Evet) deyin. Tarayıcınız açılacak, Google hesabınızla giriş yapıp izin verin.
11. İşlem tamamlandığında terminalde `y` diyerek kaydedin.
12. Son olarak `q` ile çıkın.

**Config İçeriğini Alma:**

Terminalde şu komutu yazarak config içeriğini ekrana yazdırın:

```bash
rclone config show
```

Çıkan sonuç şuna benzer olacaktır:

```ini
[gdrive]
type = drive
scope = drive
token = {"access_token":"...","token_type":"Bearer","refresh_token":"...","expiry":"..."}
team_drive = 
```

**Bu bloğun tamamını kopyalayın ve PbSync kurulum ekranındaki "Rclone Configuration" kutusuna yapıştırın.**

---

## 🚀 Kullanım

1.  **Source Selection:** Listeden bir VM seçin. Ardından o VM'e ait tarihli bir Snapshot seçin.
2.  **Target:** Yedeğin gönderileceği bulut servisini (`gdrive`) seçin. İsterseniz `Backups/LinuxVMs` gibi bir alt klasör belirtebilirsiniz.
3.  **Browse Files:** "Browse Files" butonuna basın. Disk içeriği taranacaktır.
    * İstediğiniz klasörleri (örneğin sadece `/home` ve `/etc`) seçmek için yanlarındaki **Add (+)** butonuna basın.
    * Eğer hiçbir şey seçmezseniz (kutucuk boş kalırsa), PbSync **tüm diski** yedekler.
4.  **Start:** "Start Stream Task" butonuna basın.
5.  Aşağıdaki siyah pencereden (Log) işlemin durumunu ve yükleme hızını canlı olarak izleyebilirsiniz.

---

## ⚠️ Önemli Notlar & Güvenlik

* **Yetkiler:** Bu konteyner `privileged: true` modunda çalışır ve host makinenin PID alanını kullanır. Bu, disk mount işlemleri için zorunludur. Uygulamayı sadece güvenli iç ağınızda barındırın.
* **Geçici Dosyalar:** PbSync, işlem sırasında `/mnt/pbsync_restore` klasörünü kullanır. İşlem bittiğinde veya hata aldığında bu klasörü otomatik temizler.
* **Performans:** Yedekleme hızı; PBS diskinizin okuma hızı, sunucunun RAM/CPU gücü ve internet upload hızınızla sınırlıdır.

---

## 📄 Lisans

Bu proje MIT lisansı ile lisanslanmıştır.