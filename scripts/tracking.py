"""Günlük takip, okuma tarafı: gelen maili sınıflar ve firma kayıtlarına işler.

Kullanıcıya SORMADAN çalışır:

  1. Gelen maili sınıflar: görüşme daveti, ret, otomatik yanıt, insan yanıtı, gürültü.
     Eskiden yazışılan domainden gelen HER mail "yanıt" sayılıyordu (bülten ve müşteri
     hizmeti maili dahil) ve domainlerin yalnızca rastgele 20'sine bakılıyordu.
  2. Bizim açtığımız konuşmaya gelen yanıtı, gönderen başka domainden olsa da yakalar.
  3. Başvuru onayı, ret ve davet maillerini (ATS'lerden gelenler) ayrı bir deftere yazar.

Bu kayıtlardan panoyu `board.py` kurar. Ağ çağrıları enjekte edilir
(`get`: url -> bytes | None), çekirdek saftır.
Dosya sonundaki __main__ kendini test eder: `python scripts/tracking.py`
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import urlencode

import autosend

API = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
TR = timezone(timedelta(hours=3))
GUN_MS = 86_400_000
APPS_KEY = "applications_seen"       # mesaj id → başvuru maili kaydı
READY_KEY = "takip_kuruldu"          # ilk tarama yapıldı mı (ilk run geniş pencereyle okur)
WINDOW_DAYS, FIRST_DAYS = 30, 120

GORUSME, RET, OTOMATIK, YANIT, GURULTU = "GORUSME", "RET", "OTOMATIK", "YANIT", "GURULTU"
ETIKET = {GORUSME: "görüşme daveti", RET: "ret", OTOMATIK: "otomatik yanıt", YANIT: "yanıt"}

RET_KESIN = (
    "not moving forward", "not be moving forward", "move forward with other", "other candidates",
    "decided not to", "not to proceed", "not be proceeding", "regret to inform", "not been selected",
    "not selected", "position has been filled", "no longer accepting", "not hiring",
    "no open positions", "no open roles", "no openings", "don't have any open",
    "do not have any open", "olumsuz", "değerlendiremiyoruz", "değerlendiremeyeceğiz",
    "ilerleyemiyoruz", "ilerleyemeyeceğiz", "üzülerek", "başka adaylarla",
    "açık pozisyonumuz bulunmamakta", "açık pozisyonumuz yok", "uygun pozisyon bulunmamakta",
    "alım yapmıyoruz")
RET_ZAYIF = ("unfortunately", "maalesef")
# "Uygun bulunursa görüşmeye çağırırız" bir davet değil, alındı bildirimidir.
KOSULLU = (
    "uygun bulunması halinde", "uygun görülmesi halinde", "uygun bulunması durumunda",
    "olumlu değerlendirilmesi", "iletişime geçeceğiz", "iletişime geçilecektir",
    "dönüş yapılacaktır", "dönüş yapacağız", "if your profile matches", "if your qualifications",
    "if your background", "if there is a match", "if there's a match", "if we see a fit",
    "should your profile", "if selected", "if you are selected", "shortlisted",
    "we will contact you", "we'll be in touch", "we will be in touch")
GORUSME_KESIN = ("interview", "mülakat")
GORUSME_IZ = GORUSME_KESIN + (
    "görüşme", "görüşelim", "tanışalım", "tanışma toplantısı", "schedule a call",
    "schedule a time", "book a time", "book a call", "quick call", "intro call", "hop on a call",
    "jump on a call", "have a chat", "love to chat", "love to talk", "let's talk",
    "calendly.com", "cal.com/", "meet.google.com", "google meet", "zoom.us", "teams.microsoft",
    "invitation:", "davet:", "davetiye", "are you available", "your availability", "müsait",
    "uygun olduğunuz")
OTOMATIK_IZ = (
    "your application", "thank you for applying", "thanks for applying", "for your interest",
    "application received", "we have received", "we received your", "get back to you",
    "başvurunuz", "yaptığınız başvuru", "başvuru için teşekkür", "ilginiz için teşekkür",
    "göstermiş olduğunuz ilgi", "değerlendirmeye alınmıştır", "en kısa sürede dönüş",
    "out of office", "ofis dışında", "automatic reply", "auto-reply", "autoreply",
    "otomatik yanıt", "type your reply above this line", "has been received",
    "business hours", "a member of our team")
GURULTU_IZ = (
    "unsubscribe", "abonelikten", "view in browser", "tarayıcıda görüntüle", "newsletter",
    "bülten", "webinar", "değerli müşterimiz", "dear customer", "siparişiniz", "your order",
    "faturanız", "invoice", "receipt", "verification code", "doğrulama kodu", "security alert",
    "güvenlik uyarısı", "kampanya", "indirim")
BOT_KUTUSU = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "newsletter",
              "news@", "marketing", "notification", "mailer", "updates@", "digest")
# Elle gönderilmiş (sent_scan) bir mail başvuru mu, destek yazışması mı: konu satırından.
_IS = re.compile(
    r"(?<![a-zçğıöşü])(?:engineer|developer|intern|position|roles?\b|appl(?:y|ication)|opportunit|"
    r"hiring|career|jobs?\b|resume|cv\b|candidate|software|backend|frontend|full.?stack|mühendis|"
    r"geliştirici|staj|pozisyon|başvuru|kariyer|özgeçmiş|yazılım|iş ?birliği|tanışma|ekibinize)")
_IS_DEGIL = ("destek talebi", "arıza", "support request", "ticket", "refund", "iade", "garanti",
             "warranty", "sipariş", "fatura", "invoice", "case ")

def _low(s: str) -> str:
    """Türkçe güvenli küçük harf: 'İ'.lower() birleşik noktalı bir 'i' üretir, desen tutmaz."""
    return (s or "").replace("İ", "i").lower()


def _clean(s: str) -> str:
    """Gmail özetindeki görünmez dolgu karakterlerini (ön başlık boşlukları) atar."""
    return re.sub(r"\s+", " ",
                  re.sub("[\u00ad\u034f\u200b-\u200f\u2800\ufeff]", "", s or "")).strip()


def _norm(s: str) -> str:
    return "".join(ch for ch in _low(s) if ch.isalnum())


def _day(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, TR).strftime("%Y-%m-%d") if ms else "?"


def _iso_ms(day) -> int:
    try:
        return int(datetime.strptime(str(day)[:10], "%Y-%m-%d").replace(tzinfo=TR).timestamp() * 1000)
    except ValueError:
        return 0


def is_job(subject: str) -> bool:
    t = _low(subject)
    return not any(p in t for p in _IS_DEGIL) and bool(_IS.search(t))


def classify(subject: str, snippet: str, sender: str = "", in_thread: bool = False,
             bulk: bool = False, auto: bool = False) -> str:
    """Gelen bir maili sınıflar. in_thread: bizim açtığımız konuşmaya geldi (gürültü olamaz).
    bulk: toplu gönderim başlığı taşıyor. auto: otomatik yanıt başlığı taşıyor."""
    t = _low(f"{subject} {snippet}")

    def has(pats) -> bool:
        return any(p in t for p in pats)

    if has(RET_KESIN):
        return RET
    if has(KOSULLU):
        return OTOMATIK
    if not in_thread:
        if bulk:
            if has(GORUSME_KESIN):
                return GORUSME
            if has(RET_ZAYIF):
                return RET
            return OTOMATIK if has(OTOMATIK_IZ) else GURULTU
        if has(GURULTU_IZ):
            return GURULTU
    if has(GORUSME_IZ):
        return GORUSME
    if has(RET_ZAYIF):
        return RET
    if auto or has(OTOMATIK_IZ):
        return OTOMATIK
    if not in_thread and any(b in _low(sender) for b in BOT_KUTUSU):
        return GURULTU
    return YANIT


# ------------------------------------------------------------------ Gmail okuma (enjekte)
def list_ids(get, query: str, cap: int = 500) -> list:
    """Sorguya uyan mesajların (id, threadId) çiftleri: en yeni önce, en fazla `cap`."""
    out, page = [], ""
    while len(out) < cap:
        raw = get(API + "?" + urlencode({"q": query, "maxResults": min(500, cap - len(out))})
                  + (f"&pageToken={page}" if page else ""))
        if not raw:
            break
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            break
        out += [(m["id"], m.get("threadId")) for m in data.get("messages", []) if m.get("id")]
        page = data.get("nextPageToken", "")
        if not page:
            break
    return out[:cap]


META_HEADERS = ("From", "Subject", "List-Unsubscribe", "Precedence", "Auto-Submitted")


def read_meta(get, mid: str) -> dict | None:
    raw = get(f"{API}/{mid}?format=metadata"
              + "".join(f"&metadataHeaders={h}" for h in META_HEADERS))
    if not raw:
        return None
    try:
        d = json.loads(raw)
        ms = int(d.get("internalDate", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    h = {x.get("name", "").lower(): x.get("value", "")
         for x in d.get("payload", {}).get("headers", [])}
    auto = h.get("auto-submitted", "").lower()
    return {"id": mid, "from": h.get("from", ""), "subject": h.get("subject", ""),
            "snippet": _clean(html.unescape(d.get("snippet", ""))), "ms": ms,
            "bulk": bool(h.get("list-unsubscribe")) or "auto-generated" in auto
            or h.get("precedence", "").lower() in ("bulk", "list", "junk"),
            "auto": "auto-replied" in auto}


def _firm_of(addr: str, index: dict, free_mail) -> str | None:
    """Gönderen adresi → firma. mail.acme.com gibi alt alan, acme.com kaydına bağlanır."""
    key = autosend.target_key(addr, free_mail)
    parts = key.split(".")
    while key not in index and "@" not in key and len(parts) > 2:
        parts = parts[1:]
        key = ".".join(parts)
    return index.get(key)


def fetch_replies(get, contacted: dict, sent_threads: dict, free_mail=frozenset(),
                  days: int = WINDOW_DAYS, max_meta: int = 200) -> list:
    """Yazıştığımız firmalardan gelen mailler (firma ve in_thread eklenmiş meta listesi).

    İki kaynak: (a) bizim açtığımız konuşmalara gelen her mail (gönderen başka domainden
    olsa da yakalanır), (b) yazıştığımız adres ya da domainden gelen, yeni konuşma açan
    mailler. sent_threads: threadId → o konuşmada yazdığımız adres."""
    index: dict = {}
    for firma, v in contacted.items():
        if not isinstance(v, dict) or not v.get("email"):
            continue
        key = autosend.target_key(v["email"], free_mail)
        if "@" in key and v.get("channel") == "sent_scan":
            continue                      # elle yazılmış kişisel mail, başvuru değil
        index.setdefault(key, firma)
    seen: set = set()
    out: list = []

    def take(mid: str, hint, in_thread: bool) -> None:
        if mid in seen or len(seen) >= max_meta:
            return
        seen.add(mid)
        m = read_meta(get, mid)
        addr = parseaddr(m["from"])[1].lower() if m else ""
        if not addr or "mailer-daemon" in addr or "postmaster" in addr:
            return                        # bounce'u track_replies ayrıca işliyor
        firma = _firm_of(addr, index, free_mail) or hint
        if firma:
            out.append({**m, "firma": firma, "in_thread": in_thread})

    for mid, th in list_ids(get, f"-from:me -in:chats newer_than:{days}d", cap=3000):
        to = sent_threads.get(th)
        if to:
            take(mid, _firm_of(to, index, free_mail), True)
    # Küçük gruplar: tek bir kalabalık domain (bildirim yağdıran) komşularını boğmasın.
    keys = sorted(index)
    for i in range(0, len(keys), 5):
        q = ("from:(" + " OR ".join(keys[i:i + 5]) + f") newer_than:{days}d "
             "-category:promotions -category:social -category:forums")
        for mid, _th in list_ids(get, q, cap=25):
            take(mid, None, False)
    return out


def judge_manual(contacted: dict, get, free_mail=frozenset(), cap: int = 20) -> int:
    """Elle gönderilmiş kayıtları bir kez sınıflar: yazdığımız mailin konusu başvuru mu?
    Karar kayda yazılır (`basvuru`); destek kaydı gibi başvuru olmayan yazışma panoya girmez."""
    n = 0
    for rec in contacted.values():
        if n >= cap:
            break
        if not isinstance(rec, dict) or rec.get("channel") != "sent_scan" or "basvuru" in rec:
            continue
        key = autosend.target_key(rec.get("email") or "@", free_mail)
        if "@" in key:
            continue                      # kişisel adres: zaten takip edilmiyor
        metas = [read_meta(get, mid) for mid, _th in list_ids(get, f"in:sent to:{key}", cap=3)]
        if any(metas):                    # liste boşsa (hata olabilir) karar yazılmaz, yine sorulur
            rec["basvuru"] = any(is_job(m["subject"]) for m in metas if m)
            n += 1
    return n


def fetch_applications(get, known: dict, skip=frozenset(), days: int = 60, cap: int = 60) -> list:
    """Başvuru onayı, ret ve davet mailleri (konu satırından). known/skip: yeniden okunmaz."""
    q = (f"-from:me newer_than:{days}d subject:(application OR applying OR applied OR "
         "başvuru OR başvurunuz OR başvurusu OR candidature)")
    out = []
    for mid, _th in list_ids(get, q, cap=cap):
        if mid in known or mid in skip:
            continue
        m = read_meta(get, mid)
        if m:
            out.append(m)
    return out


# ------------------------------------------------------------------ yanıtları state'e işleme
_ESKI = re.compile(r"^YANIT \(([^)]*)\): ?(.*)$", re.S)


def migrate_legacy(contacted: dict) -> int:
    """Eski 'YANIT (tarih): özet' kayıtlarını sınıflar; gürültü çıkanın yanıt izini siler."""
    n = 0
    for rec in contacted.values():
        if not isinstance(rec, dict) or rec.get("yanit_turu"):
            continue
        m = _ESKI.match(str(rec.get("last_reply_seen") or ""))
        if not m:
            continue
        ozet = _clean(html.unescape(m.group(2)))
        tur = classify("", ozet)
        if tur == GURULTU:
            rec["last_reply_seen"] = None
        else:
            try:
                ms = int(datetime.strptime(m.group(1).strip(), "%a, %d %b %Y")
                         .replace(tzinfo=TR).timestamp() * 1000)
            except ValueError:
                ms = 0
            rec.update(yanit_turu=tur, yanit_ms=ms, yanit_ozet=ozet[:200], yanit_eski=True)
        n += 1
    return n


def _verdict(msgs: list) -> dict | None:
    """Bir firmanın mailleri → durumu belirleyen mail. Hepsi gürültüyse None.
    Ret, kendisinden sonra davet ya da insan yanıtı gelmediyse kesindir; yoksa davet,
    o da yoksa insan yanıtı, o da yoksa son otomatik yanıt."""
    real = [m for m in msgs if m["tur"] != GURULTU]

    def last(tur):
        return max((m for m in real if m["tur"] == tur), key=lambda m: m["ms"], default=None)

    ret, gor, yan = last(RET), last(GORUSME), last(YANIT)
    if ret and all(ret["ms"] >= m["ms"] for m in (gor, yan) if m):
        return ret
    return gor or yan or max(real, key=lambda m: m["ms"], default=None)


def apply_replies(contacted: dict, replies: list, window_start_ms: int = 0) -> list:
    """Sınıflanmış yanıtları firma kayıtlarına yazar; YENİ olayların notlarını döndürür."""
    by: dict = {}
    for m in replies:
        m["tur"] = classify(m.get("subject", ""), m.get("snippet", ""), m.get("from", ""),
                            m.get("in_thread", False), m.get("bulk", False), m.get("auto", False))
        by.setdefault(m["firma"], []).append(m)
    notes: list = []
    for firma, msgs in by.items():
        rec = contacted.get(firma)
        if not isinstance(rec, dict):
            continue
        v = _verdict(msgs)
        if v and rec.get("channel") == "sent_scan" and not rec.get("basvuru"):
            if v["tur"] != GORUSME:
                continue                  # başvuru olmayan elle yazışma; yalnızca davet sayılır
            rec["basvuru"] = True
        eski = bool(rec.get("yanit_eski"))
        onceki = int(rec.get("yanit_ms") or 0)
        if v is None:
            # Penceredeki tek "yanıt" gürültüymüş: eski sınıflamasız kayıt da oydu, sil.
            if eski and onceki >= window_start_ms:
                for k in ("yanit_turu", "yanit_ms", "yanit_ozet", "yanit_kimden", "yanit_eski"):
                    rec.pop(k, None)
                if "BOUNCE" not in str(rec.get("last_reply_seen") or ""):
                    rec["last_reply_seen"] = None
            continue
        if not eski and v["ms"] <= onceki and rec.get("yanit_turu") == v["tur"]:
            continue
        # Eski kaydın saati yok (gün başı yazıldı); aynı günün maili yeni olay sayılmaz.
        yeni = v["ms"] > onceki + (GUN_MS if eski else 0)
        kimden = parseaddr(v.get("from", ""))
        ozet = f"{v.get('subject', '').strip()}: {v.get('snippet', '').strip()}".strip(": ")
        rec.update(yanit_turu=v["tur"], yanit_ms=v["ms"], yanit_ozet=ozet[:200],
                   yanit_kimden=(kimden[0] or kimden[1])[:80])
        rec.pop("yanit_eski", None)
        if "BOUNCE" not in str(rec.get("last_reply_seen") or ""):
            rec["last_reply_seen"] = f"YANIT ({_day(v['ms'])}): {v.get('snippet', '')[:160]}"
        if yeni:
            notes.append(f"**{firma}**: {ETIKET[v['tur']]}: {v.get('subject', '')[:80]}")
    return notes


def apply_applications(state: dict, apps: list, now_ms: int, keep_days: int = 120) -> int:
    """Başvuru maillerini kalıcı deftere yazar (gürültü de yazılır: bir daha okunmasın)."""
    seen = state.setdefault(APPS_KEY, {})
    added = 0
    for m in apps:
        tur = classify(m["subject"], m["snippet"], m["from"], False, m.get("bulk", False),
                       m.get("auto", False))
        kimden = parseaddr(m["from"])
        seen[m["id"]] = {"ms": m["ms"], "tur": tur, "kimden": (kimden[0] or kimden[1])[:80],
                         "konu": m["subject"][:140]}
        added += tur != GURULTU
    for k in [k for k, v in seen.items() if now_ms - int(v.get("ms") or 0) > keep_days * GUN_MS]:
        del seen[k]
    return added


def sync(state: dict, get, sent_threads: dict, free_mail=frozenset(), now_ms: int = 0,
         today: str = "") -> list:
    """Gmail'den yanıtları ve başvuru maillerini okuyup state'e işler; yeni olay notları döner.
    İlk çalıştırmada geniş pencere okunur ki eski kayıtlar gerçek maillerden yeniden sınıflansın."""
    contacted = state.setdefault("companies_already_contacted", {})
    migrate_legacy(contacted)
    judge_manual(contacted, get, free_mail)
    days = WINDOW_DAYS if state.get(READY_KEY) else FIRST_DAYS
    replies = fetch_replies(get, contacted, sent_threads, free_mail, days)
    notes = apply_replies(contacted, replies, now_ms - days * GUN_MS)
    apps = fetch_applications(get, state.get(APPS_KEY, {}), {m["id"] for m in replies})
    apply_applications(state, apps, now_ms)
    state[READY_KEY] = state.get(READY_KEY) or today
    return notes


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    c = classify
    assert c("Re: Merhaba", "Propose Develops Google Meet ile katılın: pazartesi") == GORUSME
    assert c("", "Thanks a lot for your interest in joining us! Just a quick heads-up") == OTOMATIK
    assert c("", "İlginiz için teşekkür ederiz, başvurunuz değerlendirmeye alınmıştır") == OTOMATIK
    assert c("", "Başvurunuz uygun bulunması halinde görüşme için aranacaksınız") == OTOMATIK
    assert c("Update", "Unfortunately we decided to move forward with other candidates") == RET
    assert c("", "Thanks for interviewing. We will not be moving forward") == RET
    assert c("", "Unfortunately I am away, can we schedule a call next week?") == GORUSME
    assert c("", "Değerli Müşterimiz, hizmetiniz tamamlandı") == GURULTU
    assert c("What's new", "Get the new beta and design kits", bulk=True) == GURULTU
    assert c("Application received", "We received your application", bulk=True) == OTOMATIK
    assert c("Re: Hello", "Get the new beta", in_thread=True) == YANIT       # konuşmamızda gürültü olmaz
    assert c("Hi", "Could you share your CV?", sender="Ada <ada@acme.io>") == YANIT
    assert c("Hi", "Product news", sender="Acme <no-reply@acme.io>") == GURULTU
    assert c("Automatic reply", "I am out", auto=True) == OTOMATIK
    assert c("", "##- Please type your reply above this line -## Your request has been received") == OTOMATIK
    assert c("", "Thank you for reaching out! Our business hours are 9am to 9pm") == OTOMATIK
    assert c("", "Thanks for reaching out, could you share your CV?", in_thread=True) == YANIT
    assert _clean("Get the beta. \u034f \u2800\u2800 ") == "Get the beta."

    # sahte Gmail: liste sorguları ve meta okuma
    D = GUN_MS
    now = _iso_ms("2026-10-04") + D // 2
    box = {
        "m1": ("t1", "Ada <ada@acme.io>", "Re: Hello", "Can we have a chat on Tuesday?", now - 2 * D, {}),
        "m2": ("t9", "News <news@acme.io>", "Monthly", "Our newsletter", now - D, {"List-Unsubscribe": "<x>"}),
        "m3": ("t2", "Jobs <jobs@ats.example>", "Beta update", "we will not be moving forward", now - 3 * D, {}),
        "m4": ("t7", "Mail <x@mail.gama.co>", "Hello", "Could you send your portfolio?", now - 4 * D, {}),
        "m5": ("t8", "Hire <no-reply@ats.example>", "Your application to Delta Labs",
               "Thank you for applying", now - D, {}),
        "m6": ("t5", "X <mailer-daemon@googlemail.com>", "Delivery failure", "550", now - D, {}),
    }

    def fake_get(url: str):
        if "format=metadata" in url:
            mid = url.split("/messages/")[1].split("?")[0]
            th, frm, sub, snip, ms, extra = box[mid]
            heads = [{"name": "From", "value": frm}, {"name": "Subject", "value": sub}]
            heads += [{"name": k, "value": v} for k, v in extra.items()]
            return json.dumps({"threadId": th, "snippet": snip, "internalDate": str(ms),
                               "payload": {"headers": heads}}).encode()
        if "in%3Asent" in url:
            ids = [k for k in box if k.startswith("s:") and k[2:] in url]
        elif "subject%3A" in url:
            ids = ["m5", "m1"]
        elif "from%3A%28" in url:
            ids = ["m1", "m2", "m4", "m10"]
        else:
            ids = list(box)
        return json.dumps({"messages": [{"id": i, "threadId": box[i][0]} for i in ids]}).encode()

    free = frozenset({"gmail.com"})
    st = {"companies_already_contacted": {
        "acme": {"email": "hi@acme.io", "channel": "autosend_vetted", "date": "2026-09-20",
                 "last_reply_seen": None},
        "beta": {"email": "ik@beta.dev", "channel": "autosend_vetted", "date": "2026-09-20",
                 "last_reply_seen": None},
        "gama": {"email": "info@gama.co", "channel": "sent_manual_reviewed", "date": "2026-09-01",
                 "last_reply_seen": None},
        "eski": {"email": "a@eski.com", "channel": "autosend_vetted", "date": "2026-07-01",
                 "last_reply_seen": "YANIT (Fri, 18 Sep 2026): Değerli Müşterimiz, hizmetiniz"},
        "bltn": {"email": "a@bltn.com", "channel": "autosend_vetted", "date": "2026-07-01",
                 "last_reply_seen": "YANIT (Mon, 3 Aug 2026 ): Tarık Bey merhaba, yaptığınız başvuru"},
        "tslk": {"email": "a@tslk.com", "channel": "gmail_draft_speculative", "date": "2026-10-02",
                 "draft_id": "D1", "last_reply_seen": None},
        "kyp": {"email": "a@kyp.com", "channel": "gmail_draft_speculative", "date": "2026-08-02",
                "draft_id": "D2", "last_reply_seen": None},
        "git": {"email": "a@git.com", "channel": "gmail_draft_speculative", "date": "2026-08-02",
                "draft_id": "D3", "last_reply_seen": None},
        "olu": {"email": "a@olu.com", "channel": "autosend_vetted", "date": "2026-08-02",
                "email_dead": True, "last_reply_seen": "BOUNCE (x): adres teslim edilemedi"},
        "arkadas": {"email": "biri@gmail.com", "channel": "sent_scan", "date": "sent_detected",
                    "last_reply_seen": None}}}
    box["m7"] = ("t6", "Biri <biri@gmail.com>", "Re: selam", "akşam görüşelim mi", now - D, {})
    # elle gönderilmiş üç kayıt: destek yazışması, başvuru, konusu belirsiz ama davet gelen
    box["s:destek.io"] = ("x1", "me", "Scroll Tekerleği Arızası: Destek Talebi", "", now - 9 * D, {})
    box["s:elle.io"] = ("x2", "me", "Yazılım Geliştirici Pozisyonu Hakkında", "", now - 9 * D, {})
    box["s:davet.io"] = ("x3", "me", "Merhaba", "", now - 9 * D, {})
    box["m8"] = ("t10", "Help <help@destek.io>", "Re: Destek", "Ürününüz kargoya verildi", now - D, {})
    box["m9"] = ("t11", "IK <ik@elle.io>", "Re: Pozisyon", "Portfolyonuzu iletir misiniz?", now - D, {})
    box["m10"] = ("t12", "Takvim <x@davet.io>", "Davetiye: Meeting Davet, Pzt 5 Eki", "", now - D, {})
    cc = st["companies_already_contacted"]
    for ad, adres in (("destek", "help@destek.io"), ("elle", "ik@elle.io"), ("davet", "x@davet.io")):
        cc[ad] = {"email": adres, "channel": "sent_scan", "date": "sent_detected"}
    threads = {"t1": "hi@acme.io", "t2": "ik@beta.dev", "t5": "a@olu.com", "t6": "biri@gmail.com",
               "t10": "help@destek.io", "t11": "ik@elle.io"}
    assert is_job("AI Engineer / EdTech Product Role") and not is_job("Lenovo Case 2031826834")
    notes = sync(st, fake_get, threads, free, now, "2026-10-04")
    assert [cc[k]["basvuru"] for k in ("destek", "elle", "davet")] == [False, True, True]
    assert [cc[k].get("yanit_turu") for k in ("destek", "elle", "davet")] == [None, YANIT, GORUSME]
    assert cc["acme"]["yanit_turu"] == GORUSME, cc["acme"]          # bülten (m2) durumu bozmadı
    assert cc["beta"]["yanit_turu"] == RET                           # başka domainden, konuşmamızda
    assert cc["gama"]["yanit_turu"] == YANIT                         # alt alandan gelen yeni konuşma
    assert cc["eski"]["last_reply_seen"] is None and "yanit_turu" not in cc["eski"]
    assert cc["bltn"]["yanit_turu"] == OTOMATIK and cc["bltn"]["yanit_eski"]
    assert "yanit_turu" not in cc["olu"]                             # bounce maili yanıt değil
    assert "yanit_turu" not in cc["arkadas"]                         # kişisel yazışma takip edilmez
    assert len(notes) == 5 and st[READY_KEY] == "2026-10-04", notes
    assert [a["tur"] for a in st[APPS_KEY].values()] == [OTOMATIK]   # m1 firma yanıtı, tekrar yok
    assert sync(st, fake_get, threads, free, now, "2026-10-05") == []  # aynı mail ikinci kez not olmaz
    print("tracking self-test: OK")
