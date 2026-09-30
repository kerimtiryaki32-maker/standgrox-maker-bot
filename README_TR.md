# Standgrox Maker Bot — Türkçe

[English](README.md) · [@crryptooKerim](https://x.com/crryptooKerim) tarafından hazırlandı.

Tek bir StandX vadeli işlem pazarında mark fiyata göre iki taraflı post-only limit emirleri yerleştiren yerel Windows panelidir. İlk açılış **simülasyon** modundadır.

> Deneysel işlem yazılımıdır. Erken iptale rağmen emirler gerçekleşebilir. Pozisyon azaltıcı piyasa çıkışı taker ücreti ve fiyat kayması yaratır; gecikebilir veya başarısız olabilir. StandX hesabınızı izleyin. Kâr veya Maker Hours garantisi yoktur.

## Kurulum

Windows 10/11, Python 3.10+, ilk EVM girişi için Node.js ve kendi StandX hesabınız gerekir. Az bakiyeli ayrı bir cüzdan kullanın; seed phrase girmeyin.

```powershell
py -m pip install -r requirements.txt
npm install
Copy-Item .env.example .env
```

1. `node make_sign_key.js` çalıştırın; çıkan `STANDX_SIGN_KEY_HEX=...` satırını **yalnızca kendi bilgisayarınızdaki** `.env` dosyasına yazın.
2. Giriş için `.env` içine `EVM_WALLET_PRIVATE_KEY=...` ekleyin. Bu çok hassas anahtarı GitHub'a yüklemeyin veya başkasına göndermeyin. Token oluşturulduktan sonra bu satırı silebilirsiniz.
3. `node login.js` çalıştırın; gelen `STANDX_TOKEN=...` satırını `.env` içine yazın. 401 hatasında tekrar giriş yapıp token yenileyin; aynı oturumda signing key'i koruyun.
4. Hatayı terminalde görmek için `py main.py`, pencereli açmak için `run_app.pyw` çalıştırın. `create_shortcut.bat` isteğe bağlı olarak masaüstü kısayolu oluşturur.
5. Windows'ta `build_windows.bat` isteğe bağlı PyInstaller kurar ve `dist/Standgrox Maker Bot.exe` üretir. EXE kullanırken özel `.env` dosyanız EXE'nin yanında bulunmalıdır. EXE burada Windows üzerinde doğrulanmadı.

Panelden pazar, hedef/alt/üst BPS, bakiye kullanımı (%100'e kadar), kaldıraç ve acil zarar eşiği seçilir. Eski panelin görünümü ve ayarları korunur. Varsayılan hedef 5,5 ve alt 5 BPS iken yaklaşan emir 5,25 BPS veya daha yakında çekilir. Karşı taraftaki emir defteri fiyatı 1 BPS yakına gelirse de çekilir. Kontrol üç saniyede bir yapılır; eski 10 saniyelik yenileme beklemesi kaldırılmıştır. Canlı işlem için **Canlı emirleri etkinleştir** seçilip onay verilmelidir. İlgili pazarda açık emir veya pozisyon yoksa bot hesap kaldıracını değiştirebilir. %100 bakiye seçildiğinde borsanın ek ücret rezervi istemesi emri reddettirebilir.

Emir gerçekleşirse diğer bot emri iptal edilir ve pozisyon azaltıcı post-only limit çıkış en iyi satışın (long) veya alışın (short) bir fiyat adımı içine yerleştirilir. Beş saniyede tamamen kapanmazsa limit çıkışın iptali doğrulanıp kalan pozisyon için pozisyon azaltıcı piyasa IOC emri gönderilir. Seçilen acil zarar eşiğine ulaşılırsa doğrudan piyasa çıkışı uygulanır. Çıkış veya iptal doğrulanamazsa bot ikinci çıkışı körlemesine göndermeden durur. Limit emrin hemen dolma garantisi yoktur. Üç saniye boşta bekleme aralığıdır; emir yenileme garantisi değildir. WebSocket üzerinden mark, tam emir defteri, emirler ve pozisyonlar takip edilir. Ayrı giriş emri koruması her piyasa bildiriminde uyanır; veri yaşını ayrıca 100 ms aralıklarla kontrol eder. Mark alt BPS ve erken çekme sınırına gelirse, emir üst BPS dışına çıkarsa veya karşı defter fiyatı 1 BPS yakına gelirse iptal ister. Yeni giriş gönderilmeden önce canlı fiyatlar yeniden kontrol edilir. Eksik/geçersiz veri veya yerel alım zamanı 3 saniyeden eski akış, yeni girişleri engeller ve mevcut girişlerin iptalini ister. 100 ms borsanın iptal süresi garantisi değildir; HTTP yazmaları sıralanır ve iptal kabulü son durum onayıyla doğrulanır. Güvenli fiyat için kısa süre emirsiz kalınabilir.

Hedef BPS, alış ve satış fiyatlarını mark fiyatının iki yanına yerleştirir. Alt/Üst BPS, mevcut emrin ne zaman yenileneceğini belirler. Yaklaşmış emir, yeni güvenli fiyat henüz bulunamasa da iptal edilir. Paneldeki son 60 dakikalık iki taraflı süre, gözlenen açık emirlere dayanan yerel ve ihtiyatlı tahmindir; StandX'in resmi Maker Hours sonucu değildir. StandX'te iki tarafın 10 BPS içinde olması ve saatlik en az 30 dakika korunması gerekir; 42 dakika yükseltilmiş kademedir. Piyasa hareketi, dolum, API gecikmesi veya ret uygunluğu kesintiye uğratabilir.

Tek giriş emri görünüyorsa bot her taramada eksik tarafı dener. 15 saniye sonra bekleyen emirleri doğrular, tek kalan emri iptal eder ve iki tarafı yeniden kurar. Geciken emir veya iptal doğrulanamıyorsa aynı tarafa ikinci emir göndermek yerine bekler. Bu işlem iki taraflı süreyi veya dolum olmayacağını garanti etmez.

**Durdur** bu botun açık emirlerini iptal edip doğrular. Bot oturumunda gerçekleşmiş pozisyon varsa yukarıdaki önce maker çıkışını dener; kapanış doğrulanmazsa StandX hesabını hemen kontrol edin. Başlangıçta zaten açık olan pozisyonlar otomatik işlem görmez. Başka stratejilerin emirlerine dokunulmaz.

WebSocket mark veya emir defteri eksik ya da gecikmişse ayrı bir yedek işçi bir saniyelik aralıklarla yeni genel HTTP verilerini almayı dener. Yavaş veya geçersiz verilerle emir gönderilmez; üç saniyelik veri yaşı HTTP isteğinin başlangıcından ölçülür. Daha yeni WebSocket verileri korunur. Bağlantı hatası ve yedek verinin hazır olduğu İşlem günlüğünde gösterilir. HTTP yedeği canlı akıştan yavaştır ve dolumu önleme garantisi vermez.

## GitHub dosya kontrolü

Kaynak kod ve boş `.env.example` paylaşılır. **`.env` asla paylaşılmaz.** `build/`, `dist/`, `__pycache__/`, `node_modules/`, `*.spec` ve kısayollar üretilen dosyalardır; GitHub'a konmaz. `.gitignore` bunları dışlar ama commit öncesi `git status` inceleyin. Daha önce anahtar commit edildiyse dosyayı sonradan silmek yeterli değildir: anahtarları ve token'ı yenileyin.

Canlı hesapta burada test edilmedi. İşlem öncesinde StandX [HTTP API](https://docs.standx.com/standx-api/perps-http) ve [EVM kimlik doğrulama](https://docs.standx.com/standx-api/perps-auth-evm-example) belgelerini inceleyin.
