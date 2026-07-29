"""
Builds the analysis prompt from a snapshot dict and calls the Groq LLM to
generate the final Indonesian-language market report.

Note: prompt content (SYSTEM_PROMPT, MACRO_RULES, output template) is kept
in Indonesian intentionally, since the LLM must reason and respond in
Indonesian — keeping the instructions in the same language as the required
output reduces the risk of the model losing precision in translation.
"""

import logging
from datetime import datetime

from groq import Groq
from utils.api_key_manager import APIKeyRotator
from config import (
    GROQ_API_KEYS,
    GROQ_MODEL,
)

logger = logging.getLogger(__name__)
groq_rotator = APIKeyRotator(GROQ_API_KEYS, service_name="Groq")

NEGATION_WORDS = [
    "collapse", "fail", "broken", "end", "resume", "resumes",
    "violat", "gagal", "runtuh", "berakhir", "dilanggar", "kembali serang",
]

# Confidence notes from macro_fetcher that indicate the actual value may not
# be the true first-print (see macro_fetcher._assess_timing_confidence).
STALE_CONFIDENCE_NOTES = {
    "possibly_revised": "gap rilis→publish >72 jam, kemungkinan bukan angka first print",
    "unknown": "timing rilis tidak bisa divalidasi (release_time/published_at hilang)",
}


# ============================================================
# FORMAT HELPERS
# ============================================================

def safe_fmt_pct(val):
    if val is None:
        return "N/A"
    try:
        return f"{float(val):+.2f}%"
    except (ValueError, TypeError):
        return "N/A"


def check_snapshot_freshness(data: dict) -> str:
    """Flag market assets using a stale (>1 day old) closing price."""
    snapshot_time_str = data.get("timestamp")
    if not snapshot_time_str:
        return "⚠️ Snapshot tanpa timestamp, tidak bisa validasi freshness."

    try:
        if "+" in snapshot_time_str or "Z" in snapshot_time_str:
            snapshot_time = datetime.fromisoformat(snapshot_time_str.replace("Z", "+00:00"))
        else:
            snapshot_time = datetime.fromisoformat(snapshot_time_str)
    except ValueError:
        return "⚠️ Format timestamp tidak dikenali, skip freshness check."

    stale_assets = []
    fresh_assets = []

    for k, v in data.get("market_prices", {}).items():
        status = v.get("market_status", "")
        if "CLOSED" in status:
            fetch_date_str = v.get("fetch_date")
            if fetch_date_str:
                try:
                    fetch_dt = datetime.fromisoformat(fetch_date_str)
                    gap_days = (snapshot_time.date() - fetch_dt.date()).days
                    if gap_days >= 1:
                        stale_assets.append(f"{k} (last close: {fetch_date_str}, {gap_days} hari lalu)")
                except ValueError:
                    stale_assets.append(f"{k} (fetch_date format tidak dikenali: {fetch_date_str})")
        else:
            fresh_assets.append(k)

    if stale_assets:
        return (
            "⚠️ **PERINGATAN DATA BASI**: Aset berikut memakai closing price lama (market tutup), BUKAN harga hari ini:\n"
            + "\n".join(f"  - {a}" for a in stale_assets)
            + f"\nAset real-time: {', '.join(fresh_assets) if fresh_assets else 'TIDAK ADA'}.\n"
            "→ JANGAN klaim pergerakan 'hari ini' untuk CLOSED. Sebut 'closing terakhir' saja.\n"
        )
    return "✅ Semua aset dalam kondisi fresh (tidak ada data basi lebih dari 1 hari)."


# ============================================================
# MACRO ACTUALS
# ============================================================

def parse_macro_number(val):
    """Parse values like '65K', '3.7%', '148' into a comparable float."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().upper().replace("%", "").replace("K", "")
    try:
        return float(s)
    except ValueError:
        return None


def classify_surprise_local(actual, forecast, eps: float = 1e-9) -> str:
    """
    Compute BEAT/MISS/IN LINE locally from actual vs forecast.
    Do NOT use the API's surprise_score — it has been observed to reflect a
    stale vintage of `actual` rather than the currently displayed value.
    """
    a = parse_macro_number(actual)
    f = parse_macro_number(forecast)
    if a is None or f is None:
        return "N/A"
    diff = a - f
    if diff > eps:
        return "BEAT"
    elif diff < -eps:
        return "MISS"
    return "IN LINE"


def check_macro_staleness(macro_actuals: dict) -> str:
    warnings = []
    for alias, info in macro_actuals.items():
        if info.get("status") != "ok":
            continue
        note = info.get("confidence_note")
        if note in STALE_CONFIDENCE_NOTES:
            warnings.append(f"{alias}: {STALE_CONFIDENCE_NOTES[note]}")

    if warnings:
        return (
            "⚠️ **DATA MAKRO PERLU DIVALIDASI**:\n"
            + "\n".join(f"  - {w}" for w in warnings)
            + "\n→ JANGAN sebut angka ini sebagai hasil rilis final tanpa disclaimer.\n"
        )
    return ""


def format_macro_actuals(macro_actuals: dict) -> str:
    """
    Show the raw confidence_note per line, plus an explicit instruction per
    category. This prevents the LLM from generalizing ("most items say
    'needs validation' so this one probably does too") — each line carries
    its own ground truth that must be read literally.
    """
    lines = ""
    for alias, info in macro_actuals.items():
        if info.get("status") != "ok":
            lines += f"• {alias}: ERROR - {info.get('detail', 'unknown error')}\n"
            continue

        actual = info.get("actual")
        forecast = info.get("forecast")
        label = classify_surprise_local(actual, forecast)
        release_date = (info.get("release_time") or "")[:10] or "N/A"
        note = info.get("confidence_note", "unknown")

        if note in STALE_CONFIDENCE_NOTES:
            instruction = " → INSTRUKSI: WAJIB tulis 'perlu divalidasi' untuk item ini."
        elif note == "uncertain":
            instruction = " → INSTRUKSI: JANGAN tulis 'perlu divalidasi' untuk item ini (confidence uncertain, bukan possibly_revised/unknown)."
        elif note == "likely_first_print":
            instruction = " → INSTRUKSI: Data ini valid/first print, JANGAN tulis 'perlu divalidasi'."
        else:
            instruction = ""

        lines += (
            f"• {alias}: {actual} (vs forecast {forecast}) → {label} "
            f"[rilis: {release_date}] "
            f"confidence_note={note}{instruction}\n"
        )

    return lines or "Tidak ada data macro actuals di snapshot ini.\n"


def format_macro_calendar(macro_calendar: dict, max_per_alias: int = 2) -> str:
    lines = ""
    has_event = False
    for alias, events in macro_calendar.items():
        for ev in events[:max_per_alias]:
            release_time = (ev.get("release_time") or "")[:16].replace("T", " ")
            impact = ev.get("impact", "N/A")
            title = ev.get("title", alias)
            lines += f"• [{release_time}] {title} (impact: {impact})\n"
            has_event = True

    if not has_event:
        return "Tidak ada rilis makro high-impact dalam beberapa hari ke depan di data ini."
    return lines


def format_macro_alerts(macro_alerts: list) -> str:
    if not macro_alerts:
        return ""

    lines = ""
    tag_map = {
        "value_changed": "♻️ REVISI",
        "first_observed": "🔔 RILIS BARU",
        "incomplete_data": "⚠️ DATA TIDAK LENGKAP",
        "fetch_error": "❌ GAGAL FETCH",
    }
    for a in macro_alerts:
        tag = tag_map.get(a.get("status"), "ℹ️")
        alias = a.get("alias", "?")
        if a.get("status") == "value_changed":
            lines += f"• {tag} [{alias}]: {a.get('old_actual')} → {a.get('new_actual')}\n"
        else:
            lines += f"• {tag} [{alias}]: actual={a.get('actual')}\n"

    if lines:
        return "⚠️ **PERUBAHAN DATA MAKRO TERBARU** (perhatikan sebelum menyimpulkan tren):\n" + lines
    return ""


def classify_vix_sentiment(prices: dict) -> str:
    vix_price = prices.get("VIX", {}).get("price")
    if vix_price is None:
        return "⚠️ Data VIX tidak tersedia"
    if vix_price < 20:
        return "🟢 RISK-ON (Sentimen positif, saham cenderung naik)"
    elif vix_price <= 30:
        return "🟡 NEUTRAL (Sentimen hati-hati)"
    return "🔴 RISK-OFF (Sentimen negatif, saham cenderung turun)"


# ============================================================
# NEWS FORMATTING + CEASEFIRE DETECTION
# ============================================================

def detect_ceasefire_signal(title: str, summary: str = ""):
    text = (title + " " + summary).lower()
    has_ceasefire_kw = any(kw in text for kw in ["ceasefire", "gencatan", "truce"])
    if not has_ceasefire_kw:
        return None
    has_negation = any(neg in text for neg in NEGATION_WORDS)
    return "ceasefire_broken" if has_negation else "ceasefire_active"


def format_news_section(
    news_list: list,
    empty_msg: str,
    title_field: str = "title",
    summary_field: str = "summary",
    detect_ceasefire: bool = False,
    show_affected_assets: bool = False,
    show_confidence: bool = False,
    top_n: int = 5,
):
    news_sorted = sorted(news_list, key=lambda x: x.get("impact_score", 0), reverse=True)
    text = ""
    ceasefire_status = None

    for n in news_sorted[:top_n]:
        title = n.get(title_field, "")
        summary = n.get(summary_field, "")
        score = n.get("impact_score", 0)
        pub_date = (n.get("published_at") or "")[:10]
        summary_short = (summary[:150] + "...") if len(summary) > 150 else summary

        extra_tags = ""
        if show_affected_assets:
            affected = n.get("affected_assets", [])
            extra_tags += f" [Affects: {', '.join(affected)}]" if affected else " [Affects: tidak disebutkan]"
        if show_confidence:
            conf = n.get("confidence")
            src_count = n.get("source_count", 0)
            if conf:
                extra_tags += f" [Confidence: {conf}, Sources: {src_count}]"

        text += f"• [{pub_date}] {title} (Score: {score}){extra_tags}\n"
        if summary_short:
            text += f"  {summary_short}\n"

        if detect_ceasefire:
            signal = detect_ceasefire_signal(title, summary)
            if signal == "ceasefire_active":
                ceasefire_status = "active"
            elif signal == "ceasefire_broken" and ceasefire_status != "active":
                ceasefire_status = "broken"

    if not text:
        text = empty_msg

    if ceasefire_status == "active":
        text += "\n⚠️ **DETEKSI: GENCATAN SENJATA AKTIF** → Minyak BEARISH, Emas NEUTRAL/koreksi."
    elif ceasefire_status == "broken":
        text += "\n⚠️ **DETEKSI: GENCATAN SENJATA GAGAL** → Eskalasi. Minyak BULLISH, Emas BULLISH."

    return text, ceasefire_status


# ============================================================
# MACRO RULES (kept in Indonesian — this is prompt content, not code)
# ============================================================

MACRO_RULES = """
📌 ATURAN KORELASI MAKRO (WAJIB DIPATUHI, URUTAN PRIORITAS):
0. PRIORITAS TERTINGGI: Jika ada sinyal geopolitik AKTIF (perang/ceasefire runtuh) DAN sinyal makro bertentangan,
   MENANGKAN sinyal geopolitik untuk Oil & Gold (lebih immediate). Untuk DXY/S&P/BTC tetap pakai makro.
   Gunakan juga field "Affects" pada berita geopolitik sebagai konfirmasi aset mana yang benar-benar terdampak
   — jangan generalisasi ke semua aset kalau berita cuma menyebut aset tertentu.
1. VIX < 20 → RISK-ON → S&P 500 BULLISH.
2. VIX 20-30 → NEUTRAL → S&P 500 NEUTRAL.
3. VIX > 30 → RISK-OFF → S&P 500 BEARISH.
4. HANYA jika Yield US10Y benar-benar NAIK (delta positif di data aktual) DAN DXY > 101
   → Gold TERKOREKSI (NEUTRAL/BEARISH). Jika Yield US10Y justru TURUN meskipun levelnya
   masih tinggi secara nominal, rule ini TIDAK berlaku — jangan gunakan level absolut
   sebagai pengganti syarat arah/delta.
5. Ceasefire AKTIF → Oil BEARISH.
6. Ceasefire RUNTUH → Oil BULLISH, Gold BULLISH.
7. Yield US10Y > 4.5% → BTC BEARISH (likuiditas ketat). Ini ATURAN MUTLAK,
   TIDAK BOLEH di-override atau di-NEUTRAL-kan oleh faktor sentimen lain
   (risk-on, dst). Kalau ada faktor penyeimbang, sebutkan sebagai catatan
   TAMBAHAN, tapi keputusan akhir tetap BEARISH.
8. Jika berita KOSONG, JANGAN mengarang — analisis murni dari angka.
9. Jika suatu data macro ditandai [PERLU VALIDASI ⚠️], JANGAN pakai data itu sebagai alasan
   utama arah rekomendasi — sebut sebagai konteks historis saja.
10. Jika ada rilis makro high-impact DALAM 24-48 JAM KE DEPAN (lihat KALENDER MAKRO),
    WAJIB sebutkan sebagai event risk di kesimpulan — rekomendasi harus lebih hati-hati/
    probabilitas diturunkan karena volatilitas berpotensi naik menjelang rilis tersebut.
11. Jika ada PERUBAHAN DATA MAKRO TERBARU (revisi/rilis baru) yang match dengan aset yang
    sedang dianalisis, WAJIB sebutkan revisi tersebut secara eksplisit — jangan diam-diam
    pakai angka baru tanpa nyebut kalau ini baru saja berubah dari angka sebelumnya.

📌 ATURAN ROTASI LIKUIDITAS (WAJIB DICEK SETIAP LAPORAN):
12. Bandingkan arah pergerakan semua aset dalam satu snapshot. Kelompokkan:
    - SAFE-HAVEN: Gold, US10Y (harga naik = yield turun = flight to safety), DXY
    - RISK ASSET: S&P 500, Nasdaq, BTC, Crude Oil (kadang campuran, cek konteks)
13. Jika SAFE-HAVEN kompak melemah SEKALIGUS RISK ASSET kompak menguat (atau sebaliknya),
    WAJIB simpulkan sebagai satu narasi ROTASI LIKUIDITAS eksplisit — sebutkan aset ASAL
    (yang ditinggalkan) dan aset TUJUAN (yang dituju dana).
    Contoh: "Gold turun tipis sementara BTC & S&P naik → indikasi dana bergeser dari
    safe-haven ke risk asset (risk-on rotation)."
14. Jika pergerakan antar-aset TIDAK menunjukkan pola rotasi yang jelas (misal semua turun
    bersamaan, atau campur aduk tanpa arah), WAJIB nyatakan: "Tidak ada indikasi rotasi
    likuiditas yang jelas pada snapshot ini." JANGAN memaksakan narasi rotasi kalau datanya
    tidak mendukung.
15. VOLUME sebagai konfirmasi: jika ada data volume perdagangan yang jauh di atas/bawah rata-rata,
    sebutkan sebagai penguat/pelemah keyakinan terhadap narasi rotasi (bukan sumber utama).
16. Bedakan istilah secara ketat: gunakan "CLOSING PRICE LAMA" untuk aset market yang
    market-nya sedang tutup (VIX/S&P/Nasdaq saat weekend/holiday) — ini normal, BUKAN
    error. Gunakan "PERLU VALIDASI" HANYA untuk macro actuals (CPI/NFP/dst) yang
    ditandai [PERLU VALIDASI ⚠️] — ini menandakan timing rilis data belum terkonfirmasi.
    JANGAN campur dua istilah ini dalam kalimat yang sama.
17. SEBELUM menulis alasan rekomendasi yang menyebut arah pergerakan suatu data
    (naik/turun/menguat/melemah), WAJIB cocokkan dengan angka % yang tertulis di
    DATA UTAMA pada laporan yang sama. Dilarang keras menyebut arah yang bertentangan
    dengan angka aktual (misal: menyebut "yield naik" padahal datanya -0.51%/turun).
18. Kalau Crude Oil/Gold bergerak signifikan TANPA ada berita geopolitik pendukung di
    snapshot ini, WAJIB nyatakan eksplisit "pergerakan harga murni price action, tidak
    ada konfirmasi driver geopolitik/berita" — jangan pakai frasa vague seperti
    "mungkin karena tidak ada berita X".
19. Berita dengan confidence rendah atau source_count=1 boleh disebut sebagai konteks,
    TAPI JANGAN dijadikan alasan tunggal/dominan untuk rekomendasi arah aset — sebut
    eksplisit sebagai "sinyal awal, belum terkonfirmasi banyak sumber" jika relevan.
20. Untuk setiap item DATA MAKRO (CPI/NFP/FOMC/GDP/PCE), WAJIB baca instruksi eksplisit
    yang tertulis di baris item tersebut pada DATA MAKRO (ACTUALS) — setiap baris sudah
    dilengkapi INSTRUKSI yang menyatakan apakah item itu perlu ditulis "perlu divalidasi"
    atau tidak. JANGAN generalisasi dari item lain: kalau 4 dari 5 item bertanda perlu
    validasi, item ke-5 BISA SAJA valid — ikuti instruksi per baris, bukan pola mayoritas.
21. SEBELUM menulis alasan REKOMENDASI ASET yang menyebut atau membantah dampak geopolitik,
    WAJIB cocokkan dengan field [Affects: ...] pada berita geopolitik yang tercantum di atas.
    Jika suatu aset TIDAK muncul di [Affects: ...] manapun (atau tertulis "tidak disebutkan"),
    gunakan frasa "tidak ada berita geopolitik yang secara spesifik menyebut [aset]" —
    JANGAN sampai section BERITA & KONTEKS dan REKOMENDASI ASET saling bertentangan
    soal aset yang sama.
"""


# ============================================================
# BUILD PROMPT
# ============================================================

def build_smart_prompt(data: dict) -> str:
    prices = data.get("market_prices", {})
    macro_actuals = data.get("macro_actuals", {})
    macro_calendar = data.get("macro_calendar", {})
    macro_alerts = data.get("macro_alerts", [])
    sentiment_news = data.get("sentiment_news", [])
    geo_news = data.get("geopolitical_news", [])

    price_lines = []
    for k, v in prices.items():
        price = v.get("price", "N/A")
        chg_1d = safe_fmt_pct(v.get("change_1d_pct"))
        chg_20d = safe_fmt_pct(v.get("change_20d_pct"))
        volume = v.get("volume", "-")
        status_flag = " [CLOSED/BASI]" if "CLOSED" in v.get("market_status", "") else " [LIVE]"
        vol_part = f", vol: {volume}" if volume and volume != "-" else ""
        price_lines.append(f"• {k}: {price} (1d: {chg_1d}, 20d: {chg_20d}{vol_part}){status_flag}")
    price_text = "\n".join(price_lines) or "Tidak ada data harga."

    freshness_warning = check_snapshot_freshness(data)
    macro_staleness_warning = check_macro_staleness(macro_actuals)
    macro_alerts_text = format_macro_alerts(macro_alerts)
    sentiment = classify_vix_sentiment(prices)
    surprise_text = format_macro_actuals(macro_actuals)
    calendar_text = format_macro_calendar(macro_calendar)

    fed_text, _ = format_news_section(
        sentiment_news,
        empty_msg="Tidak ada berita Fed di snapshot ini. JANGAN mengarang berita — analisis murni dari angka makro.",
        title_field="title",
        summary_field="summary",
    )

    geo_text, ceasefire_status = format_news_section(
        geo_news,
        empty_msg="Tidak ada berita geopolitik di snapshot ini. JANGAN mengarang narasi geopolitik.",
        title_field="headline",
        summary_field="why_it_matters",
        detect_ceasefire=True,
        show_affected_assets=True,
        show_confidence=True,
    )

    today = datetime.now().strftime("%d %b %Y, %H:%M WIB")

    prompt = f"""
Anda adalah analis makro ekonomi senior. Buat laporan berdasarkan data berikut.

📅 Hari ini: {today}

{MACRO_RULES}

{freshness_warning}
{macro_staleness_warning}
{macro_alerts_text}

=== DATA PASAR ===
{price_text}

=== SENTIMEN PASAR (VIX) ===
{sentiment}

=== DATA MAKRO (ACTUALS) ===
{surprise_text}

=== KALENDER MAKRO (UPCOMING, event risk) ===
{calendar_text}

=== BERITA FED (Top 5) ===
{fed_text}

=== BERITA GEOPOLITIK (Top 5) ===
{geo_text}

TUGAS:
Buat laporan dengan format PERSIS di bawah ini. Gunakan HANYA data yang tersedia.
Jika ada peringatan DATA BASI, PERLU VALIDASI, atau PERUBAHAN DATA MAKRO TERBARU,
WAJIB sebutkan di laporan dan JANGAN jadikan itu dasar rekomendasi arah aset tanpa disclaimer.

📊 MACRO LIQUIDITY UPDATE — {today}

🔹 DATA UTAMA
- DXY: [nilai] ([perubahan 1d]) | US10Y: [nilai]% ([perubahan])
- Gold: $[nilai] ([perubahan]) | BTC: $[nilai] ([perubahan])
- S&P 500: [nilai] ([perubahan]) | Nasdaq: [nilai] ([perubahan])
- Crude Oil: $[nilai] ([perubahan]) | VIX: [nilai] ([perubahan])
- CPI: [nilai] (BEAT/MISS/IN LINE) → [1 kalimat, ikuti instruksi per baris di DATA MAKRO]
- NFP: [nilai] (BEAT/MISS/IN LINE) → [1 kalimat, ikuti instruksi per baris di DATA MAKRO]
- FOMC: [nilai]% (BEAT/MISS/IN LINE) → [1 kalimat, ikuti instruksi per baris di DATA MAKRO]
- GDP: [nilai] (BEAT/MISS/IN LINE) → [1 kalimat, ikuti instruksi per baris di DATA MAKRO]
- PCE: [nilai] (BEAT/MISS/IN LINE) → [1 kalimat, ikuti instruksi per baris di DATA MAKRO]

🌊 LIKUIDITAS & ROTASI ASET
[1-2 kalimat: identifikasi apakah ada pola rotasi (safe-haven ↔ risk asset), sebutkan
aset ASAL dan TUJUAN secara eksplisit jika ada. Jika tidak ada pola jelas, nyatakan itu.
Ikuti ATURAN ROTASI LIKUIDITAS di atas.]

📅 EVENT RISK MENDATANG
[1 kalimat: sebutkan rilis makro high-impact dalam beberapa hari ke depan jika ada, dan
implikasinya terhadap volatilitas/probabilitas rekomendasi. Jika tidak ada, nyatakan itu.]

🌍 BERITA & KONTEKS
- [Ringkasan berita Fed / kebijakan moneter-fiskal terpenting, atau "tidak ada data" jika kosong]
- [Ringkasan berita geopolitik terpenting beserta aset yang terdampak (affected_assets),
  atau "tidak ada data" jika kosong]

💡 KESIMPULAN & LOGIKA UTAMA [Probabilitas: TINGGI/SEDANG/RENDAH]
[1-2 paragraf kesimpulan + alasan dominan. Patuhi macro rules! Sebut eksplisit data mana yang
perlu validasi/baru direvisi jika ada — sesuai instruksi per baris, JANGAN generalisasi.
Kaitkan dengan narasi rotasi likuiditas dan event risk mendatang di atas jika relevan.]

📌 REKOMENDASI ASET (WAJIB PATUHI MACRO RULES!)
- DXY (Dolar AS): [BULLISH/BEARISH/NEUTRAL] — [alasan]
- Gold (XAU): [BULLISH/BEARISH/NEUTRAL] — [alasan]
- S&P 500: [BULLISH/BEARISH/NEUTRAL] — [alasan: patokan VIX]
- Crude Oil (CL): [BULLISH/BEARISH/NEUTRAL] — [alasan: cek status ceasefire & field Affects,
  jangan kontradiksi dengan section BERITA & KONTEKS]
- BTC: [BULLISH/BEARISH/NEUTRAL] — [alasan: yield & risk sentiment]

Catatan:
- JANGAN sebut data yang tidak tersedia.
- JANGAN mengarang berita/peristiwa.
- JANGAN memaksakan narasi rotasi likuiditas kalau data tidak mendukung pola tersebut.
- JANGAN generalisasi status validasi macro dari item lain — cek instruksi per baris.
- Probabilitas: TINGGI (≥70%), SEDANG (40-69%), RENDAH (<40%)
"""
    return prompt


def _validate_data(data: dict):
    if not data.get("market_prices"):
        return "❌ Tidak ada data market_prices sama sekali — snapshot kemungkinan gagal fetch. Analisis dibatalkan."
    return None


def generate_analysis(data: dict) -> str:
    """Single entry point used by pipeline.py to get the final AI report."""
    validation_error = _validate_data(data)
    if validation_error:
        return validation_error

    if not GROQ_API_KEYS:
        return "❌ AI Error: Tidak ada GROQ_API_KEYS yang dikonfigurasi."

    prompt = build_smart_prompt(data)

    for attempt in range(len(GROQ_API_KEYS)):
        try:
            key = groq_rotator.get_key()
            client = Groq(api_key=key)

            completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": "Anda adalah analis finansial senior. Jawab dalam bahasa Indonesia profesional. Jangan mengarang data."},
                    {"role": "user", "content": prompt},
                ],
                model=GROQ_MODEL,
                temperature=0.3,
                max_tokens=2048,
            )
            return completion.choices[0].message.content
        except RuntimeError as e:
            logger.error("Groq runtime error: %s", e)
            return f"❌ AI Error: {e}"
        except Exception as e:
            err_str = str(e).lower()
            if "rate" in err_str or "429" in err_str:
                logger.warning("Groq rate limit hit, rotating key")
                groq_rotator.rotate("rate limit")
                continue
            if attempt == len(GROQ_API_KEYS) - 1:
                logger.error("Groq error (last attempt): %s", e)
                return f"❌ AI Error: {e}"
            continue

    return "❌ AI Error: Semua Groq API key gagal/blocked."


if __name__ == "__main__":
    dummy_data = {
        "timestamp": datetime.now().isoformat(),
        "market_prices": {},
        "macro_actuals": {},
        "macro_calendar": {},
        "macro_alerts": [],
        "sentiment_news": [],
        "geopolitical_news": [],
    }
    print(build_smart_prompt(dummy_data))