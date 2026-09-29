# Standgrox Maker Bot — Türkçe

[English](README.md) · [@crryptooKerim](https://x.com/crryptooKerim) tarafından hazırlandı.

Tek bir StandX vadeli işlem pazarında mark fiyata göre iki taraflı post-only limit emirleri yerleştiren yerel Windows panelidir. İlk açılış **simülasyon** modundadır.

> Deneysel işlem yazılımıdır. Canlı emirler gerçekleşebilir, limit çıkış gerçekleşmeyebilir, acil market çıkışı belirlediğiniz eşikten fazla zarar yazabilir. StandX hesabınızı izleyin. Kâr veya Maker Hours garantisi yoktur.

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

Panelden pazar, hedef/alt/üst BPS, bakiye kullanımı, kaldıraç ve acil zarar eşiği seçilir. Canlı işlem için **Canlı emirleri etkinleştir** seçilip onay verilmelidir. İlgili pazarda açık emir veya pozisyon yoksa bot hesap kaldıracını değiştirebilir.

**Durdur** bu botun ilgili pazarda açtığı giriş ve çıkış emirlerini iptal etmeye çalışır ve açık emir sonucunu doğrular. Pozisyonu kapatmaz. İptal doğrulanmazsa StandX hesabını hemen kontrol edin. Çıkış emri iptal edilen açık pozisyonu ayrıca yönetmeniz gerekir. Başka stratejilerin emirlerine dokunulmaz.

## GitHub dosya kontrolü

Kaynak kod ve boş `.env.example` paylaşılır. **`.env` asla paylaşılmaz.** `build/`, `dist/`, `__pycache__/`, `node_modules/`, `*.spec` ve kısayollar üretilen dosyalardır; GitHub'a konmaz. `.gitignore` bunları dışlar ama commit öncesi `git status` inceleyin. Daha önce anahtar commit edildiyse dosyayı sonradan silmek yeterli değildir: anahtarları ve token'ı yenileyin.

Canlı hesapta burada test edilmedi. İşlem öncesinde StandX [HTTP API](https://docs.standx.com/standx-api/perps-http) ve [EVM kimlik doğrulama](https://docs.standx.com/standx-api/perps-auth-evm-example) belgelerini inceleyin.
