# Kurulum: sıfırdan kendi günlük taramanız

Tek seferlik kurulum, yaklaşık 30 dakika. Bittiğinde sistem her sabah GitHub Actions
üzerinde çalışır: şirket keşfeder, eler, e-postayı doğrular, Gmail'inize taslak bırakır ve
size günlük rapor yollar. Bilgisayarınızın açık olması gerekmez.

Aylık maliyet: Anthropic API birkaç dolar, GitHub Actions ve arama ücretsiz kotada.

Gerekenler: Python 3.10+, [GitHub CLI](https://cli.github.com/) (`gh auth login` yapılmış),
bir Gmail hesabı. `pip install` yok, kod sadece standart kütüphaneyi kullanır.

Aşağıdaki komutlarda `KULLANICI` yerine kendi GitHub kullanıcı adınızı yazın.

---

## 1) İki repo: kod (public) ve veri (private)

Kod bu repoda durur. Profiliniz, loglarınız ve gönderim geçmişiniz **ayrı ve private** bir
repoda durur; cron da orada çalışır. Böylece kişisel veriniz hiçbir zaman public repoya girmez.

```bash
gh repo fork tarikdeveci/outreachos --clone
```

```bash
gh repo create KULLANICI/outreachos-data --private --clone
```

Kodu değiştirmeyecekseniz fork şart değil; workflow doğrudan bu repoyu klonlayabilir.
Arama hedeflerini kendinize uyarlayacaksanız (aşağıda 7. bölüm) fork gerekir.

---

## 2) Profilinizi yazın

`state.example.json` dosyasını veri reponuza `state.json` olarak kopyalayın ve doldurun.

```bash
cp outreachos/state.example.json outreachos-data/state.json
```

Bu dosya LLM'in **tek gerçek kaynağıdır**: buraya yazmadığınız hiçbir deneyim, proje veya
sayı mailde geçemez, denetim katmanı da iddiaları bu dosyayla karşılaştırır. O yüzden:

- `profile.projects`: her proje için ne yaptığınızı ve ölçülebilir sonucu yazın.
- `profile.role_filters`: hangi unvanların eleneceği (kıdem, alan).
- `profile.cv_links` (isteğe bağlı): anahtar → CV linki. Birden çok CV'niz varsa anahtarı rolü
  anlatacak biçimde adlandırın; taslak adımı şirkete uyanı seçer, link mailin sonuna eklenir.
  Tek CV'niz varsa tek satır yazın, hiç yoksa alanı silin.
- `excluded_sectors`, `excluded_companies_seed`: hiç yazılmayacak sektör ve şirketler.
- `excluded_companies_seed_personal`: tanıdığınız şirketler (otomasyon dokunmaz).
- Abartmayın. Profilde olmayan bir iddia mailde çıkarsa taslak ⚠️ damgası alır.

---

## 3) Workflow'u kopyalayın

```bash
mkdir -p outreachos-data/.github/workflows && cp outreachos/.github/workflows/daily.example.yml outreachos-data/.github/workflows/daily.yml
```

Kopyada iki şeyi değiştirin:

1. Fork ettiyseniz `repository: tarikdeveci/outreachos` satırını `KULLANICI/outreachos` yapın.
2. `on:` altına cron'u ekleyin (şablonda bilerek yok, açıklaması dosyada):

```yaml
on:
  schedule:
    - cron: "0 5 * * *"      # 05:00 UTC = 08:00 Europe/Istanbul
  workflow_dispatch:
```

Sonra veri reposunu push edin:

```bash
cd outreachos-data && git add . && git commit -m "ilk kurulum" && git branch -M main && git push -u origin main
```

---

## 4) Secret'lar (hepsi veri reposunda)

Her komut değeri sorar; yapıştırın. Değer ekranda görünmez, repoya yazılmaz.

**Anthropic API anahtarı.** console.anthropic.com → API Keys → Create Key. Claude
aboneliğinden ayrı, kullandıkça öde bir hesaptır.

```bash
gh secret set ANTHROPIC_API_KEY --repo KULLANICI/outreachos-data
```

**Arama (Serper.dev).** serper.dev → Sign up (kart istemez, 2.500 sorgu hediye) → API Key.
Google'ın Custom Search JSON API'sini kullanmayın: yeni müşterilere kapalı, her çağrı 403 döner.

```bash
gh secret set SERPER_API_KEY --repo KULLANICI/outreachos-data
```

Anahtar yoksa script çökmez, DuckDuckGo yedeğine düşer; ama `site:` sorguları orada zayıftır.

**Gmail.** [Google Cloud Console](https://console.cloud.google.com/) üzerinde:

1. Yeni proje → **Gmail API**'yi etkinleştirin.
2. OAuth consent screen → External → kendi Gmail adresinizi **test user** olarak ekleyin.
3. Credentials → Create credentials → OAuth client ID → **Desktop app** → Client ID ve Secret.

```bash
gh secret set GMAIL_CLIENT_ID --repo KULLANICI/outreachos-data
```

```bash
gh secret set GMAIL_CLIENT_SECRET --repo KULLANICI/outreachos-data
```

Refresh token'ı bir kez, yerelde alın (script Client ID ve Secret'ı sorar, tarayıcı açar,
token'ı doğrudan GitHub secret'ına yazar; token ekranda görünmez):

```bash
OUTREACHOS_DATA_REPO=KULLANICI/outreachos-data python outreachos/scripts/get_gmail_token.py
```

İstenen izinler: `gmail.compose` (taslak açar, günceller, siler), `gmail.readonly` (yanıt ve
bounce takibi), `gmail.send` (günlük raporu **kendi adresinize** yollar; `AUTO_SEND` açıksa
onaylı taslakları gönderir). Rapor için hedef adres bağlı hesabın kendi adresi değilse
gönderim reddedilir.

---

## 5) Aday havuzunu toplayın (yerelde)

Türkiye'deki startup dizinleri GitHub Actions IP'lerini engelliyor, o yüzden havuz kendi
makinenizde toplanıp veri reposuna commit'lenir. Ayda bir tekrarlamak yeterli.

```bash
OUTREACHOS_DATA_DIR="$PWD/outreachos-data" python outreachos/scripts/refresh_pool.py
```

```bash
cd outreachos-data && git add candidate_pool.json && git commit -m "havuz tazelendi" && git push
```

---

## 6) İlk deneme

Önce kuru deneme: arama ve e-posta doğrulama çalışır, taslak oluşturulmaz, hiçbir şey kaydedilmez.

```bash
gh workflow run daily.yml --repo KULLANICI/outreachos-data -f dry_run=true
```

```bash
gh run watch --repo KULLANICI/outreachos-data
```

Log'da hangi kaynakta kaç şirkete bakıldığını ve hangi adresin neden elendiğini görürsünüz.
Her şey yolundaysa `-f dry_run=true` olmadan çalıştırın: taslaklar Gmail'inizde belirir,
özet hem mail hem de veri reposunda issue olarak gelir.

İlk haftalarda taslakları **elle okuyun**. Sistemin ne yazdığını görmeden otomatik
gönderimi açmayın.

---

## 7) Hedefi kendinize uyarlama

Şu an arama hedefi kodda sabit: **Türkiye ağırlıklı, junior yazılım / AI rolleri**. Alanınız
veya ülkeniz farklıysa fork'unuzda şu dört yeri değiştirin:

| Dosya | Ne | Ne işe yarar |
|---|---|---|
| `scripts/discover.py` | `SEED_DIRECTORIES` | Taranan startup dizinleri ve portföy sayfaları |
| `scripts/discover.py` | `todays_queries()` | Haftanın gününe göre arama sorguları |
| `scripts/drafting.py` | `JUDGE_SYSTEM` | Hangi şirketin "uygun" sayılacağı |
| `scripts/drafting.py` | `DRAFT_SYSTEM` | Mailin kimin ağzından, hangi tonda yazılacağı |

Rol ve kıdem filtreleri ise kodda değil, `state.json` içindedir (`profile.role_filters`).

---

## 8) Otomasyon anahtarları

Hepsi veri reponuzdaki `daily.yml` içinde, şablonda **kapalı** gelir. Kapalıyken sistem
sadece taslak üretir; hiçbir şey göndermez, hiçbir şey silmez.

| Anahtar | Açıkken ne yapar | Geri alınır mı |
|---|---|---|
| `AUTO_SEND` | Denetimden ✅ geçen taslağı, 1 gün veto penceresinden sonra şirkete gönderir. O gün taslağı Gmail'den silerseniz gitmez. | Gönderilen mail geri alınamaz |
| `AUTO_SEND_CAP` | Günlük gönderim tavanı (varsayılan 5) | |
| `AUTO_REPAIR` | ⚠️ damgalı taslağı onarır, sarmalanmış linkleri açar, sonucu yeniden denetler | Taslak yerinde değişir |
| `AUTO_REPAIR_DELETE` | İki turda onarılamayan ve mükerrer taslağı **kalıcı** siler | Hayır, çöp kutusuna gitmez |
| `REPAIR_MAX` | Run başına en fazla onarım denemesi (varsayılan 10) | |

Neden önemli: bekleyen taslak `MAX_PENDING_DRAFTS`'ı (12) geçince yeni üretim durur. Anahtarlar
kapalıyken taslakları sizin gönderip silmeniz gerekir; yoksa sistem birkaç günde kilitlenir.
Tam otomatik çalışma için üçü de (`AUTO_SEND`, `AUTO_REPAIR`, `AUTO_REPAIR_DELETE`) açık olmalı.
Üçü de yalnızca motorun kendi açtığı taslaklara dokunur; Gmail'de elle yazdığınız taslaklar
onarılmaz, silinmez, gönderilmez.

Önerilen sıra: birkaç gün hepsi kapalı → rapordaki ✅ / ⚠️ kararlarına güvenince `AUTO_REPAIR`
→ sonra `AUTO_SEND` → en son `AUTO_REPAIR_DELETE`.

Diğer ayarlar: `DAILY_TARGET` (günlük yeni taslak, 12), cron saati (UTC yazılır),
`BOUNCE_CRITICAL` (bounce bu oranı geçerse yeni outreach durur), `SCALEUP_BAND` (öne alınan
ekip büyüklüğü aralığı, varsayılan `20,300`), `NAMED_CONTACT` (varsayılan açık: sitede rolüyle
birlikte yayınlanmış kişi adresi genel kutunun önüne geçer; `0` yazarsanız yalnızca `info@`,
`careers@` gibi genel kutulara yazılır), `ATS_ADAY_LIMIT` (yazılacak adresi olmayan
şirketlerin ilan panosundan başvuru listesine bir run'da girebilecek yeni ilan, varsayılan
`8`; `0` yazarsanız bu şirketler eskisi gibi atlanır).

Run'dan sonra veri reponuzda `takip.md` oluşur: kimden yanıt geldi, kime cevap borçlusunuz,
hangi ilan yeni. Görüşme daveti gelen firma için hazırlık notu da aynı dosyaya ve o günün
raporuna yazılır. Bunların hiçbiri için bir şey işaretlemeniz gerekmez.

---

## E-posta doğrulama: ne çözer, ne çözmez

Adres iki testten geçmek zorunda:

1. Şirketin kendi sayfasının HTML'inde **birebir** geçiyor olmalı (tahmin yok).
2. Domainin **MX kaydı** doğrulanmalı (DNS-over-HTTPS).

Çözdüğü: uydurulmuş adres ve ölü domain. Çözmediği: sitede yayınlanan ama artık okunmayan
kutu. Bunu göndermeden önce anlamanın güvenilir yolu yok (GitHub Actions'ta 25. port
kapalı, catch-all sunucular yanlış olumlu verir). Bunun yerine script her run'da gelen
kutusundaki bounce'ları okur, adresi `email_dead` işaretler ve o şirketi bir daha denemez.
Her ölü adres en fazla bir kez maliyet çıkarır.

Mimarinin tamamı ve tasarım gerekçeleri: [ARCHITECTURE.md](ARCHITECTURE.md).
