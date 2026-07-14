# Design

## Theme
Dark "operatör terminali", **ember/mercan** varyantı. Sıcak koyu kömür yüzeyler (hafif warm tint, chroma ~0.008 @40°), tek bir coral-mercan sinyal rengi, veriler için monospace. Sakin, keskin, kişisel. Işık: gece, ateş-parıltısı. (Not: yeşil/lime, kullanıcının PreventA projesiyle çakıştığı için terk edildi.)

## Color (OKLCH)
Ruh coral sinyal + sıcak kömürde. Yüzeylerde çok hafif warm tint (mercan tarafına doğru), nötr-soğuk değil.

| Rol | OKLCH | Kullanım |
|-----|-------|----------|
| `--bg` | `oklch(0.185 0.008 40)` | En derin zemin |
| `--surface` | `oklch(0.225 0.008 40)` | Paneller, kartlar |
| `--surface-2` | `oklch(0.265 0.008 40)` | Girişler, hover, yükseltilmiş |
| `--line` | `oklch(0.32 0.006 40)` | Kenarlıklar |
| `--ink` | `oklch(0.965 0.004 50)` | Birincil metin |
| `--muted` | `oklch(0.72 0.006 40)` | İkincil metin (AA ≥4.5:1) |
| `--faint` | `oklch(0.56 0.008 40)` | Üçüncül / meta |
| `--signal` | `oklch(0.70 0.17 30)` | Birincil aksiyon, aktif, odak, ADAY (coral) |
| `--signal-ink` | `oklch(0.18 0.01 40)` | Signal üzeri metin |

### Durum (kategorik — ADAY coral sinyal; ELENEN çakışmasın diye taupe'a alındı)
| Durum | OKLCH |
|-------|-------|
| TASLAK | `oklch(0.80 0.13 75)` (amber) |
| ATS_DIGEST | `oklch(0.75 0.09 200)` (soğuk teal — coral'a kontrast) |
| ADAY | `oklch(0.70 0.17 30)` (signal coral) |
| FORM_DOLDURULDU | `oklch(0.73 0.13 300)` (violet) |
| BEKLIYOR | `oklch(0.84 0.11 95)` (gold) |
| ARASTIRILMADI | `oklch(0.66 0.01 60)` (nötr sıcak gri) |
| ELENEN_* | `oklch(0.55 0.035 45)` (muted taupe — coral'dan uzak) |

## Typography
- **UI/gövde**: system sans stack — `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`. Tek aile; ağırlık kontrastıyla hiyerarşi (400/500/600/700).
- **Veri/sayı**: monospace — `ui-monospace, "JetBrains Mono", "SF Mono", "Cascadia Code", monospace`. Metrikler, tarihler, sayaçlar, ilan sayıları `font-variant-numeric: tabular-nums`.
- Ölçek sabit rem (fluid değil): 12 / 13 / 14 / 16 / 20 / 28 / 40. Oran ~1.2. Splash h1 istisna: `clamp(2rem, 5vw, 3.4rem)` (tavan ≤6rem).
- Display tracking: -0.02em (floor -0.04'ün üstünde). Gövde 0.

## Components
- **Kart/panel**: `--surface`, 1px `--line`, radius 12px (kartlar 12–14; tag/pill full). Gölge yok ya da tek tanımlı: `0 1px 2px oklch(0 0 0 / .3)`. Border+geniş-gölge birlikte YASAK.
- **Buton**: primary = signal zemin + signal-ink; ghost = şeffaf + 1px line; her ikisi hover/focus/active/disabled. Radius 10px.
- **Rozet (durum)**: durum renginin %14 zemini + tam renk metin; küçük, radius full, etiket metinli (renk-only değil).
- **Odak**: `outline: 2px solid --signal; outline-offset: 2px`.
- **Kanban kolon/kart**: kart hover'da `--surface-2` + 1px signal ipucu; sürüklenirken cursor grabbing; drop hedefi signal kesikli çizgi.
- **Boş durum**: arayüzü öğreten kısa metin, "kayıt yok" değil.

## Motion
- 150–250ms, `cubic-bezier(0.22, 1, 0.36, 1)` (ease-out-quint). Bounce/elastic yok.
- Yalnızca durum: hover, focus, drawer aç/kapa (transform+opacity), sekme geçişi (opacity), rozet/kart giriş (stagger, kısa).
- Drawer: sağdan kayar (translateX) + backdrop fade. Splash: hero + kart girişleri kısa stagger.
- `@media (prefers-reduced-motion: reduce)`: tüm geçişler anlık/crossfade.

## Layout
- Dashboard: üst bar (marka + sekme nav + çıkış) + içerik. Maks genişlik 1360px, 20px gutter.
- KPI şeridi: `repeat(auto-fit, minmax(150px,1fr))`. Grafik/panel grid 2 kolon (md), 1 (mobil).
- Kanban: yatay kaydırılabilir kolonlar, 288px sabit genişlik.
- Responsive yapısaldır: nav sekmeleri daralır, grid kolonları breakpoint ile iner, tablo yatay kaydırır.
