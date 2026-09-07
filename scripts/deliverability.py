"""Mail sağlığı / teslim edilebilirlik guard'ı — bounce oranını ölçer, eşiği aşınca frenler.

Neden ayrı bir kat: mevcut sistem tek tek ölü adresi işaretliyordu ama TOPLAM bir
bounce oranı görmüyordu. Bir kaynak birden çok ölü/eski adres döktüğünde script körü
körüne taslak üretmeye devam ediyor; asıl Gmail itibarını yakan da bu. Bu modül gerçek
gönderilenler üzerinden oranı hesaplar, üç durumlu bir sağlık verir (İYİ/İZLEME/KRİTİK)
ve kritikte o günkü yeni outreach'i durdurur.

Ölçüm KAYNAĞI bilinçli olarak "gerçekten Gönderilenler'e düşen mailler":
  - payda: pencere içinde outreach adresine giden farklı alıcı (Gönderilenler'den).
  - pay:   pencere içinde hard-bounce alan farklı outreach adresi (mailer-daemon).
Taslak SAYISI payda DEĞİL — kullanıcı taslakların hepsini göndermiyor; bounce ancak
gerçekten gönderilen maile gelir.

Neden SMTP/RCPT probe yok: 25. port GitHub Actions'ta kapalı ve probe'un kendisi
itibara zarar verip bloklanmaya yol açabiliyor. Neden SPF/DKIM/DMARC kontrolü yok:
gönderen kimliği @gmail.com; bu kayıtlar Google'ın ve her zaman hizalı — kontrol etmek
sadece gürültü olurdu. Ölçülebilir ve anlamlı tek sinyal bounce/yanıt oranı.

Saf çekirdek (classify_bounce, circuit, banner, oran matematiği) ağ gerektirmez ve
dosyanın sonundaki __main__ bloğu ile kendini test eder: `python scripts/deliverability.py`.
"""
from __future__ import annotations

import time

# Ücretsiz/kişisel posta sağlayıcıları — bunlara giden mail outreach SAYILMAZ
# (self-report, kişisel yazışma paydayı şişirmesin).
FREE_MAIL = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "yandex.com", "yandex.ru", "icloud.com", "me.com", "proton.me",
    "protonmail.com", "aol.com", "gmx.com", "mail.com", "msn.com",
}

# Bounce sınıflandırma: geçici (soft) işaretler ÖNCE aranır; hiçbiri yoksa kalıcı (hard)
# varsayılır. Gerekçe: taranmış eski bir adrese gelen DSN'lerin çoğu kalıcı
# "user unknown"; geçici sorunlar genelde 4.x.x / quota / temporarily içerir.
SOFT_SIGNS = (
    "450", "451", "452", "4.2.2", "4.3.1", "4.4.", "4.7.",
    "quota", "over quota", "mailbox full", "kota", "temporar", "geçici",
    "try again", "tekrar dene", "greylist", "deferred", "rate limit",
    "too many", "busy", "timed out", "timeout",
)
HARD_SIGNS = (
    "550", "551", "553", "554", "5.1.1", "5.1.0", "5.0.0", "5.1.2", "5.4.",
    "user unknown", "no such user", "does not exist", "address not found",
    "recipient not found", "no mailbox", "mailbox not found", "unrouteable",
    "unknown recipient", "account has been disabled", "no longer", "invalid recipient",
    "kullanıcı bulunamadı", "adres bulunamadı",
)


def is_outreach_recipient(email: str, own: str = "") -> bool:
    """Bir alıcı outreach hedefi mi — kendisi değil, ücretsiz posta değil, geçerli @."""
    e = (email or "").strip().lower()
    if "@" not in e:
        return False
    if own and e == own.strip().lower():
        return False
    return e.split("@")[-1] not in FREE_MAIL


def classify_bounce(text: str) -> str:
    """Bir bounce metnini 'soft' (geçici) ya da 'hard' (kalıcı) diye sınıflar."""
    t = (text or "").lower()
    if any(s in t for s in SOFT_SIGNS):
        return "soft"
    return "hard"


def _rate_state(rate: float, recipients: int, watch: float, critical: float,
                min_sent: int) -> str:
    """Oran + örnek büyüklüğünden durum. Küçük örnekte (n<min) fren yok — yanlış alarm
    (ör. 1 bounce / 3 gönderim = %33) sistemi haksız yere durdurmasın."""
    if recipients < min_sent:
        return "INSUFFICIENT"
    if rate >= critical:
        return "CRITICAL"
    if rate >= watch:
        return "WATCH"
    return "OK"


def _trend(history: list[dict], rate: float) -> str:
    """Bugünkü oranı önceki (en fazla 3) run'ın ortalamasıyla kıyaslar."""
    prev = [h.get("rate") for h in (history or [])[-3:] if isinstance(h.get("rate"), (int, float))]
    if not prev:
        return "—"
    base = sum(prev) / len(prev)
    if rate > base + 0.005:
        return "↑ artıyor"
    if rate < base - 0.005:
        return "↓ düşüyor"
    return "→ sabit"


def assess(*, gmail_search, token, contacted: dict, sent_events: list,
           own: str = "", window_days: int = 30, watch: float = 0.03,
           critical: float = 0.06, min_sent: int = 12,
           history: list | None = None) -> dict:
    """Gmail'den gerçek teslim-edilebilirlik durumunu çıkarır.

    gmail_search: (query) -> [{from,subject,date,snippet}] (discover.gmail_search sarmalı).
    sent_events:  [(email, internal_ms)] — scan_sent'in tek geçişte topladığı gönderilenler.
    contacted:    state['companies_already_contacted'] (yanıt sayımı + adres eşlemesi için).
    token None ise ölçüm yapılamaz → durum 'UNKNOWN', fren devrede değil (fail-open).
    """
    now_ms = time.time() * 1000.0
    cutoff = now_ms - window_days * 86400_000.0

    # payda: pencere içinde farklı outreach alıcısı
    recipients = {e.lower() for (e, ms) in (sent_events or [])
                  if isinstance(ms, (int, float)) and ms >= cutoff and is_outreach_recipient(e, own)}

    # yanıt (pozitif sinyal): state'te YANIT işaretli kayıt sayısı — ekstra API yok
    replies = sum(1 for v in (contacted or {}).values()
                  if str(v.get("last_reply_seen") or "").startswith("YANIT"))

    if not token:
        return {"state": "UNKNOWN", "sent": len(recipients), "hard": 0, "soft": 0,
                "replies": replies, "bounce_rate": 0.0, "trend": "—",
                "window_days": window_days, "watch": watch, "critical": critical,
                "min_sent": min_sent, "note": "Gmail token yok — ölçüm atlandı"}

    # pay: mailer-daemon DSN'lerini oku, bilinen outreach adresine eşle, sınıfla, adrese göre dedup
    known = {v.get("email", "").lower(): k for k, v in (contacted or {}).items() if v.get("email")}
    hard: set[str] = set()
    soft: set[str] = set()
    for m in gmail_search(f"from:mailer-daemon newer_than:{window_days}d"):
        blob = ((m.get("snippet") or "") + " " + (m.get("subject") or "")).lower()
        hit = next((e for e in known if e and e in blob), None)
        if not hit or not is_outreach_recipient(hit, own):
            continue
        (soft if classify_bounce(blob) == "soft" else hard).add(hit)
    soft -= hard  # bir adres hem soft hem hard göründüyse kalıcı say

    denom = max(len(recipients), 1)
    rate = len(hard) / denom
    state = _rate_state(rate, len(recipients), watch, critical, min_sent)
    return {"state": state, "sent": len(recipients), "hard": len(hard), "soft": len(soft),
            "replies": replies, "bounce_rate": rate, "trend": _trend(history, rate),
            "window_days": window_days, "watch": watch, "critical": critical,
            "min_sent": min_sent, "note": ""}


def circuit(base_target: int, health: dict, pending_count: int, max_pending: int) -> tuple:
    """Guard'ın karar katı: (run_target, sebepler).

    - KRİTİK bounce  → 0 (yeni outreach durdurulur, itibar koruması).
    - bekleyen taslak > max_pending → 0 (önce backlog gönderilsin; ani hacim spike'ı önlenir).
    İZLEME sayıyı otomatik kısmaz (sadece uyarı) — fren, bekleyen-taslak kuralı.
    """
    reasons: list[str] = []
    t = base_target
    if health.get("state") == "CRITICAL":
        t = 0
        reasons.append(f"hard-bounce %{health['bounce_rate'] * 100:.1f} ≥ kritik "
                       f"%{health['critical'] * 100:.0f} — yeni outreach durduruldu")
    if pending_count > max_pending:
        t = 0
        reasons.append(f"Gmail'de {pending_count} bekleyen outreach taslağı > {max_pending} "
                       "— önce bekleyenleri gönder/temizle, sonra yeni taslak açılır")
    return max(t, 0), reasons


_LABEL = {"OK": ("🟢", "İYİ"), "WATCH": ("🟡", "İZLEME"), "CRITICAL": ("🔴", "KRİTİK"),
          "INSUFFICIENT": ("⚪", "YETERSİZ VERİ"), "UNKNOWN": ("⚪", "ÖLÇÜLEMEDİ")}


def banner(health: dict, pending_count: int, max_pending: int,
           run_target: int, base_target: int) -> list:
    """Rapor/issue/self-report için düz-metin sağlık bloğu (emoji + sayılar + eylem)."""
    emoji, label = _LABEL.get(health.get("state", "UNKNOWN"), ("⚪", "?"))
    rate = health.get("bounce_rate", 0.0) * 100
    w = health.get("window_days", 30)
    head = f"{emoji} Mail sağlığı: {label} — hard-bounce %{rate:.1f} (son {w}g)"
    if health.get("state") in ("OK", "WATCH", "CRITICAL"):
        head += f", trend {health.get('trend', '—')}"
    lines = [head,
             f"   gönderilen {health.get('sent', 0)} · hard-bounce {health.get('hard', 0)} · "
             f"soft {health.get('soft', 0)} · yanıt {health.get('replies', 0)} · "
             f"bekleyen taslak {pending_count}/{max_pending}"]
    if health.get("note"):
        lines.append(f"   not: {health['note']}")
    if health.get("state") == "CRITICAL":
        lines.append("   ⛔ Yeni outreach DURDURULDU. Bounce takibi + ATS + rapor devam ediyor.")
        lines.append("   yap: Gönderilenler'deki ölü adresleri ayıkla, birkaç gün yavaşla; "
                     "oran düşünce otomatik devam eder.")
    elif health.get("state") == "WATCH":
        lines.append("   ⚠ Oran yükseliyor — yeni taslak açmadan önce bekleyenleri gönder/temizle.")
    if pending_count > max_pending and health.get("state") != "CRITICAL":
        lines.append(f"   ⏸ Bekleyen taslak sınırı aşıldı ({pending_count}>{max_pending}) — "
                     "bugün yeni taslak açılmadı.")
    if run_target < base_target:
        lines.append(f"   → bugünkü hedef: {run_target}/{base_target}")
    return lines


def history_push(state: dict, health: dict, day: str, cap: int = 30) -> None:
    """Sağlık anlık görüntüsünü state'e ekler (trend için, son `cap` run)."""
    d = state.setdefault("deliverability", {})
    hist = d.setdefault("history", [])
    hist.append({"date": day, "sent": health.get("sent", 0), "hard": health.get("hard", 0),
                 "soft": health.get("soft", 0), "rate": round(health.get("bounce_rate", 0.0), 4),
                 "state": health.get("state", "UNKNOWN")})
    d["history"] = hist[-cap:]
    d["latest"] = d["history"][-1]


# --------------------------------------------------------------------- self-test
if __name__ == "__main__":
    # classify_bounce
    assert classify_bounce("550 5.1.1 user unknown") == "hard"
    assert classify_bounce("452 4.2.2 mailbox full, over quota") == "soft"
    assert classify_bounce("delivery failed, no such user") == "hard"
    assert classify_bounce("temporarily deferred, try again later") == "soft"

    # is_outreach_recipient
    assert is_outreach_recipient("careers@acme.io", "me@gmail.com")
    assert not is_outreach_recipient("me@gmail.com", "me@gmail.com")
    assert not is_outreach_recipient("someone@outlook.com")

    now = time.time() * 1000.0
    fresh = now - 5 * 86400_000.0            # 5 gün önce → pencere içinde
    old = now - 90 * 86400_000.0             # 90 gün önce → pencere dışı
    contacted = {
        "acme": {"email": "careers@acme.io", "last_reply_seen": "YANIT (2026-07-20): ..."},
        "dead1": {"email": "info@dead1.com", "last_reply_seen": None},
        "dead2": {"email": "hello@dead2.com", "last_reply_seen": None},
        "soft1": {"email": "jobs@soft1.io", "last_reply_seen": None},
    }
    sent = [("careers@acme.io", fresh), ("info@dead1.com", fresh), ("hello@dead2.com", fresh),
            ("jobs@soft1.io", fresh), ("me@gmail.com", fresh), ("x@old.com", old)]
    sent += [(f"t{i}@co{i}.com", fresh) for i in range(12)]   # örnek min_sent'i geçsin

    def fake_search(_q):
        return [
            {"snippet": "550 user unknown info@dead1.com", "subject": "Delivery Status", "date": "", "from": "mailer-daemon"},
            {"snippet": "hello@dead2.com does not exist 5.1.1", "subject": "failed", "date": "", "from": "mailer-daemon"},
            {"snippet": "jobs@soft1.io mailbox full 452 over quota", "subject": "delayed", "date": "", "from": "mailer-daemon"},
        ]

    h = assess(gmail_search=fake_search, token="x", contacted=contacted, sent_events=sent,
               own="me@gmail.com", window_days=30, watch=0.03, critical=0.06, min_sent=12,
               history=[{"rate": 0.02}])
    assert h["sent"] == 16, h["sent"]              # 4 gerçek + 12 sentetik, freemail+eski hariç
    assert h["hard"] == 2, h["hard"]               # dead1, dead2
    assert h["soft"] == 1, h["soft"]               # soft1
    assert h["replies"] == 1, h["replies"]
    assert abs(h["bounce_rate"] - 2 / 16) < 1e-6, h["bounce_rate"]
    assert h["state"] == "CRITICAL", h["state"]    # 2/16 = %12.5 > kritik %6

    # WATCH bandını da doğrula: 12 gönderimin 1'i hard = %8.3 (watch%3 ≤ x < ...)
    watch_sent = [(f"w{i}@wco{i}.com", fresh) for i in range(12)]
    watch_contacted = {"wco0": {"email": "w0@wco0.com", "last_reply_seen": None}}

    def watch_search(_q):
        return [{"snippet": "w0@wco0.com 550 user unknown", "subject": "failed", "date": "", "from": "mailer-daemon"}]

    hw = assess(gmail_search=watch_search, token="x", contacted=watch_contacted, sent_events=watch_sent,
                own="me@gmail.com", window_days=30, watch=0.03, critical=0.20, min_sent=12)
    assert hw["state"] == "WATCH", (hw["state"], hw["bounce_rate"])   # %8.3: watch ≤ x < critical(%20)
    assert circuit(12, hw, 3, 12)[0] == 12         # WATCH sayıyı kısmaz, sadece uyarır

    rt, why = circuit(12, h, pending_count=3, max_pending=12)
    assert rt == 0 and why, (rt, why)
    rt2, why2 = circuit(12, {"state": "OK", "bounce_rate": 0.0}, pending_count=20, max_pending=12)
    assert rt2 == 0 and why2, (rt2, why2)          # backlog freni
    rt3, _ = circuit(12, {"state": "OK", "bounce_rate": 0.0}, pending_count=3, max_pending=12)
    assert rt3 == 12

    # küçük örnek → fren yok
    h_small = assess(gmail_search=fake_search, token="x", contacted=contacted,
                     sent_events=[("info@dead1.com", fresh), ("hello@dead2.com", fresh)],
                     own="me@gmail.com", min_sent=12)
    assert h_small["state"] == "INSUFFICIENT", h_small["state"]
    assert circuit(12, h_small, 3, 12)[0] == 12    # yetersiz veri durdurmaz

    # token yok → fail-open
    h_not = assess(gmail_search=fake_search, token=None, contacted=contacted, sent_events=sent, own="me@gmail.com")
    assert h_not["state"] == "UNKNOWN"
    assert circuit(12, h_not, 3, 12)[0] == 12

    b = banner(h, 3, 12, 0, 12)
    assert any("KRİTİK" in x for x in b) and any("DURDURULDU" in x for x in b)
    print("deliverability self-test: OK")
