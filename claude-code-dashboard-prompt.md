# Claude Code Prompt — İş Arama Otomasyon Dashboard'u

Bu dosyayı olduğu gibi kopyalayıp Claude Code'a yapıştır (proje kök dizini: `C:\Users\Lenovo\Desktop\uygulamalar\is-arama-otomasyon`). Claude Code önce mevcut verileri okuyup şemayı çıkarsın, sonra aşağıdaki sistemi kursun.

---

## PROMPT (buradan itibaren Claude Code'a yapıştır)

Sen bir iş arama outreach sistemini dashboard + otomasyon haline getireceksin. Önce şu iki dosyayı oku ve gerçek şemalarını çıkar, hiçbir alanı varsayma:

- `C:\Users\Lenovo\Desktop\uygulamalar\is-arama-otomasyon\outreach_log.csv` (kolonlar: tarih, firma, sektor, kanal, proje, durum, eposta_veya_link)
- `C:\Users\Lenovo\Desktop\uygulamalar\is-arama-otomasyon\state.json` (profile, excluded_sectors, excluded_companies_seed, excluded_companies_seed_personal, companies_already_contacted, companies_logged_portal_only, processed_posting_urls, job_connectors, daily_caps)

Bu iki dosya mevcut sistemin tek doğruluk kaynağı. Hiçbir geçmiş veriyi silme/kaybetme — migrate et.

### 1) Veri katmanı

- CSV + JSON'u tek bir SQLite veritabanına (`tracker.db`) migrate et. Tablolar: `companies` (firma, sektor, kanal, proje_eslesme, durum, eposta_veya_link, tarih, draft_id, notlar, son_guncelleme), `profile` (state.json'daki profile objesini olduğu gibi tut — name, title, projects, role_filters, project_sector_mapping), `daily_stats` (tarih, taslak_sayisi, form_sayisi).
- `durum` alanı enum olsun: `TASLAK`, `ATS_DIGEST`, `ADAY`, `FORM_DOLDURULDU`, `ELENEN_SENIORITY`, `ELENEN_LOKASYON`, `ELENEN_SEKTOR`, `ELENEN_UYGUNSUZ`, `ELENEN_EPOSTA_BULUNAMADI`, `ELENEN_KISISEL`, `ARASTIRILMADI`, `BEKLIYOR` (kişisel/duygusal kararlar için — örn. Patientdesk.ai gibi tanıdık şirketler, "elenen" değil "bekliyor" olarak ayrı bir statü).
- CSV dosyasını da senkron tut (append-only log olarak kalsın, insan tarafından da okunabilir olması önemli) — DB birincil, CSV yedek/export.

### 2) Dashboard (web arayüzü)

Yerelde çalışan tek-kullanıcılı bir web app (öneri: Next.js + SQLite + Tailwind, ya da Python/FastAPI + basit HTML/HTMX — hangisi daha hızlı kurulursa). Özellikler:

- **Genel bakış paneli**: bugün kaç yeni temas, toplam şirket sayısı, durum bazlı dağılım (pasta/bar grafik), günlük taslak limiti göstergesi (limit config'den açılıp kapanabilsin, sabit kod olmasın).
- **Kanban görünümü**: durum kolonları halinde şirket kartları (TASLAK → ADAY → yanıt bekleniyor → yanıt geldi), sürükle-bırak ile manuel durum değiştirme.
- **Tablo görünümü**: filtrelenebilir (sektör, proje eşleşmesi, kanal, tarih aralığı, durum), aranabilir.
- **Şirket detay paneli**: tüm alanlar + serbest metin not alanı + "neden bu statüde" geçmişi (audit log — kim/ne zaman değiştirdi).
- **Gmail taslak linki**: draft_id varsa doğrudan `https://mail.google.com/mail/u/0/#drafts?compose=<id>` gibi bir linke tıklanabilsin.
- **Kişisel/ilişkisel durumlar için özel etiket**: bir şirketi "tanıdık/arkadaş şirketi" olarak işaretleyebilme, bu tür kayıtlar otomasyonun dışında tutulsun (otomatik mail atılmasın, sadece manuel karar beklesin).

### 3) Otomasyon pipeline'ı (arka plan işleri)

Benim (Claude/Cowork) elle yaptığım şu akışı script'e dök:

1. **Keşif**: yapılandırılabilir kaynak listesinden (İTÜ Çekirdek, Webrazzi, YC şirket dizini, startups.watch vb.) yeni şirket adayı topla. Web arama için bir arama API'si gerekecek (SerpAPI, Google Custom Search, veya Bing Search API) — hangisini kullanacağını kullanıcıya sor, hardcode etme.
2. **Profil eşleştirme**: `state.json > profile.role_filters` ve `project_sector_mapping`'i kullanarak junior/uygun rol filtresi uygula (zero_tolerance_rule'a harfiyen uy: senior/kıdemli anahtar kelimesi geçen hiçbir ilan hiçbir tabloya girmesin).
3. **İletişim doğrulama**: önce gerçek ATS linki (Lever/Greenhouse/Ashby/Workable) ara, yoksa doğrulanmış genel email (info@/hello@/careers@) ara. Kişiye özel tahmini email (isim.soyisim@) OLUŞTURMA — bu kural kritik, önceki oturumda yanlış email riski yüzünden özellikle uygulandı.
4. **Aksiyon**: ATS varsa `ATS_DIGEST` olarak logla (link + rol listesi). Email doğrulandıysa Gmail API ile TASLAK oluştur (asla otomatik gönderme — insan onayı şart, `send` değil `drafts.create` kullan). Form varsa doldur ama CAPTCHA'lı formlarda dur, kullanıcıya bırak.
5. **Yanıt takibi**: Gmail API ile `companies_already_contacted` içindeki thread'leri periyodik kontrol et, yanıt gelirse durumu güncelle ve dashboard'da bildirim göster.
6. **Günlük özet**: her çalıştırmada `daily_stats` tablosuna satır ekle, dashboard'da trend grafiği olarak göster.

### 4) Güvenlik / insan-onayı kuralları (değiştirilemez)

- Gmail'den **gerçek gönderim asla otomatik yapılmasın** — sadece taslak oluşturulsun, gönderme kullanıcının elinde kalsın.
- CAPTCHA'lı formlar otomatik geçilmeye çalışılmasın.
- `excluded_companies_seed_personal` listesindeki şirketler (kişisel tanıdıklar) pipeline'a hiç girmesin, ayrı bir manuel karar kutusunda dursun.
- `daily_caps` config'den açılıp kapanabilsin ama varsayılan olarak açık gelsin (spam gibi görünmemek için).

### 5) Teslim

- `README.md`: nasıl çalıştırılır (`npm install && npm run dev` veya eşdeğeri), Gmail API OAuth kurulumu adım adım, arama API key'i nereye konur.
- Migration script'i tek seferlik çalışsın, mevcut CSV/JSON'u kaybetmeden DB'ye aktarsın.
- Mevcut veriyi (bugüne kadar loglanan ~35+ şirket) ilk açılışta dashboard'da görebilmeliyim.

Başlamadan önce iki dosyayı okuyup şemayı bana özetle, sonra plana geç.

---

## Kullanım notu

Bu prompt'u Claude Code'a verirken proje klasörünü (`is-arama-otomasyon`) açık tutman yeterli. Arama API'si (SerpAPI/Google/Bing) ve Gmail OAuth için kendi API anahtarlarını girmen gerekecek — Claude Code sana nereye koyacağını README'de gösterecek.
