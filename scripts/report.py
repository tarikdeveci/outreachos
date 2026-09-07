"""Günlük self-report: ATS başvuru listesini toplar ve özeti kullanıcının KENDİ adresine yollar.

Ayrım (güvenlik): şirketlere otomatik mail YOK — outreach yalnızca taslak kalır.
Buradaki `send_self_report` SADECE self-report içindir; hedef adres bağlı Gmail
hesabının kendi adresiyle eşleşmezse gönderimi reddeder (own_address doğrulaması),
yani profildeki kendi adresin dışına bir tek mail bile gitmez.

Bağımlılık enjeksiyonu ile döngüsel import'tan kaçınılır: gather_ats_digest, arama ve
rol-filtresi fonksiyonlarını parametre olarak alır (discover.py'nin search / pipeline).
"""
from __future__ import annotations

import base64
import json
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

# ATS board rotasyonu — junior/uygun ilanlar. discover.py'nin search()'ü ile aranır.
# Startup ATS'leri (firma adı URL path'inden çıkar) + KURUMSAL kaynaklar (LinkedIn Jobs,
# Indeed, kariyer.net, Workday, SmartRecruiters) — havuz %100 startup olduğu için
# kurumsal ilanlar SADECE bu digest'ten geliyor; kullanıcı bunlara kendi başvuruyor.
STARTUP_ATS_HOSTS = ("lever.co", "greenhouse.io", "ashbyhq.com", "workable.com")
STARTUP_ATS_QUERIES = [
    'site:jobs.lever.co (junior OR "new grad" OR intern) (software OR AI OR ML OR data) 2026',
    "site:boards.greenhouse.io junior (software engineer OR AI OR machine learning) 2026",
    "site:jobs.ashbyhq.com (junior OR associate) (software OR AI OR product) 2026",
    "site:apply.workable.com junior (developer OR engineer OR AI OR product) 2026",
]
CORPORATE_QUERIES = [
    'site:linkedin.com/jobs junior (software OR AI OR "machine learning" OR data) engineer '
    "(İzmir OR İstanbul OR remote OR Türkiye) 2026",
    'site:tr.indeed.com (junior OR "yeni mezun") (yazılım OR software OR AI) mühendis',
    'site:kariyer.net (junior OR "yeni mezun") (yazılım OR "yapay zeka" OR backend OR "full stack") mühendis',
    "site:myworkdayjobs.com (graduate OR junior) (software OR AI OR data) engineer 2026",
    "site:jobs.smartrecruiters.com junior (software OR AI OR data) engineer 2026",
]
ATS_QUERIES = STARTUP_ATS_QUERIES + CORPORATE_QUERIES


# ATS host'larında tekil ilan değil, jenerik uç nokta olan path parçaları.
# Gözlenen vaka: arama sonucunda çıkan boards.greenhouse.io/embed/job_app her gün
# rapora "Embed" adlı bir firmaymış gibi giriyordu — tıklanacak bir ilan değil.
GENERIC_ATS_SEGMENTS = {"embed", "job_app", "jobs", "job", "search", "apply", "applications",
                        "careers", "career", "job-boards", "collections", "signup", "login"}


def is_real_posting(link: str) -> bool:
    """Link tekil bir ilana mı işaret ediyor, yoksa ATS'in jenerik uç noktasına mı.

    Digest'in değeri 'tıkla ve başvur' olmasında; firma kök sayfası ya da arama
    sonucu sayfası listeye girerse kullanıcı boşuna tıklıyor ve aynı çöp kayıt
    her gün tekrar ediyor (dedup firma+başlık ile çalıştığı için elenmiyor)."""
    try:
        u = urlparse(link)
    except ValueError:
        return False
    if u.scheme not in ("http", "https") or not u.netloc:
        return False
    parts = [p for p in u.path.split("/") if p]
    if not parts:
        return False
    host = u.netloc.lower()
    # LinkedIn ÖNCE: tekil ilan yolu da /jobs/ ile başlar, jenerik listeye takılmasın.
    if "linkedin.com" in host:
        return "/jobs/view/" in u.path  # /jobs/search ve /jobs/collections ilan değil
    if parts[0].lower() in GENERIC_ATS_SEGMENTS:
        return False
    if any(h in host for h in STARTUP_ATS_HOSTS):
        return len(parts) >= 2          # jobs.lever.co/<firma>/<ilan> — tek parça firma sayfası
    if "indeed." in host:
        return bool(u.query) or len(parts) >= 2   # viewjob?jk=... veya /rc/clk/...
    return len(parts) >= 2 or bool(u.query)


def _ats_company(link: str) -> str:
    """İlan linkinden firma adını çıkarır. Startup ATS'lerinde firma URL path'inde
    (jobs.lever.co/<firma>/...); kurumsal kaynaklarda (LinkedIn/Indeed/kariyer.net)
    path firma vermez, o yüzden host'a düşülür."""
    try:
        host = urlparse(link).netloc.lower()
        parts = [p for p in urlparse(link).path.split("/") if p]
    except ValueError:
        return ""
    if any(h in host for h in STARTUP_ATS_HOSTS) and parts:
        return parts[0].replace("-", " ").title()
    # kurumsal: <firma>.myworkdayjobs.com gibi alt alan varsa onu al, yoksa host
    host = host.replace("www.", "")
    sub = host.split(".")[0]
    return sub.title() if sub not in ("jobs", "boards", "apply", "tr", "linkedin", "indeed") else host


def gather_ats_digest(search_fn, role_ok, log, limit: int = 12) -> list[dict]:
    """ATS board'larından uygun (role_ok(title) True) ilanları toplar.
    (firma, title, link) listesi döndürür; firma+başlık ile dedup eder.
    search_fn: discover.search gibi (query)->[{title,link,snippet}] bir fonksiyon."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for q in ATS_QUERIES:
        for r in search_fn(q):
            link = r.get("link", "")
            title = (r.get("title") or "").split(" - ")[0].split(" | ")[0].split(" at ")[0].strip()
            if not link or not title:
                continue
            if not is_real_posting(link):
                continue
            firma = _ats_company(link)
            key = (firma.lower(), title.lower())
            if key in seen:
                continue
            if not role_ok(title):
                continue
            seen.add(key)
            out.append({"firma": firma, "title": title, "link": link})
            if len(out) >= limit:
                log.append(f"  ATS digest: {len(out)} ilan (limite ulaşıldı)")
                return out
    log.append(f"  ATS digest: {len(out)} uygun ilan")
    return out


def linkedin_search_url(company: str, role: str) -> str:
    """Firma + rol için hazır LinkedIn kişi-arama linki. Kullanıcı tıklar, gerçek
    kişileri görür, kendisi bağlantı/mesaj atar — scraping veya otomatik mesaj YOK."""
    return ("https://www.linkedin.com/search/results/people/?keywords="
            + quote(f"{company} {role}".strip()) + "&origin=GLOBAL_SEARCH_HEADER")


def build_linkedin_targets(drafted, limit: int = 5) -> list:
    """CV'ye en uygun (en yüksek skorlu) ilk `limit` firma için LinkedIn kişi hedefleri.
    Günde en fazla `limit` firma = aynı anda 50 kişiye junk mesaj yok, hesap güvende.
    Profil taramaz, otomatik mesaj atmaz; her firma için hazır arama linki + not verir."""
    ranked = sorted([d for d in drafted if d.get("hedef_kisiler")],
                    key=lambda d: d.get("skor") or 0, reverse=True)[:limit]
    return [{"firma": d.get("firma", ""), "skor": d.get("skor"),
             "roller": [{"rol": r, "url": linkedin_search_url(d.get("firma", ""), r)}
                        for r in d["hedef_kisiler"]],
             "mesaj": d.get("linkedin_mesaji", "")} for d in ranked]


def build_report_text(day, profile, drafted, ats, reply_notes, skipped, banner_lines=None,
                      audit_lines=None, send_lines=None) -> str:
    """Kullanıcıya gidecek düz-metin günlük rapor. Saf fonksiyon (test edilebilir)."""
    name = profile.get("name", "")
    lines = [f"İş Arama Otomasyonu — Günlük Rapor ({day})", ""]
    lines.append(f"Merhaba {name.split()[0] if name else ''}, bugünkü tarama özeti aşağıda.")
    lines.append("Bu mail KENDİ adresinden gönderildi; şirketlere hiçbir otomatik mail gitmedi —")
    lines.append("outreach mailleri Gmail > Taslaklar'da senin onayını bekliyor.")
    lines.append("")

    if banner_lines:
        lines += list(banner_lines) + [""]
    if audit_lines:
        lines += ["== Bekleyen taslak triyajı (mükerrer + içerik) =="] + list(audit_lines) + [""]
    if send_lines:
        lines += ["== Otomatik gönderim (veto pencereli) =="] + list(send_lines) + [""]

    lines.append(f"== ATS/Portal üzerinden SEN başvuracaksın ({len(ats)}) ==")
    if ats:
        for a in ats:
            lines.append(f"  • {a['firma']} — {a['title']}")
            lines.append(f"      {a['link']}")
    else:
        lines.append("  (bugün uygun yeni ATS ilanı bulunamadı)")
    lines.append("")

    lines.append(f"== Gmail'de hazır bekleyen taslaklar ({len(drafted)}) ==")
    if drafted:
        for d in drafted:
            score = d.get("skor")
            tag = f" — uygunluk %{score}" if score else ""
            lines.append(f"  • {d['firma']} ({d.get('domain', '')}){tag} → {d.get('email', '')}")
            if d.get("skor_gerekce"):
                lines.append(f"      neden: {d['skor_gerekce']}")
            if d.get("kariyer"):
                lines.append(f"      kendi başvuru sayfası: {d['kariyer']}")
            if d.get("hedef_kisiler"):
                lines.append(f"      LinkedIn'de hedefle: {', '.join(d['hedef_kisiler'])}")
            if d.get("linkedin_mesaji"):
                lines.append(f"      LinkedIn notu: {d['linkedin_mesaji']}")
    else:
        lines.append("  (bugün yeni taslak açılmadı)")
    lines.append("")

    li = build_linkedin_targets(drafted)
    lines.append(f"== LinkedIn — bugün elle ulaş (en uygun {len(li)} firma; hesabın güvende) ==")
    if li:
        for t in li:
            lines.append(f"  • {t['firma']} (%{t['skor']} uygun)")
            for r in t["roller"]:
                lines.append(f"      {r['rol']}: {r['url']}")
            if t["mesaj"]:
                lines.append(f"      hazır not: {t['mesaj']}")
    else:
        lines.append("  (skorlu taslak yok — bugün LinkedIn hedefi çıkmadı)")
    lines.append("")

    if reply_notes:
        lines.append(f"== Gelen yanıt / bounce ({len(reply_notes)}) ==")
        for n in reply_notes:
            lines.append(f"  • {n.replace('**', '')}")
        lines.append("")

    lines.append(f"Elenen/uygunsuz: {len(skipped)} kayıt (ayrıntı: gunluk_ozet/{day}.md).")
    lines.append("")
    lines.append("— outreachos günlük otomasyonu")
    return "\n".join(lines)


def own_address(token: str) -> str:
    """Bağlı Gmail hesabının kendi adresi — self-report hedefinin doğrulaması için."""
    raw = _get_auth("https://gmail.googleapis.com/gmail/v1/users/me/profile", token)
    if not raw:
        return ""
    try:
        return json.loads(raw).get("emailAddress", "")
    except json.JSONDecodeError:
        return ""


def send_self_report(to: str, subject: str, body: str, token: str) -> str | None:
    """Raporu KULLANICININ KENDİ adresine yollar. Bağlı hesabın adresiyle eşleşmezse
    gönderim yapmaz (yanlışlıkla dışarı mail atmaya karşı güvenlik kilidi)."""
    own = (own_address(token) or "").lower()
    if not own or to.lower() != own:
        print(f"    ! Rapor gönderilmedi: hedef ({to}) bağlı hesapla ({own or '?'}) eşleşmiyor.")
        return None
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
    data = json.dumps({"raw": raw}).encode()
    req = Request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                  data=data, method="POST",
                  headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=25) as r:
            return json.load(r).get("id")
    except (HTTPError, URLError, OSError) as e:
        detail = e.read().decode(errors="replace")[:200] if isinstance(e, HTTPError) else ""
        hint = " (gmail.send izni yok — token'ı yeni scope ile yenile)" \
            if (isinstance(e, HTTPError) and e.code in (401, 403)) else ""
        print(f"    ! Rapor maili gönderilemedi: {e}{hint} {detail}")
        return None


def _get_auth(url: str, token: str) -> bytes | None:
    req = Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urlopen(req, timeout=20) as r:
            return r.read()
    except (HTTPError, URLError, OSError):
        return None


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    # is_real_posting: tekil ilan kabul, jenerik uç nokta ret
    for u in ("https://jobs.lever.co/shyftlabs/1a665cf5-1f3f-4610-be00-b46ffa13a675",
              "https://boards.greenhouse.io/stripe/jobs/6789",
              "https://jobs.ashbyhq.com/openai/8f2a-4c1d",
              "https://apply.workable.com/acme/j/ABC123/",
              "https://www.linkedin.com/jobs/view/4012345678",
              "https://tr.indeed.com/viewjob?jk=abc123",
              "https://www.kariyer.net/is-ilani/acme-junior-yazilim-3456789",
              "https://acme.myworkdayjobs.com/External/job/Istanbul/Software-Engineer_R-1"):
        assert is_real_posting(u), u
    for u in ("https://boards.greenhouse.io/embed/job_app",          # gözlenen çöp vaka
              "https://boards.greenhouse.io/embed/job_app?token=99",
              "https://jobs.lever.co/palantir",                       # sadece firma sayfası
              "https://www.linkedin.com/jobs/search?keywords=junior",
              "https://www.linkedin.com/jobs/collections/recommended",
              "https://tr.indeed.com/jobs?q=junior",                  # arama sayfası
              "https://jobs.ashbyhq.com/",
              "https://apply.workable.com/careers/",
              "not-a-url"):
        assert not is_real_posting(u), u

    # _ats_company: startup ATS'te firma path'ten, kurumsalda alt alan/host'tan
    assert _ats_company("https://jobs.lever.co/acme-labs/x") == "Acme Labs"
    assert _ats_company("https://acme.myworkdayjobs.com/External/job/x") == "Acme"

    # gather_ats_digest: çöp link listeye girmemeli, rol filtresi uygulanmalı
    results = [
        {"title": "Junior Software Engineer - Acme", "link": "https://jobs.lever.co/acme/u1"},
        {"title": "Greenhouse Software", "link": "https://boards.greenhouse.io/embed/job_app"},
        {"title": "Junior AI Engineer", "link": "https://jobs.lever.co/beta"},   # ilan değil
        {"title": "Senior Staff Engineer", "link": "https://jobs.lever.co/gamma/u2"},
    ]
    log: list = []
    out = gather_ats_digest(lambda q: results if q == ATS_QUERIES[0] else [],
                            lambda t: "senior" not in t.lower(), log)
    assert [o["firma"] for o in out] == ["Acme"], out

    # aynı firma+başlık iki kez gelirse tek kayıt
    dup = [results[0], dict(results[0], link="https://jobs.lever.co/acme/u9")]
    out2 = gather_ats_digest(lambda q: dup if q == ATS_QUERIES[0] else [], lambda t: True, [])
    assert len(out2) == 1, out2

    print("report self-test: OK")
