# outreachos — İş Arama Otomasyonu

Her sabah tek başına çalışan bir iş-arama / soğuk outreach sistemi: şirket keşfeder, eler,
e-postayı doğrular, kişiselleştirilmiş bir mail **taslağı** yazar, uydurma iddia içerip
içermediğini denetler ve sana günlük rapor yollar. Bilgisayarın kapalıyken de çalışır
(GitHub Actions cron). Artı: yerel, tek-kullanıcılı bir **dashboard + karar motoru**.

Sıfır bağımlılık — sadece **Python 3** (stdlib). `npm install` / `pip install` yok.

> 📐 **Mimarinin tamamı ve "kendin nasıl yaparsın" rehberi: [ARCHITECTURE.md](ARCHITECTURE.md)**
> Tasarım kararları, düşülen çukurlar, bounce guard matematiği, LLM denetim katmanı ve
> adım adım kurulum sırası orada.

**Öne çıkan tasarım kararları** (ayrıntı: ARCHITECTURE.md)
- **Gönderim varsayılan olarak kapalı**: outreach taslakta kalır, gönderme kararı insanda.
  İsteyen veto pencereli gönderimi (`AUTO_SEND`) ve otomatik onarımı (`AUTO_REPAIR`) açıp
  sistemi insan müdahalesi olmadan çalıştırır (§8).
- **Üç ayrı LLM adımı** (ele → yaz → *bağımsız* denetle) + deterministik sayı kontrolü;
  tek çağrıya "yaz ve kendini denetle" demek çalışmıyor.
- **E-posta tahmin edilmez** — adres sitede birebir geçmeli **ve** MX doğrulanmalı.
- **Teslim edilebilirlik guard'ı** — bounce oranı ölçülür; %6'yı aşarsa veya bekleyen
  taslak 12'yi geçerse yeni outreach otomatik durur.
- **Bekleyen taslak triyajı**: her run mükerrer + içerik denetimi yapıp "gönder / sil /
  düzelt" listesi çıkarır; `AUTO_REPAIR` açıksa düzeltilecekleri kendisi onarır.

> 🚀 **Sıfırdan kendi kurulumunuz için: [SETUP.md](SETUP.md)** (fork, private veri reposu,
> secret'lar, ilk deneme run'ı, otomasyon anahtarları).

Tek doğruluk kaynağı: `tracker.db` (SQLite). `outreach_log.csv` insan-okunur yedek olarak otomatik senkronlanır.

---

## 1. Hızlı Başlangıç (2 komut)

```bash
# 1) mevcut CSV + state.json -> tracker.db  (tek seferlik, veriyi SİLMEZ)
python src/migrate.py

# 2) dashboard'u başlat
python src/app.py
```

Sonra tarayıcıda: **http://localhost:8787**

> Farklı port: `DASHBOARD_PORT=9000 python src/app.py`

İlk açılışta o güne kadar loglanan şirketler dashboard'da görünür.

---

## 2. Dosya Yapısı

Bu repo **kodun tamamını** tutar; kişisel veri (profil, CV, loglar) ayrı bir private
repoda durur ve koda `OUTREACHOS_DATA_DIR` ile gösterilir. Tek klasörde çalışmak
isterseniz değişkeni hiç tanımlamayın — veri kökü repo kökü olur.

```
outreachos/
├── src/
│   ├── db.py               # şema + durum normalizasyonu + veri kökü seam'i
│   ├── migrate.py          # CSV+JSON -> DB (idempotent, güvenli)
│   ├── app.py              # stdlib web sunucu + JSON API
│   └── pipeline.py         # KARAR MOTORU (rol filtresi, email doğrulama)
├── scripts/                # GÜNLÜK MOTOR (cron bunu çalıştırır)
│   ├── discover.py         # ana akış: keşif → doğrulama → taslak → guard → rapor
│   ├── drafting.py         # judge / draft / verify (3 ayrı LLM çağrısı)
│   ├── numeric.py          # deterministik sayı denetimi (taslaktaki her sayı profilde ya da sitede)
│   ├── deliverability.py   # bounce ölçümü + eşikler + devre kesici
│   ├── audit_drafts.py     # bekleyen taslak triyajı
│   ├── repair.py           # ⚠️ taslakları onarır, onarılamayanı siler (varsayılan KAPALI)
│   ├── autosend.py         # veto pencereli gönderim (varsayılan KAPALI)
│   ├── report.py           # günlük rapor + ATS digest
│   ├── jobboard.py         # adresi olmayan şirketin ilan panosu (Greenhouse, Lever, Ashby, Workable)
│   ├── tracking.py         # gelen yanıtı okur ve sınıflar (görüşme, ret, otomatik, gürültü)
│   ├── board.py            # takip panosu: firma durumu, bugünün işleri, ilan defteri
│   ├── prep.py             # görüşme daveti gelince hazırlık notu (site + profil, uydurma denetimli)
│   ├── refresh_pool.py     # aday havuzunu tazeler
│   └── get_gmail_token.py  # Gmail refresh token → GitHub secret
├── web/index.html          # dashboard arayüzü (Tailwind + Chart.js CDN)
├── state.example.json      # profil şablonu — kopyalayıp doldurun
├── SETUP.md                # sıfırdan kurulum rehberi
└── .github/workflows/daily.example.yml   # cron şablonu (veri reponuza kopyalayın)
```

Veri tarafında (ayrı private repo veya aynı klasör) beklenen dosyalar:

```
state.json              # profil + kurallar + geçmiş
outreach_log.csv        # insan-okunur log
tracker.db              # SQLite — BİRİNCİL veri (migration üretir)
cv/                     # taslak yazarken kullanılan özgeçmişler
gunluk_ozet/            # her run'ın günlük özeti
takip.md                # takip panosu: motor her run'da baştan yazar
candidate_pool.json     # keşif havuzu
```

Motorun saf mantık modülleri ağsız kendi kendini test eder:

```bash
python scripts/deliverability.py
python scripts/audit_drafts.py
python scripts/drafting.py
python scripts/numeric.py
python scripts/repair.py
python scripts/autosend.py
python scripts/report.py
python scripts/jobboard.py
python scripts/tracking.py
python scripts/board.py
python scripts/prep.py
python scripts/discover.py --self-test
```

---

## 3. Dashboard Özellikleri

| Sekme | İçerik |
|-------|--------|
| **Genel Bakış** | Toplam/bugün/aday/taslak/elenen KPI'ları, durum pasta grafiği, günlük trend, taslak limiti göstergesi (aç/kapat) |
| **Kanban** | Aktif akış kolonları (Araştırılmadı → Taslak → ATS → Form → Aday → Bekliyor), **sürükle-bırak ile durum değiştir** |
| **Tablo** | Firma/sektör/not araması + durum/sektör/proje filtreleri |
| **Kişisel/Bekleyen** | 👤 tanıdık şirketleri ve manuel karar bekleyenler — otomasyon dışı |

**Şirket detay paneli** (karta tıkla): tüm alanlar, serbest not, kişisel işaretleme, Gmail taslak linki (`✉️ Gmail taslağını aç`), ve **audit log** (kim/ne zaman hangi alanı değiştirdi). Her düzenleme hem DB'ye hem CSV'ye yazılır.

---

## 4. Durum (durum) Enum'u

Kanonik: `TASLAK, ATS_DIGEST, ADAY, FORM_DOLDURULDU, ELENEN_SENIORITY, ELENEN_LOKASYON, ELENEN_SEKTOR, ELENEN_UYGUNSUZ, ELENEN_EPOSTA_BULUNAMADI, ELENEN_KISISEL, ARASTIRILMADI, BEKLIYOR`

Gerçek CSV'deki zengin değerler (`ATS_DIGEST_CHROME_GEREKLI`, `ADAY_Product Owner`, `FORM_DOLDURULDU_CAPTCHA_BEKLIYOR` vb.) kanonik enum'a normalize edilir; **ham değer `durum_raw` kolonunda korunur** — hiçbir bilgi kaybolmaz.

---

## 5. Karar Motoru (pipeline.py)

Manuel outreach akışını deterministik koda döker. Anahtarsız çalışır — eldeki adayı sınıflandırır:

```bash
python src/pipeline.py selftest                 # kural testleri (7/7 geçer)
python src/pipeline.py role "Sr. Frontend Developer"
python src/pipeline.py decide '{"firma":"Acme","title":"Junior AI Engineer","link":"https://jobs.lever.co/acme/x"}'
```

Uyguladığı **değiştirilemez kurallar** (`state.json`'dan okunur):
- **zero_tolerance_rule** — `Senior/Sr./Lead/Kıdemli/5+ yıl` veya sert-ele (`QA/Test/Destek/Veri girişi`) eşleşirse ilan **hiçbir tabloya girmez**.
- **Kişiye özel email tahmin edilmez** — sadece doğrulanmış genel email (`info@/hello@/careers@`) veya gerçek ATS linki (Lever/Greenhouse/Ashby/Workable) kabul edilir. Aksi halde `ELENEN_EPOSTA_BULUNAMADI`.
- `excluded_companies_seed_personal` (tanıdıklar) ve `excluded_sectors` (savunma/siber) pipeline'a **hiç girmez**.

---

## 6. Otomasyon Pipeline'ı — Harici Entegrasyonlar (opsiyonel)

Karar motoru anahtarsız çalışır. **Canlı keşif ve otomatik taslak oluşturma** için aşağıdaki anahtarlar gerekir. Hangisini kullanacağın sana bağlı; sistem hardcode etmez, ortam değişkeninden okur.

### 6a. Web Arama API'si (keşif için)
Yeni şirket keşfi (İTÜ Çekirdek / Webrazzi / YC dizini taraması) bir arama API'si ister. Birini seç ve anahtarı ortam değişkenine koy:

```bash
export SEARCH_PROVIDER=serper && export SEARCH_API_KEY="..."   # Serper.dev (serper.dev/api-key)
```

> Google Custom Search JSON API'yi **kullanma**: yeni müşterilere kapalı (her çağrı
> 403 döner) ve 2027-01-01'de tamamen kapanıyor. Bing Search API de emekliye ayrıldı.
> Bulut ajanı (günlük cron) Serper kullanıyor; ayrıntı için [SETUP.md](SETUP.md).
Windows PowerShell: `$env:SEARCH_API_KEY="..."`

> Anahtar yoksa keşif atlanır; dashboard ve mevcut kayıtlar sorunsuz çalışır.

### 6b. Gmail API (taslak oluşturma + yanıt takibi + kendine rapor)
Şirketlere gönderim **varsayılan olarak kapalıdır**: outreach yalnızca `drafts.create` ile taslak olur, gönderme kararı sende. `gmail.send` iki yerde kullanılır: (1) günlük özet **senin kendi adresine** rapor olarak gider (hedef bağlı hesabın kendi adresi değilse reddedilir); (2) `AUTO_SEND=1` yaparsan, denetimden ✅ geçen taslaklar bir günlük veto penceresinden sonra şirketlere gönderilir (§8).

1. [Google Cloud Console](https://console.cloud.google.com/) → yeni proje → **Gmail API**'yi etkinleştir.
2. **OAuth consent screen** → External → kendi Gmail'ini test kullanıcısı ekle.
3. **Credentials → OAuth client ID → Desktop app** → `credentials.json` indir, proje köküne koy.
4. Kapsam (scope): `gmail.compose` (taslak) + `gmail.readonly` (yanıt takibi) + `gmail.send` (kendine günlük rapor; `AUTO_SEND=1` ise onaylı taslakların gönderimi). Daha önce bağladıysan send yeni scope olduğu için Gmail'i **yeniden bağla**.
5. İlk çalıştırmada tarayıcıda onay verirsin; token `token.json`'a kaydolur.

> `credentials.json` ve `token.json` **asla commit edilmez** (gizli). CAPTCHA'lı formlar otomatik geçilmez — sana bırakılır.

---

## 7. Güvenlik Kuralları (değiştirilemez)

- ❌ **Şirketlere** gönderim varsayılan olarak kapalı: outreach sadece taslak. ✅ Günlük rapor yalnızca **senin kendi adresine** gider (`send_self_report`, dış adrese asla).
- ⚙️ `AUTO_SEND=1` bilinçli bir tercihtir ve kendi zinciri vardır: yalnızca içerik denetiminden ✅ geçmiş, bir gün veto penceresinde beklemiş ve MX'i yeniden doğrulanmış taslak gönderilir; günlük sert tavan uygulanır.
- ⚙️ `AUTO_REPAIR_DELETE=1` taslakları **kalıcı** siler (çöp kutusu yok). Sadece onarılamayan ve mükerrer taslaklar için; kapalıyken motor hiçbir taslağı silmez.
- ✅ **Aynı yere ikinci mail gitmez:** gönderimden hemen önce son bir kapı alıcıyı aynı run'a, kalıcı gönderim kaydına ve Gmail Gönderilenler'e (tarih sınırı olmadan) karşı yeniden sorar. Daha önce mail gitmişse taslak gönderilmez; Gmail'e sorulamazsa da gönderilmez, sonraki run yeniden dener.
- ✅ Motor yalnızca **kendi açtığı** taslaklara dokunur: Gmail'de elle yazdığın taslaklar denetlenmez, onarılmaz, silinmez, gönderilmez (raporda "motorun kaydında yok" diye sayılır).
- ❌ CAPTCHA otomatik geçilmez; LinkedIn'de otomatik mesaj atılmaz (hazır arama linki verilir, mesajı sen atarsın).
- ❌ Kişisel tanıdık şirketleri (`excluded_companies_seed_personal`) pipeline'a girmez — Kişisel/Bekleyen sekmesinde manuel karar bekler.
- ✅ `daily_caps` config'den açılıp kapanabilir; varsayılan **açık** (Genel Bakış'taki "aktif" anahtarı).
- ✅ Şüphedeyken durmak varsayılandır: doğrulama yapılamadıysa taslak "temiz" sayılmaz.

---

## 8. Teslim Edilebilirlik (Deliverability) Katmanı

Soğuk mail atan her sistemin çarptığı duvar: bounce oranı yükselir, mailler spam'e düşer.
Sistem bunu **ölçer ve kendini frenler** (matematiği ve gerekçesi: [ARCHITECTURE.md](ARCHITECTURE.md) §5).

| Durum | Hard-bounce (son 30g) | Davranış |
|---|---|---|
| 🟢 İYİ | < %3 | normal |
| 🟡 İZLEME | %3–6 | rapora uyarı + trend |
| 🔴 KRİTİK | ≥ %6 | **yeni outreach durur** (takip/rapor devam eder) |

Ek fren: **bekleyen taslak > 12 ise yeni taslak üretilmez** — önce backlog boşalsın
(biriken taslak hem boşuna LLM parası hem de bir gün topluca gönderilirse spam sinyali).

Bounce alan adres `email_dead` işaretlenir, o domain bir daha denenmez. Küçük örneklemde
(12 gönderimden az) fren devreye girmez; Gmail okunamazsa "ölçülemedi" olur ve **fren
uygulanmaz** (fail-open).

**Bekleyen taslak triyajı:** her run taslakları ✅ temiz / 🔁 mükerrer / ⚠️ içerik hatası /
👀 kararsız diye etiketleyip rapora yazar. Etiketler sana iş vermez: mükerreri ve içerik
hatasını otomatik onarım çözer, kararsız taslak sonraki run'da yeniden denetlenir ve yine
kararsız kalırsa onarıma gider. Kararlar gövde, profil ve denetim kurallarının
hash'iyle cache'lenir; kurallar değişince eski kararlar kendiliğinden tazelenir.

**Opsiyonel: otomatik onarım** (`AUTO_REPAIR=1`, varsayılan **kapalı**): ⚠️ damgalı
taslaktaki desteklenmeyen iddiayı çıkarır, Gmail'in sarmaladığı linkleri açar ve sonucu
yeniden denetimden geçirir (en fazla iki tur, run başına `REPAIR_MAX` taslak). Onarılan
taslak ✅ sayılır ve `AUTO_SEND` açıksa ertesi gün gönderim kuyruğuna girer. `AUTO_REPAIR_DELETE=1` ayrıca iki turda
onarılamayan taslağı ve Gönderilenler'de karşılığı olan mükerreri **kalıcı siler** (geri
alınamaz); kapalıyken bunlar raporda "onarılamadı, elle düzelt" diye listelenir.

**Opsiyonel: veto pencereli otomatik gönderim** (`AUTO_SEND=1`, varsayılan **kapalı**):
denetimden ✅ geçen taslak bir gün kuyrukta bekler, raporda listelenir; **istemediğini
Gmail'den silersen gitmez**. Gönderimden hemen önce MX yeniden doğrulanır ve günlük
sert tavan (`AUTO_SEND_CAP`, varsayılan 5) uygulanır.

Üçü birlikte açıkken döngü kendi kendine döner: denetle → onar → gönder → yeni taslak üret.
Kapalıyken bekleyen taslak 12'yi geçtiğinde üretim durur ve taslakları elle göndermen gerekir.

Bütün guard modülleri ağsız self-test içerir:

```bash
python scripts/deliverability.py
python scripts/audit_drafts.py
python scripts/drafting.py
python scripts/numeric.py
python scripts/repair.py
python scripts/autosend.py
python scripts/report.py
python scripts/jobboard.py
python scripts/tracking.py
python scripts/board.py
python scripts/prep.py
python scripts/discover.py --self-test
```

---

## 9. Migration'ı Tekrar Çalıştırma

`migrate.py` idempotenttir: tekrar çalıştırınca CSV'den yeni satırları ekler, mevcutları günceller ama **senin dashboard'da girdiğin notları/durumları KORUR**. Sıfırdan kurmak için: `python src/migrate.py --force`.
