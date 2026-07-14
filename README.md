# İş Arama Otomasyon Dashboard

Tarık Deveci'nin iş arama outreach sistemini **yerel, tek-kullanıcılı bir dashboard + karar motoruna** dönüştürür.
Sıfır bağımlılık — sadece **Python 3** (stdlib) gerekir. `npm install` / `pip install` yok.

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
# seçeneklerden BİRİ:
export SEARCH_PROVIDER=serpapi   && export SEARCH_API_KEY="..."   # SerpAPI
export SEARCH_PROVIDER=google    && export SEARCH_API_KEY="..." && export SEARCH_CX="..."  # Google Custom Search
export SEARCH_PROVIDER=bing      && export SEARCH_API_KEY="..."   # Bing Search
```
Windows PowerShell: `$env:SEARCH_API_KEY="..."`

> Anahtar yoksa keşif atlanır; dashboard ve mevcut 67 şirket sorunsuz çalışır.

### 6b. Gmail API (taslak oluşturma + yanıt takibi)
**Otomatik GÖNDERİM YOKTUR** — yalnızca `drafts.create`. Gönderme kararı her zaman sende.

1. [Google Cloud Console](https://console.cloud.google.com/) → yeni proje → **Gmail API**'yi etkinleştir.
2. **OAuth consent screen** → External → kendi Gmail'ini test kullanıcısı ekle.
3. **Credentials → OAuth client ID → Desktop app** → `credentials.json` indir, proje köküne koy.
4. Kapsam (scope): `https://www.googleapis.com/auth/gmail.compose` (taslak) + `gmail.readonly` (yanıt takibi).
5. İlk çalıştırmada tarayıcıda onay verirsin; token `token.json`'a kaydolur.

> `credentials.json` ve `token.json` **asla commit edilmez** (gizli). CAPTCHA'lı formlar otomatik geçilmez — sana bırakılır.

---

## 7. Güvenlik Kuralları (değiştirilemez)

- ❌ Gmail'den otomatik **gönderim yok** — sadece taslak.
- ❌ CAPTCHA otomatik geçilmez.
- ❌ Kişisel tanıdık şirketleri (`excluded_companies_seed_personal`) pipeline'a girmez — Kişisel/Bekleyen sekmesinde manuel karar bekler.
- ✅ `daily_caps` config'den açılıp kapanabilir; varsayılan **açık** (Genel Bakış'taki "aktif" anahtarı).

---

## 8. Migration'ı Tekrar Çalıştırma

`migrate.py` idempotenttir: tekrar çalıştırınca CSV'den yeni satırları ekler, mevcutları günceller ama **senin dashboard'da girdiğin notları/durumları KORUR**. Sıfırdan kurmak için: `python src/migrate.py --force`.
