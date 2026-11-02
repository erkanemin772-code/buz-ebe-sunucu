# Buz Ebe İnternet aktarıcısı

Farklı ağlardaki telefonları IP yazdırmadan birleştirir. Host, oda kapasitesini **2–5 oyuncu** seçer. Join, etkin odaları listeler ve seçilen odaya katılma isteği gönderir. Oyuncu ancak Host kabul edince oyuna alınır. Kabul bekleyenler doluluk sayısına dahil edilmez. Oda adları herkese açık listede görünür; uygulama kişisel bilgi gerektirmeyen oyuncu adları üretir.

Fizik, puan ve botlar Host telefonunda çalışır. Aktarıcı yalnızca bağlantıları, oda listesini, Host onaylarını ve canlı ses paketlerini yönetir. Boşalan oyuncu yuvasını oyun motoru botla doldurur. Host bağlantısı tamamen kapanırsa oda kapanır; Host görevinin başka cihaza aktarılması bu sürümde yoktur.

## WSS hizmeti

Aşağıdaki kendi sunucunuzda çalıştırma komutları Android proje kökünü varsayar; bağımsız sunucu paketinde `relay/` önekini kaldırın. Oluşturulan Render hizmeti yönetilen TLS kullanır; güncel hizmet ayrıntıları aşağıdaki Render bölümündedir.

Python 3.11 veya üstü gerekir. TCP/TLS modu standart kütüphane kullanır. WebSocket modu, RFC 6455 çerçeveleme, maskeleme, parçalama ve akış kontrolü için sabitlenmiş [websockets 17.4](https://pypi.org/project/websockets/17.4/) paketini kullanır:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r relay/requirements.txt
```

Kalıcı internet hizmeti için herkese açık bir sunucu, alan adı ve güvenilir TLS sertifikası gerekir. Sertifika ve özel anahtarı proje/zip dışında saklayın. Hizmeti ayrı ve yetkisiz bir hesapla çalıştırın:

```sh
.venv/bin/python relay/relay.py --websocket --bind 0.0.0.0 --port 8443 --cert /etc/buzebe/fullchain.pem --key /etc/buzebe/privkey.pem
```

TCP 8443 portunu açın. Android uygulamasını gerçek alan adınızı içeren `BUZEBE_RELAY_URL=wss://oyun.example.com:8443/ws` proje özelliğiyle derleyin. `BuildConfig.RELAY_URL` bu değeri kullanır. Örnek alan adı bir hizmet değildir. İstemci TLS sertifikasının güvenini ve alan adını doğrular. Telefonlar aynı hizmete bağlanmalıdır.

WebSocket uç noktası `/ws`, yalnızca sağlık yanıtı veren HTTP yolu `/` adresidir. HTTP ters vekilinde WebSocket Upgrade ve yeterli boşta kalma süresi etkin olmalıdır. Hizmeti bir süreç yöneticisiyle yönetin; sertifika yenilenince kontrollü yeniden başlatın. [Resmî WebSocket sunucu belgeleri](https://websockets.readthedocs.io/en/stable/reference/asyncio/server.html).

## Geçici internet geliştirme tüneli

TLS sonlandıran bir tünel, yalnızca loopback üzerinden dinleyen oyun aktarıcısını dışarı açabilir:

```sh
.venv/bin/python relay/relay.py --websocket --dev-loopback --bind 127.0.0.1 --port 8766
cloudflared tunnel --url http://127.0.0.1:8766
```

Oluşan HTTPS adresinin `wss://.../ws` karşılığı geliştirme APK'sının `BUZEBE_RELAY_URL` değerine yazılır. Bu geçici bir test hizmetidir: bilgisayar, aktarıcı ve tünel çalışırken erişilir; tünel yeniden başlatılınca adres değişebilir. Kalıcı barındırma veya üretim dağıtımı sağlamaz. Yalnızca oyun aktarıcısı paylaşılmalı; dosya sunucusu veya geliştirme araçları aynı tünele konulmamalıdır.

## TCP/TLS alternatifi

```sh
python3 relay/relay.py --bind 0.0.0.0 --port 8443 --cert /etc/buzebe/fullchain.pem --key /etc/buzebe/privkey.pem
```

APK adresi bu modda `tls://oyun.example.com:8443` olur. Düz TCP yalnızca `--dev-loopback --bind 127.0.0.1` ile kabul edilir. Normal HTTP tünelleri ham TLS/TCP taşımaz; tünel için WebSocket kullanın.

## Testler ve sınırlar

```sh
.venv/bin/python -m unittest discover -s relay -v
```

- Varsayılan sınır 500 oda ve 3032 bağlantıdır. `--max-rooms` 1–5000 aralığını kabul eder; bağlantı sınırı oda sınırının altı katı artı 32'dir.
- Oda kapasitesi 2–5, bekleyen istek sınırı oda başına 8'dir. Her Host yalnızca kendi odasındaki istekleri kabul edebilir. Kabul edilen misafirler 1…kapasite−1 yuvalarına yerleştirilir; Host 0'dır.
- Etkin oda listesi 64 oda/sayfa sunar ve `next` ile devam eder. Dolu odalar listede kalır, yeni katılımlar açık hata alır. Host kapanınca oda listeden kalkar.
- Mesaj üst sınırı 16 KiB, bağlantı başına sürekli hız 90 mesaj/saniye ve kısa süreli pay 120 mesajdır. İlk mesaj 10 saniye, mesaj sessizliği 20 saniye, yavaş alıcı 2 saniye ile sınırlıdır. İstemci beklerken de `ping` gönderir.
- WebSocket sıkıştırması kapalı, giriş kuyruğu 8 çerçeve, protokol ping/pong süreleri 10 saniyedir. Tarayıcı Origin başlığı reddedilir; yerel Android istemcisi Origin göndermez.
- Hareketler sonlu sayılara ve −1…1 aralığına sınırlandırılır; eski sıra numaraları yok sayılır. Onay bekleyen oyuncu kontrol, oyun durumu veya ses gönderemez ve oyun/ses verisi alamaz.
- Ses paketleri 40 ms / 1280 bayt PCM'nin doğrulanmış base64 karşılığıdır. Yalnızca aynı odadaki kabul edilmiş oyunculara canlı iletilir; gönderen kendisini geri duymaz. Ses kaydı veya ses geçmişi tutulmaz.
- Aktarıcı IP adresi, oda adı/kodu, oyuncu adı, ses veya mesaj içeriğini günlüğe yazmaz. Oda verileri yalnızca bellektedir. Tünel/barındırma sağlayıcısının kendi kayıt politikası ayrıca geçerlidir.
- Rekabetçi hile önleme, kalıcı hesaplar ve kalıcı skor bu hizmetin parçası değildir; Host oyun üzerinde yetkilidir.

## İletişim sözleşmesi

WebSocket'te her metin çerçevesi tek JSON nesnesidir. TCP/TLS'de her JSON nesnesi yeni satırla biter.

- İlk `host`: `name`, `capacity` → `hosted`: `code`, `name`, `capacity`, `id:0`.
- `list`, isteğe bağlı `cursor` alır. `rooms` yanıtındaki odalar `code`, `name`, `players`, `capacity` taşır; `next` başka sayfa yoksa `null` olur. Aynı tarama bağlantısı `list` ve `ping` ile açık kalabilir.
- Seçilen odanın dahili `code` alanıyla `join` ve isteğe bağlı `name` gönderilir. Oyuncuya `pending` / `requestId`, Host'a `join_request` / `requestId` / `name` gider. Kullanıcı oda kodu yazmaz.
- Host `accept` veya `reject` ile `requestId` gönderir. Kabulde oyuncu `joined` / `id` / `code` / `capacity`, Host `peer_joined` / `id` / `requestId` / `name` alır. Redde oyuncu `error` / `code:rejected` alır.
- Bekleyen ayrılırsa Host `request_left` / `requestId` alır. Eski veya doluluk nedeniyle kabul edilemeyen istekte Host bağlantısı kapanmaz; `request_error` alır.
- Host `type:state` anlık durumlarını tüm kabul edilmiş oyunculara yayınlar. Misafir `type:input`, `seq`, `x`, `z`, `drop`, `swap`, `active` gönderir; sunucu gerçek yuvasını `id` olarak ekler. `active:false` hareketi sıfırlar ve bot devrini bildirir.
- `type:voice`, `data` (base64) gönderildiğinde, diğer kabul edilmiş oyuncular `type:voice`, `data`, `from` (sunucunun belirlediği yuva) alır. Ses anlık duruma eklenmez.
- Ayrılan misafir Host'a `peer_left` / `id` olarak bildirilir. Host ayrılırsa kabul edilmiş ve bekleyen tüm oyuncular `error` / `code:host_left` alır. `ping` → `pong`; `leave` bağlantıyı kapatır. Hatalarda makine için `code`, kullanıcı için Türkçe `message` bulunur.

## Render üzerinde ücretsiz hizmet

2 Kasım 2026 tarihinde Render'da `buz-ebe-sunucu` adlı **Python 3 Web Service**, Frankfurt bölgesinde **Free / 0 USD** planıyla oluşturuldu. Kaynak, kullanıcının onayıyla herkese açık yapılan [erkanemin772-code/buz-ebe-sunucu](https://github.com/erkanemin772-code/buz-ebe-sunucu) deposunun `main` dalıdır; depodaki 9 bağımsız sunucu dosyası doğrudan bağlandı. İlk dağıtım (`1322a74`) 41 saniyede tamamlandı; Render durumu **Live**, HTTP sağlık yanıtı **200** olarak doğrulandı. Gerçek Android GameClient ile sabit WSS adresinde 15 uçtan uca kontrol geçti; test odalarının temizliği bağımsız oda listesiyle doğrulandı.

`render.yaml`, aynı ücretsiz tek örnekli düzeni tanımlar; ücretli kaynak tanımlanmaz. Bu dosya `relay` klasörü tek başına deponun kökü olduğunda kullanılır. Tam Android projesi depoya yüklenecekse Render Root Directory alanını `relay` yapın.

- HTTPS / sağlık adresi: [buz-ebe-sunucu.onrender.com](https://buz-ebe-sunucu.onrender.com/)
- Android WSS adresi: `wss://buz-ebe-sunucu.onrender.com/ws`
- Build: `pip install -r requirements.txt`
- Start: `python start_render.py`
- Health check: `/`
- Tek süreç/tek örnek kullanılır; oda listesi bellektedir. `BUZEBE_MAX_ROOMS=50`, otomatik dağıtım kapalıdır.
- Public adres Render tarafından HTTPS/WSS ve güvenilir TLS ile sağlanır. `--behind-proxy` yalnızca Render benzeri yönetilen TLS vekilinin izole HTTP arka bağlantısında kullanılmalıdır. Bunu doğrudan internete düz HTTP sunmak için kullanmayın.
- Android kaynak projesinde `BUZEBE_RELAY_URL=wss://buz-ebe-sunucu.onrender.com/ws` ayarı debug ve release için kaydedildi. 0.3.2 deneme APK ve imzalı AAB bu adresle yeniden derlendi.

Ücretsiz hizmet 15 dakika HTTP veya WebSocket mesajı almayınca uyur; yeni bağlantıda uyanması yaklaşık bir dakika sürebilir. Güncel Android istemcisi ilk bağlantıda geçici hatalar için en çok 90 saniye bekler; kullanıcı menüye çıkınca denemeleri durdurur. Render hesabında ödeme yöntemi yoksa aylık dış trafik kotası bitince hizmet ay sonuna kadar durdurulabilir.

Ücretli plana daha sonra geçilirse: 2 Kasım 2026 tarihli resmi fiyat ekranında en küçük ücretli web hizmeti 7 USD/ay gösterilmiştir. Bu sadece işlem ücretidir: Hobby çalışma alanında 5 GB/ay dış trafik dahildir, fazlası 0,15 USD/GB'dir; vergiler ve başka kaynaklar ayrıca olabilir. Canlı ses trafiği bu kotadan kullanır. Beş oyuncunun sürekli konuşması, mevcut PCM/base64 protokolünde oyun trafiği hariç yaklaşık 3,1 GB/saat dış trafik üretebilir. Bu, üst sınıra yakın bir hesaplamadır; ölçülmüş hizmet kapasitesi değildir. Ücretsiz plan için bu ücretli yükseltme yapılmamıştır; kullanıcı ayrıca onaylamadan ücretli plana geçilmemelidir.

Plan: https://render.com/pricing
WebSocket: https://render.com/docs/websocket
Trafik: https://render.com/docs/outbound-bandwidth

Render hizmetinin ilk dağıtımı tamamlandı ve HTTP sağlık yanıtı 200 olarak doğrulandı. WSS üzerinden gerçek Android GameClient ile 15 uçtan uca kontrol geçti. Bu kontroller tek emülatörde ayrı istemciler ve yapay ses paketleri kullandı; iki fiziksel telefonda ses kalitesi testi değildir. Test odalarının temizliği daha sonraki bağımsız oda listesiyle doğrulandı; ilk 20 saniyelik gözlem sonucu zamanında doğrulayamamıştı. Yeniden başlatma veya dağıtım mevcut odaları kapatır; 50 oda sınırı performans garantisi değildir.
