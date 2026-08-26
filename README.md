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
- **Şirketlere otomatik mail yok** — outreach taslakta kalır, gönderme kararı insanda.
- **Üç ayrı LLM adımı** (ele → yaz → *bağımsız* denetle) + deterministik sayı kontrolü;
  tek çağrıya "yaz ve kendini denetle" demek çalışmıyor.
- **E-posta tahmin edilmez** — adres sitede birebir geçmeli **ve** MX doğrulanmalı.
- **Teslim edilebilirlik guard'ı** — bounce oranı ölçülür; %6'yı aşarsa veya bekleyen
  taslak 12'yi geçerse yeni outreach otomatik durur.
- **Bekleyen taslak triyajı** — her run mükerrer + içerik denetimi yapıp "gönder / sil /
  düzelt" listesi çıkarır.

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

İlk açılışta bugüne kadar loglanan **67 şirket** dashboard'da görünür.

---

## 2. Dosya Yapısı

```
is-arama-otomasyon/
├── outreach_log.csv        # insan-okunur log (DB'den senkronlanır — elle de düzenlenebilir)
├── state.json              # profil + kurallar + geçmiş (migration kaynağı)
├── tracker.db              # SQLite — BİRİNCİL veri (migration üretir)
├── src/
│   ├── db.py               # şema + durum normalizasyonu
│   ├── migrate.py          # CSV+JSON -> DB (idempotent, güvenli)
│   ├── app.py              # stdlib web sunucu + JSON API
│   └── pipeline.py         # KARAR MOTORU (rol filtresi, email doğrulama)
└── web/index.html          # dashboard arayüzü (Tailwind + Chart.js CDN)
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
> Bulut ajanı (`outreachos-data`) Serper kullanıyor; ayrıntı için `SETUP.md`.
Windows PowerShell: `$env:SEARCH_API_KEY="..."`

> Anahtar yoksa keşif atlanır; dashboard ve mevcut 67 şirket sorunsuz çalışır.

### 6b. Gmail API (taslak oluşturma + yanıt takibi + kendine rapor)
Şirketlere **otomatik gönderim YOKTUR** — şirket outreach'i yalnızca `drafts.create` (taslak), gönderme kararı her zaman sende. Tek istisna: günlük özet **senin kendi adresine** rapor olarak gönderilir (`gmail.send`, hedef bağlı hesabın kendi adresi değilse reddedilir).

1. [Google Cloud Console](https://console.cloud.google.com/) → yeni proje → **Gmail API**'yi etkinleştir.
2. **OAuth consent screen** → External → kendi Gmail'ini test kullanıcısı ekle.
3. **Credentials → OAuth client ID → Desktop app** → `credentials.json` indir, proje köküne koy.
4. Kapsam (scope): `gmail.compose` (taslak) + `gmail.readonly` (yanıt takibi) + `gmail.send` (yalnızca kendine günlük rapor). Daha önce bağladıysan send yeni scope olduğu için Gmail'i **yeniden bağla**.
5. İlk çalıştırmada tarayıcıda onay verirsin; token `token.json`'a kaydolur.

> `credentials.json` ve `token.json` **asla commit edilmez** (gizli). CAPTCHA'lı formlar otomatik geçilmez — sana bırakılır.

---

## 7. Güvenlik Kuralları (değiştirilemez)

- ❌ **Şirketlere** otomatik gönderim yok — outreach sadece taslak. ✅ Tek istisna: günlük rapor yalnızca **senin kendi adresine** gider (`send_self_report`, dış adrese asla).
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

**Bekleyen taslak triyajı:** her run taslakları ✅ gönder / 🔁 sil (mükerrer) / ⚠️ düzelt /
👀 elle bak diye etiketleyip rapora yazar. Kararlar gövde hash'iyle cache'lenir.

**Opsiyonel — veto pencereli otomatik gönderim** (`AUTO_SEND=1`, varsayılan **kapalı**):
denetimden ✅ geçen taslak bir gün kuyrukta bekler, raporda listelenir; **istemediğini
Gmail'den silersen gitmez**. Gönderimden hemen önce MX yeniden doğrulanır ve günlük
sert tavan (varsayılan 5) uygulanır.

Bütün guard modülleri ağsız self-test içerir:

```bash
python scripts/deliverability.py && python scripts/audit_drafts.py && python scripts/autosend.py
```

---

## 9. Migration'ı Tekrar Çalıştırma

`migrate.py` idempotenttir: tekrar çalıştırınca CSV'den yeni satırları ekler, mevcutları günceller ama **senin dashboard'da girdiğin notları/durumları KORUR**. Sıfırdan kurmak için: `python src/migrate.py --force`.
