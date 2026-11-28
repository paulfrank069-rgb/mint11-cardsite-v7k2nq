#!/usr/bin/env python3
"""Build the verified-cards site from cards.json.

Re-run after appending cards to cards.json:
    python3 /workspace/cardsite/build.py

Writes two outputs:
  - index.html: self-contained file with images embedded as base64
    (downscaled to max 1400 px wide, JPEG quality 80) so it opens offline.
  - pages/: the hosted copy, with the same images as separate JPEGs under
    pages/images/ and a slim index.html that references them. This is what
    gets published to GitHub Pages (see PUBLISH.md).

Every card must start with "Card front (label cropped)" and
"Card back (label cropped)" photos (no slab labels, cert numbers, or
barcodes). warn_missing_label_cropped() prints a loud WARNING if any
card is missing them, but the build still completes.
"""

from __future__ import annotations

import base64
import html
import io
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

import needs_photos
import linkcheck

HERE = Path(__file__).resolve().parent
CARDS_PATH = HERE / "cards.json"
OUT_PATH = HERE / "index.html"
PAGES_DIR = HERE / "pages"
PAGES_IMG = PAGES_DIR / "images"
# Static extras copied as-is into pages/ (the password-protected Money page and
# its encrypted data file). Plaintext money data never goes in the repo.
STATIC_EXTRAS = ("money", "data")
MAX_WIDTH = 1400
JPEG_QUALITY = 80
ICT = ZoneInfo("Asia/Bangkok")

STATUS_LABEL = {
    "PASS": "PASS",
    "WATCH": "WATCH",
    "ALERT": "ALERT",
    "UNVERIFIED": "UNVERIFIED",
    "ENDED": "ENDED",
    "FAIL": "FAIL",
    "OWNED": "OWNED",
}
LANE_ORDER = ["1", "2", "5"]

_CLOSE_RE = re.compile(
    r"(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3,9})\s+(?P<year>\d{4})"
    r"(?:[^\d]{0,40}(?P<hm>\d{1,2}:\d{2}))?"
)
ENDED_KEEP_DAYS = 7
REMOVED_PATH = HERE / "cards_removed.json"


def parse_close_time_ict(text: object) -> datetime | None:
    """Pull the first calendar date, and a nearby time if present, out of close_time_ict.

    Accepts both 'Fri 20 Nov 2026 09:02 ICT' and 'Ended Mon 23 Nov 2026
    (scheduled 10:00 ICT, extended bidding)'. Date-only strings are stored
    as midnight ICT so the 7-day ENDED window can be applied.
    """
    if not text:
        return None
    m = _CLOSE_RE.search(str(text))
    if not m:
        return None
    hm = m.group("hm") or "00:00"
    stamp = f"{m.group('day')} {m.group('mon')} {m.group('year')} {hm}"
    for fmt in ("%d %b %Y %H:%M", "%d %B %Y %H:%M"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=ICT)
        except ValueError:
            continue
    return None


def apply_ended_status(cards: list[dict], now: datetime) -> None:
    """Move a card to ENDED at build time once its close time has passed."""
    for card in cards:
        close = parse_close_time_ict(card.get("close_time_ict"))
        if close is not None and close < now:
            card["status"] = "ENDED"


def card_end_time_ict(card: dict) -> datetime | None:
    """Best available auction-end timestamp for the 7-day ENDED window."""
    close = parse_close_time_ict(card.get("close_time_ict"))
    if close is not None:
        return close
    return parse_close_time_ict(card.get("sold_date_ict"))


def archive_ended_older_than(
    cards: list[dict], now: datetime, days: int = ENDED_KEEP_DAYS
) -> tuple[list[dict], list[dict]]:
    """Keep ENDED cards for `days` calendar days after close; archive the rest.

    Frank 24 Nov 2026: ENDED cards stay visible for 1 week, filterable per lane.
    A card is kept if its close date is on or after (today − 7 days) in ICT
    (so on 24 Nov 2026 the cutoff is 17 Nov 2026). Cards whose close date
    cannot be parsed are kept, never silently dropped.
    """
    cutoff = now.astimezone(ICT).date() - timedelta(days=days)
    keep: list[dict] = []
    prune: list[dict] = []
    for card in cards:
        if card.get("status") != "ENDED":
            keep.append(card)
            continue
        close = card_end_time_ict(card)
        if close is None:
            print(
                "KEEP ENDED (unparsed close date): "
                f"{card.get('id')} close={card.get('close_time_ict')!r} "
                f"sold_date={card.get('sold_date_ict')!r}"
            )
            keep.append(card)
            continue
        if close.date() >= cutoff:
            keep.append(card)
        else:
            print(
                f"PRUNE ENDED >{days}d: {card.get('id')} "
                f"close={close.date().isoformat()} cutoff={cutoff.isoformat()}"
            )
            prune.append(card)
    if prune:
        import json as _json
        existing: dict
        if REMOVED_PATH.is_file():
            existing = _json.loads(REMOVED_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or not isinstance(existing.get("cards"), list):
                existing = {"removed_at": "", "cards": existing if isinstance(existing, list) else []}
        else:
            existing = {"removed_at": "", "cards": []}
        stamp = now.strftime("%Y-%m-%d %H:%M ICT")
        for card in prune:
            row = dict(card)
            row["removed_at"] = stamp
            row["removed_reason"] = (
                f"ENDED more than {days} days after close "
                f"(Frank 24 Nov 2026, 7-day site retention)"
            )
            existing["cards"].append(row)
        existing["removed_at"] = stamp
        REMOVED_PATH.write_text(
            _json.dumps(existing, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Archived {len(prune)} ENDED card(s) older than {days} days to {REMOVED_PATH}")
    else:
        print(f"ENDED retention: kept {sum(1 for c in keep if c.get('status')=='ENDED')} "
              f"(cutoff {cutoff.isoformat()}); pruned 0")
    return keep, prune


def e(text: object) -> str:
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def load_cards() -> dict:
    data = json.loads(CARDS_PATH.read_text(encoding="utf-8"))
    cards = data.get("cards") or data
    if isinstance(cards, dict):
        raise SystemExit("cards.json must contain a 'cards' array")
    cards = sorted(
        cards,
        key=lambda c: c.get("verified_sort") or "",
        reverse=True,
    )
    data["cards"] = cards
    return data


# Alt (and some other) originals are RGBA with the background punched out.
# A plain Image.convert("RGB") fills those transparent pixels with black and
# leaves a black halo around the slab. Always composite onto light grey first.
ALPHA_COMPOSITE_BG = (0xE6, 0xE6, 0xE6)


def rgb_for_site(im: Image.Image, background: tuple[int, int, int] = ALPHA_COMPOSITE_BG) -> Image.Image:
    """Convert any mode to RGB, compositing alpha onto #e6e6e6 (not black)."""
    has_alpha = (
        im.mode in ("RGBA", "LA")
        or (im.mode == "P" and "transparency" in im.info)
    )
    if has_alpha:
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, background)
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return im.convert("RGB")


def prepare_image(path: Path) -> tuple[bytes, int, int]:
    """Return (jpeg_bytes, width, height) after downscale + JPEG q80."""
    with Image.open(path) as im:
        im = rgb_for_site(im)
        w, h = im.size
        if w > MAX_WIDTH:
            h = round(h * (MAX_WIDTH / w))
            w = MAX_WIDTH
            im = im.resize((w, h), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), w, h


def data_uri_from_jpeg(data: bytes) -> str:
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def page_image_name(card: dict, index: int, kind: str) -> str:
    cid = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(card.get("id") or "card")).strip("-")[:60]
    kind = re.sub(r"[^a-zA-Z0-9_-]+", "-", kind or "photo").strip("-")[:40] or "photo"
    return f"{cid}_{index:02d}_{kind}.jpg"


# Vintage = release year 2008 or earlier (RULES_COMMON). Modern = 2009 or later.
# Never guess a year from a set name alone.
_YEAR_MIN = 1996
_YEAR_MAX = 2030


def parse_year_value(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        y = value
    elif isinstance(value, float) and value.is_integer():
        y = int(value)
    else:
        m = re.fullmatch(r"(?:19|20)\d{2}", str(value).strip())
        if not m:
            return None
        y = int(m.group(0))
    if _YEAR_MIN <= y <= _YEAR_MAX:
        return y
    return None


def year_from_text(text: object) -> int | None:
    """Leading release year only (Alt/PASS titles start with it)."""
    m = re.match(r"\s*((?:19|20)\d{2})\b", str(text or ""))
    return parse_year_value(m.group(1)) if m else None


def card_year(card: dict) -> int | None:
    """Year from a card/PASS field, or a leading year in the title or set. Never guessed."""
    for key in ("year", "yearStr", "release_year", "set_year"):
        y = parse_year_value(card.get(key))
        if y is not None:
            return y
    for key in ("title", "name", "set"):
        y = year_from_text(card.get(key))
        if y is not None:
            return y
    return None


def card_era(card: dict) -> str:
    """vintage <= 2008, modern >= 2009, unknown if the year is missing."""
    y = card_year(card)
    if y is None:
        return "unknown"
    return "vintage" if y <= 2008 else "modern"


def era_badge_html(card: dict) -> str:
    era = card_era(card)
    y = card_year(card)
    if era == "unknown":
        label, cls = "Year unknown", "badge-era-unknown"
    elif era == "vintage":
        label, cls = f"Vintage · {y}", "badge-era-vintage"
    else:
        label, cls = f"Modern · {y}", "badge-era-modern"
    return f'<span class="badge {cls}">{e(label)}</span>'


VENUE_ORDER = ("Fanatics", "Alt", "Goldin", "eBay")


def venue_from_url(url: object) -> str | None:
    """Work the venue out from the listing URL (Fanatics / Alt / Goldin / eBay)."""
    u = ("" if url is None else str(url)).lower()
    if "fanaticscollect.com" in u or "fanatics.com" in u:
        return "Fanatics"
    if "alt.xyz" in u:
        return "Alt"
    if "goldin.co" in u:
        return "Goldin"
    if "ebay.com" in u or "ebay." in u:
        return "eBay"
    return None


def card_venue(card: dict) -> str:
    return venue_from_url(card.get("link") or "") or "unknown"


def venue_badge_html(card: dict) -> str:
    venue = card_venue(card)
    if venue == "unknown":
        return ""
    cls = "badge-venue-" + venue.lower()
    return f'<span class="badge badge-venue {cls}">{e(venue)}</span>'


def counts(cards: list[dict]) -> dict[str, int]:
    active = [c for c in cards if c.get("status") != "ENDED"]
    out = {
        "ALL": len(active),
        "PASS": sum(1 for c in active if c.get("status") == "PASS"),
        "WATCH": sum(1 for c in active if c.get("status") == "WATCH"),
        "ALERT": sum(1 for c in active if c.get("status") == "ALERT"),
        "UNVERIFIED": sum(1 for c in active if c.get("status") == "UNVERIFIED"),
        "FAIL": sum(1 for c in active if c.get("status") == "FAIL"),
        "OWNED": sum(1 for c in active if c.get("status") == "OWNED"),
        "ENDED": sum(1 for c in cards if c.get("status") == "ENDED"),
    }
    for lane in LANE_ORDER:
        out[f"lane-{lane}"] = sum(1 for c in active if str(c.get("lane")) == lane)
        out[f"lane-{lane}-ended"] = sum(
            1 for c in cards
            if c.get("status") == "ENDED" and str(c.get("lane")) == lane
        )
    out["lane-ALL-ended"] = out["ENDED"]
    for era in ("vintage", "modern", "unknown"):
        out[f"era-{era}"] = sum(1 for c in active if card_era(c) == era)
        out[f"era-{era}-ended"] = sum(
            1 for c in cards
            if c.get("status") == "ENDED" and card_era(c) == era
        )
    out["era-ALL"] = out["ALL"]
    out["era-ALL-ended"] = out["ENDED"]
    for venue in VENUE_ORDER:
        out[f"venue-{venue}"] = sum(1 for c in active if card_venue(c) == venue)
        out[f"venue-{venue}-ended"] = sum(
            1 for c in cards
            if c.get("status") == "ENDED" and card_venue(c) == venue
        )
    out["venue-ALL"] = out["ALL"]
    out["venue-ALL-ended"] = out["ENDED"]
    out["venue-unknown"] = sum(1 for c in active if card_venue(c) == "unknown")
    out["venue-unknown-ended"] = sum(
        1 for c in cards
        if c.get("status") == "ENDED" and card_venue(c) == "unknown"
    )
    return out


def render_comp_text(comp: dict) -> str:
    """Plain-text last-sold item. Per-sale and PriceCharting links are not shown."""
    return e((comp.get("text") or "").strip())


def render_sold_dd(row: dict) -> str:
    value = (row.get("value") or "").strip()
    if value:
        return e(value)
    comps = row.get("comps") or []
    parts = [render_comp_text(c) for c in comps]
    parts = [p for p in parts if p]
    if parts:
        return "; ".join(parts)
    return ""


# Confirmed Alt asset page (indexed examples and Alt API docs):
#   https://alt.xyz/itm/<assetId>/research?grade=PSA-10.0
# Other confirmed grade query values: PSA-9.0, PSA-8.0, CGC-10.0, BGS-9.5, BGS-10.0.
# CGC Pristine is not a confirmed query value, so those cards use the PSA 10 tab.
def alt_grade_query(grade: str) -> str:
    s = re.sub(r"\s+", " ", (grade or "").strip().upper())
    if s == "PSA 8":
        return "PSA-8.0"
    if s == "PSA 9":
        return "PSA-9.0"
    if s == "PSA 10":
        return "PSA-10.0"
    if s == "CGC 10":
        return "CGC-10.0"
    return "PSA-10.0"


def alt_sold_history_url(card: dict) -> str:
    asset_id = (card.get("alt_asset_id") or "").strip()
    if not asset_id:
        return ""
    return f"https://alt.xyz/itm/{asset_id}/research?grade={alt_grade_query(card.get('grade') or '')}"


def render_sold(rows: list[dict]) -> str:
    if not rows:
        return ""
    items = []
    for row in rows:
        items.append(
            "<div class='kv'>"
            f"<dt>{e(row.get('label', ''))}</dt>"
            f"<dd>{render_sold_dd(row)}</dd>"
            "</div>"
        )
    return (
        "<section class='block money'>"
        "<h3>Last solds (30 days)</h3>"
        f"<dl class='solds'>{''.join(items)}</dl>"
        "</section>"
    )


def render_sold_result(card: dict) -> str:
    """Clickable sold result under the ended line, when fields are set.

    Prefer the exact Alt sale page (sold_link). If the only link is a
    PriceCharting page, label it PriceCharting so it is not mistaken for
    the exact sale. Ended cards with no sold price get 'Result on Alt'
    and a price-unconfirmed note.
    """
    link = (card.get("sold_link") or "").strip()
    price = (card.get("sold_price") or "").strip()
    date = (card.get("sold_date_ict") or "").strip()
    note = (card.get("sold_price_note") or "").strip()
    kind = (card.get("sold_link_kind") or "").strip().lower()
    if not kind:
        if "pricecharting.com" in link.lower():
            kind = "pricecharting"
        elif link:
            kind = "alt"

    label = ""
    href = link
    if price:
        date_bit = f" on {date}" if date else ""
        if kind == "pricecharting":
            label = f"Sold {price}{date_bit} — PriceCharting"
        elif href:
            label = f"Sold {price}{date_bit} — view sale"
        else:
            label = f"Sold {price}{date_bit}"
    elif (card.get("status") == "ENDED"):
        href = href or (card.get("link") or "").strip()
        if href:
            label = "Result on Alt"
            note = note or "Price unconfirmed"

    if not label:
        return ""

    if href:
        link_html = (
            f"<a class='sold-link' href='{e(href)}' target='_blank' "
            f"rel='noopener noreferrer'>{e(label)}</a>"
        )
    else:
        link_html = f"<span class='sold-link'>{e(label)}</span>"
    note_html = f"<p class='sold-note'>{e(note)}</p>" if note else ""
    return f"<p class='sold-result'>{link_html}</p>{note_html}"


def render_risks(risks: list[str]) -> str:
    if not risks:
        return ""
    lis = "".join(f"<li>{e(r)}</li>" for r in risks if r)
    return (
        "<section class='block risks'>"
        "<h3>Risks</h3>"
        f"<ul>{lis}</ul>"
        "</section>"
    )


LANE5_WARN_TEXT = "\u26a0 Below 950 (Gem Mint line): may not cross to PSA 10"


def lane5_warn(card: dict) -> str:
    """Lane 5 display flag (KB v2.9, Frank OK 22 Nov 00:13): not a fail gate."""
    try:
        sc = float(card.get("score_v29"))
    except (TypeError, ValueError):
        return ""
    if str(card.get("lane")) == "5" and sc < 950:
        return f"<p class='warn-red' role='alert'>{e(LANE5_WARN_TEXT)} \u00b7 v2.9 score {e(card.get('score_v29'))}</p>"
    return ""


def unverified_note(card: dict) -> str:
    """v2.9 8x corner gate not met: photos too small to call the corners."""
    if card.get("status") != "UNVERIFIED":
        return ""
    txt = card.get("unverified_reason") or "Photos too small: unverified"
    return f"<p class='warn-unv'>{e(txt)}</p>"


_SCORE_NUM = r"(\d{2,3}(?:\.\d+)?)"


def _score_to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_score_num(n: float) -> str:
    if abs(n - round(n)) < 1e-9:
        return str(int(round(n)))
    return f"{n:.1f}"


def _score_text_for_parse(text: str) -> str:
    """Drop trailing 'old score' / 'was ~920' notes so stale numbers are ignored."""
    return re.split(
        r"(?i)(?:\s*[·•,;]?\s*\bold score\b|\s*·\s*was\b|\s+\bwas\s+~?" + _SCORE_NUM + r")",
        text,
        maxsplit=1,
    )[0]


def parse_score_info(card: dict) -> dict:
    """Pull a display label + color tier from score / score_v29 without dropping text.

    Handles 981.7, 970.4, ~883, 945.2, ~977–982 / 987.4, and N/A / UNVERIFIED.
    Color tiers: >=960 high, 950–959 mid, <950 low, missing/unverified none.
    """
    raw = str(card.get("score") or "").strip()
    status = str(card.get("status") or "")
    unverified = status == "UNVERIFIED" or raw.upper().startswith("UNVERIFIED")
    score_v29 = _score_to_float(card.get("score_v29"))

    label = "N/A"
    value: float | None = None

    if unverified:
        return {"tier": "none", "value": None, "label": "N/A", "warn": False}

    m_pure = re.fullmatch(r"(~)?\s*" + _SCORE_NUM + r"\s*", raw)
    if m_pure:
        value = float(m_pure.group(2))
        label = ("~" if m_pure.group(1) else "") + _format_score_num(value)
    else:
        text = _score_text_for_parse(raw)
        ct = re.search(
            r"CT\s+conservative\s+(~)?" + _SCORE_NUM + r"\s*[–-]\s*" + _SCORE_NUM,
            text,
            re.I,
        )
        lane = re.search(r"\blane\s+score\s+(~)?" + _SCORE_NUM, text, re.I)
        scores = list(
            re.finditer(
                r"(?<!old )(?<!best case )(?<!best-case )\bscore\s+(~)?"
                + _SCORE_NUM
                + r"(?:\s*[–-]\s*" + _SCORE_NUM + r")?",
                text,
                re.I,
            )
        )
        lo = hi = None
        tilde = False
        if scores:
            sm = scores[0]
            tilde = bool(sm.group(1))
            lo = float(sm.group(2))
            hi = float(sm.group(3)) if sm.group(3) else None
        official = _score_to_float(lane.group(2)) if lane else None
        if official is None and lo is not None and hi is None:
            official = lo
        if ct:
            ct_lo, ct_hi = float(ct.group(2)), float(ct.group(3))
            display_range = f"{ct.group(1) or ''}{_format_score_num(ct_lo)}–{_format_score_num(ct_hi)}"
            value = official if official is not None else ct_lo
            if official is not None and not (ct_lo - 0.05 <= official <= ct_hi + 0.05):
                label = f"{display_range} / {_format_score_num(official)}"
            else:
                label = display_range
        elif lo is not None and hi is not None:
            display_range = (
                f"{'~' if tilde else ''}{_format_score_num(lo)}–{_format_score_num(hi)}"
            )
            value = official if official is not None else lo
            if official is not None and not (lo - 0.05 <= official <= hi + 0.05):
                label = f"{display_range} / {_format_score_num(official)}"
            else:
                label = display_range
        elif lo is not None:
            value = official if official is not None else lo
            if official is not None and abs(official - lo) > 0.15:
                label = (
                    f"{'~' if tilde else ''}{_format_score_num(lo)} / {_format_score_num(official)}"
                )
            elif tilde:
                label = "~" + _format_score_num(lo)
            else:
                label = _format_score_num(official if official is not None else lo)
        elif official is not None:
            value = official
            label = _format_score_num(official)
        elif score_v29 is not None:
            value = score_v29
            label = _format_score_num(score_v29)

    if value is None and score_v29 is not None:
        value = score_v29
        if label == "N/A":
            label = _format_score_num(score_v29)

    warn = value is not None and value < 950
    if warn and "⚠" not in label and "<950" not in label:
        label = f"{label} ⚠ <950"

    if value is None:
        tier = "none"
    elif value >= 960:
        tier = "high"
    elif value >= 950:
        tier = "mid"
    else:
        tier = "low"

    return {"tier": tier, "value": value, "label": label, "warn": warn}


def score_pill_html(info: dict, *, kind: str = "row") -> str:
    label = info.get("label") or "N/A"
    if kind == "row":
        text = label if label.upper().startswith("SCORE") else f"Score {label}"
    else:
        text = label
    return (
        f"<span class='score-pill score-pill-{e(info.get('tier') or 'none')}'>"
        f"{e(text)}</span>"
    )


def header_score_pill(card: dict) -> str:
    if not (card.get("score") or card.get("score_v29") not in (None, "")):
        return ""
    return score_pill_html(parse_score_info(card), kind="header")


def status_badge_html(card: dict) -> str:
    """Status badge. WATCH is yellow and always says 'below bar' plus the score."""
    status = str(card.get("status") or "")
    if status == "WATCH":
        info = parse_score_info(card)
        value = info.get("value")
        if value is not None:
            text = f"WATCH – below bar (score {_format_score_num(value)})"
        else:
            text = "WATCH – below bar"
        return f'<span class="badge badge-WATCH">{e(text)}</span>'
    shown = STATUS_LABEL.get(status, status) if status else ""
    return f'<span class="badge badge-{e(status)}">{e(shown)}</span>'


STATUS_SECTION_ORDER = [
    "PASS", "WATCH", "ALERT", "UNVERIFIED", "FAIL", "OWNED", "ENDED",
]
STATUS_SECTION_TITLE = {
    "PASS": "PASS",
    "WATCH": "WATCH – below the bar",
    "ALERT": "ALERT",
    "UNVERIFIED": "UNVERIFIED",
    "FAIL": "FAIL",
    "OWNED": "OWNED",
    "ENDED": "ENDED",
}


def wrap_status_sections(cards: list[dict], articles: list[str]) -> str:
    """Group cards into PASS / WATCH / other status sections, then ENDED.

    Era and status filters still apply; empty sections are hidden in JS.
    """
    from collections import defaultdict
    buckets: dict[str, list[str]] = defaultdict(list)
    other: list[str] = []
    known = set(STATUS_SECTION_ORDER)
    for card, art in zip(cards, articles):
        st = str(card.get("status") or "")
        if st in known:
            buckets[st].append(art)
        else:
            other.append(art)
    parts: list[str] = []
    for st in STATUS_SECTION_ORDER:
        items = buckets.get(st) or []
        if not items:
            continue
        title = STATUS_SECTION_TITLE.get(st, st)
        parts.append(
            f'<section class="status-section" data-section-status="{e(st)}" '
            f'id="section-{e(st.lower())}">'
            f'<h2 class="status-section-title status-section-title-{e(st)}">'
            f'{e(title)}</h2>'
            + "".join(items)
            + "</section>"
        )
    if other:
        parts.append(
            '<section class="status-section" data-section-status="OTHER" '
            'id="section-other">'
            '<h2 class="status-section-title">Other</h2>'
            + "".join(other)
            + "</section>"
        )
    return "".join(parts)


def render_score_centering(card: dict) -> str:
    parts: list[str] = []
    if card.get("score"):
        info = parse_score_info(card)
        raw = str(card.get("score") or "").strip()
        detail = raw
        if re.fullmatch(r"~?\d{2,3}(?:\.\d+)?", raw):
            detail = ""
        detail_html = (
            f"<span class='score-detail'>{e(detail)}</span>" if detail else ""
        )
        parts.append(
            f"<div class='kv score-row score-row-{e(info['tier'])}'>"
            "<dt>Score</dt>"
            f"<dd><div class='score-dd'>{score_pill_html(info, kind='row')}"
            f"{detail_html}</div></dd></div>"
        )
    if card.get("centering_front"):
        parts.append(
            "<div class='kv'><dt>Front centering</dt>"
            f"<dd>{e(card['centering_front'])}</dd></div>"
        )
    if card.get("centering_back"):
        parts.append(
            "<div class='kv'><dt>Back centering</dt>"
            f"<dd>{e(card['centering_back'])}</dd></div>"
        )
    if card.get("centering_note"):
        parts.append(f"<p class='note'>{e(card['centering_note'])}</p>")
    if not parts:
        return ""
    return (
        "<section class='block gradeblock'>"
        "<h3>Score &amp; centering</h3>"
        + lane5_warn(card) +
        f"<dl class='solds'>{''.join(p for p in parts if p.startswith('<div'))}</dl>"
        + "".join(p for p in parts if p.startswith("<p"))
        + "</section>"
    )


def render_gallery(card: dict, images: list[dict], start_index: int) -> str:
    if not images:
        return (
            "<section class='block'>"
            "<h3>Photos</h3>"
            "<p class='note'>No photos that passed the label/cert check.</p>"
            "</section>"
        )
    figs: list[str] = []
    idx = start_index
    for img in images:
        figs.append(
            "<figure class='shot' data-kind='"
            + e(img.get("kind") or "photo")
            + "'>"
            f"<button type='button' class='shot-btn' data-idx='{idx}' "
            f"aria-label='Enlarge: {e(img.get('caption') or img.get('alt') or 'photo')}'>"
            f"<img src='{img['uri']}' alt='{e(img.get('alt') or '')}' "
            f"width='{img['width']}' height='{img['height']}' loading='lazy'>"
            "</button>"
            f"<figcaption>{e(img.get('caption') or '')}</figcaption>"
            "</figure>"
        )
        idx += 1
    return (
        "<section class='block gallery'>"
        "<h3>Photos</h3>"
        f"<div class='shots'>{''.join(figs)}</div>"
        "<p class='hint'>Tap a photo to enlarge.</p>"
        "</section>"
    )


def render_card(card: dict, images: list[dict], start_index: int) -> str:
    status = e(card.get("status") or "")
    lane = e(card.get("lane") or "")
    title = e(card.get("title") or "")
    grade = e(card.get("grade") or "")
    link = card.get("link") or ""
    platform = e(card.get("platform") or "")
    close = card.get("close_time_ict") or ""
    price = card.get("price_bid") or ""
    profit = card.get("profit_if_hit") or ""
    loss = card.get("loss_if_miss") or ""
    note = card.get("live_sold_note") or ""
    verified = card.get("verified_at") or ""
    lane_label = e(card.get("lane_label") or f"Lane {lane}")

    meta_bits = [f"<span>{lane_label}</span>"]
    if verified:
        meta_bits.append(f"<span>Verified {e(verified)}</span>")

    money_rows = []
    if price:
        money_rows.append(
            f"<div class='kv'><dt>Live / ask</dt><dd>{e(price)}</dd></div>"
        )
    if close:
        money_rows.append(
            f"<div class='kv'><dt>Closes</dt><dd>{e(close)}</dd></div>"
        )
    sugg = card.get("suggested_bid") or ""
    if sugg:
        money_rows.append(
            f"<div class='kv highlight'><dt>Suggested bid</dt><dd>{e(sugg)}</dd></div>"
        )
    if profit:
        money_rows.append(
            f"<div class='kv highlight'><dt>If it hits</dt><dd>{e(profit)}</dd></div>"
        )
    if loss:
        money_rows.append(
            f"<div class='kv miss'><dt>If it misses</dt><dd>{e(loss)}</dd></div>"
        )

    money_head = ""
    if money_rows:
        money_head = f"<dl class='solds facts'>{''.join(money_rows)}</dl>"

    live_html = f"<p class='live-note'>{e(note)}</p>" if note else ""
    sold_html = render_sold_result(card)

    btn = linkcheck.listing_button_html(card, e)
    hist = ""
    hist_url = alt_sold_history_url(card)
    if hist_url:
        hist = (
            f"<a class='history-btn' href='{e(hist_url)}' target='_blank' "
            "rel='noopener noreferrer'>Alt sold history</a>"
        )
    actions = ""
    if btn or hist:
        actions = f"<div class='card-actions'>{btn}{hist}</div>"

    ended_hidden = " hidden" if (card.get("status") == "ENDED") else ""
    era = card_era(card)
    era_attr = e(era)
    venue = card_venue(card)
    venue_attr = e(venue)
    status_badge = status_badge_html(card)

    return f"""
<article class="card" data-status="{status}" data-lane="{lane}" data-era="{era_attr}" data-venue="{venue_attr}" id="{e(card.get('id') or '')}"{ended_hidden}>
  <header class="card-head">
    <div class="badges">
      {status_badge}
      {venue_badge_html(card)}
      <span class="badge badge-lane">Lane {lane}</span>
      {era_badge_html(card)}
      {header_score_pill(card)}
    </div>
    <h2>{title}, {grade}</h2>
    <p class="card-meta">{' · '.join(meta_bits)}</p>
    {lane5_warn(card)}
    {unverified_note(card)}
  </header>
  {actions}
  {live_html}
  {sold_html}
  {money_head}
  {render_sold(card.get('last_solds') or [])}
  {render_risks(card.get('risks') or [])}
  {render_score_centering(card)}
  {render_gallery(card, images, start_index)}
</article>
"""


CSS = r"""
:root {
  --cream: #F8F1E4;
  --cream-2: #F3E6D0;
  --champagne: #EAD7B4;
  --panel: #FBF3E6;
  --gold: #D9A12E;
  --gold-2: #F2C14E;
  --gold-deep: #A87818;
  --lavender: #C9B5EC;
  --lavender-soft: #E9DEFB;
  --ink: #2E1A47;
  --ink-soft: #4A3563;
  --muted: #6B5A78;
  --line: #E0C98A;
  --pass-bg: #DCE8D4;
  --pass-ink: #2F5A3A;
  --watch-bg: #F6E3A8;
  --watch-ink: #6B4A10;
  --alert-bg: #E9DEFB;
  --alert-ink: #4A2E86;
  --shadow: 0 10px 30px rgba(46, 26, 71, 0.08);
  --radius: 18px;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body {
  margin: 0;
  background:
    radial-gradient(1200px 500px at 10% -10%, #E9DEFB 0%, transparent 55%),
    radial-gradient(900px 400px at 110% 0%, #F2C14E33 0%, transparent 50%),
    var(--cream);
  color: var(--ink);
  font-family: "Segoe UI", "Helvetica Neue", system-ui, sans-serif;
  line-height: 1.5;
  font-size: 16px;
}
img { max-width: 100%; height: auto; display: block; }
a { color: var(--ink); }
.wrap {
  width: min(980px, calc(100% - 1.5rem));
  margin: 0 auto;
  padding: 1.25rem 0 4rem;
}
.site-header {
  padding: 1.4rem 0 1rem;
  border-bottom: 2px solid var(--gold);
}
.site-nav { display: flex; gap: 0.4rem; flex-wrap: wrap; margin: 0 0 0.8rem; }
.site-nav a {
  display: inline-flex; align-items: center; min-height: 40px;
  padding: 0.3rem 0.9rem; border-radius: 999px;
  border: 1px solid var(--champagne); background: var(--panel);
  text-decoration: none; font-weight: 600; font-size: 0.92rem;
}
.site-nav a[aria-current="page"] { background: var(--ink); color: var(--cream); border-color: var(--ink); }
.site-nav a:focus-visible { outline: 3px solid var(--gold-2); outline-offset: 2px; }
.eyebrow {
  margin: 0 0 0.3rem;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  font-size: 0.72rem;
  color: var(--gold-deep);
  font-weight: 700;
}
.site-header h1 {
  margin: 0;
  font-family: Palatino, "Palatino Linotype", "Iowan Old Style", "Times New Roman", serif;
  font-weight: 600;
  font-size: clamp(1.8rem, 5vw, 2.6rem);
  letter-spacing: -0.02em;
  line-height: 1.15;
}
.updated, .intro {
  color: var(--ink-soft);
  margin: 0.45rem 0 0;
}
.counts {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 0.9rem 0 0;
}
.counts span {
  background: var(--lavender-soft);
  border: 1px solid var(--lavender);
  color: var(--ink);
  border-radius: 999px;
  padding: 0.2rem 0.7rem;
  font-size: 0.85rem;
  font-weight: 600;
}
.filters {
  position: sticky;
  top: 0;
  z-index: 5;
  background: color-mix(in srgb, var(--cream) 88%, white 0%);
  backdrop-filter: blur(10px);
  padding: 0.75rem 0 0.55rem;
  margin: 0 -0.75rem;
  padding-left: 0.75rem;
  padding-right: 0.75rem;
  border-bottom: 1px solid var(--line);
}
.filters p {
  margin: 0 0 0.35rem;
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 700;
}
.row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  margin-bottom: 0.55rem;
}
.filters button {
  appearance: none;
  border: 1px solid var(--champagne);
  background: var(--panel);
  color: var(--ink);
  min-height: 44px;
  padding: 0.4rem 0.85rem;
  border-radius: 999px;
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
.search-wrap {
  position: relative;
  margin-bottom: 0.4rem;
}
.search-wrap input {
  width: 100%;
  box-sizing: border-box;
  min-height: 48px;
  padding: 0.55rem 3rem 0.55rem 0.9rem;
  border: 1px solid var(--champagne);
  border-radius: 999px;
  background: var(--panel);
  color: var(--ink);
  font: inherit;
  font-size: 16px;
}
.search-wrap input::-webkit-search-cancel-button { -webkit-appearance: none; display: none; }
.search-wrap input:focus-visible { outline: 3px solid var(--gold-2); outline-offset: 2px; }
.filters .search-clear {
  position: absolute;
  right: 4px;
  top: 50%;
  transform: translateY(-50%);
  min-height: 40px;
  width: 40px;
  padding: 0;
  border: none;
  background: transparent;
  font-size: 1.4rem;
  line-height: 1;
  color: var(--muted);
}
.sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
.filters p.search-count {
  margin: 0 0 0.55rem;
  text-transform: none;
  letter-spacing: normal;
  font-weight: 600;
  font-size: 0.85rem;
  color: var(--muted);
}
.filters button[aria-pressed="true"] {
  background: var(--ink);
  color: var(--cream);
  border-color: var(--ink);
}
.filters button:focus-visible,
.open-btn:focus-visible,
.shot-btn:focus-visible,
.lb-nav:focus-visible {
  outline: 3px solid var(--gold-2);
  outline-offset: 2px;
}
.card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 1.15rem 1rem 1.3rem;
  margin: 1.15rem 0 0;
}
.card-head h2 {
  margin: 0.55rem 0 0.25rem;
  font-size: 1.28rem;
  line-height: 1.3;
  font-family: Palatino, "Palatino Linotype", "Iowan Old Style", serif;
}
.card-meta {
  margin: 0;
  color: var(--muted);
  font-size: 0.9rem;
}
.badges { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.badge {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 800;
  letter-spacing: 0.06em;
}
.badge-PASS { background: var(--pass-bg); color: var(--pass-ink); }
.badge-WATCH { background: var(--watch-bg); color: var(--watch-ink); max-width: 100%; white-space: normal; text-align: left; line-height: 1.25; }
.status-section { margin: 0 0 1.6rem; }
.status-section-title {
  font-size: 1.05rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin: 0 0 0.7rem;
  padding: 0.45rem 0.7rem;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--cream-2);
  color: var(--ink);
}
.status-section-title-PASS { background: var(--pass-bg); color: var(--pass-ink); border-color: transparent; }
.status-section-title-WATCH { background: var(--watch-bg); color: var(--watch-ink); border-color: transparent; }
.status-section-title-ENDED { background: #E8E4DE; color: #3F3A33; }
.badge-ALERT { background: var(--alert-bg); color: var(--alert-ink); }
.badge-UNVERIFIED { background: #E5E1DA; color: #3F3A33; border: 1px dashed #6B5A78; }
.badge-FAIL { background: #F6D5D0; color: #7A1E12; border: 1px solid #C0392B; }
.badge-OWNED { background: #D8E4F2; color: #1F3A5F; border: 1px solid #4A6FA5; }
.badge-ENDED { background: #E8E4DE; color: #3F3A33; border: 1px solid #C4B9A8; }
.warn-unv { margin: 0.5rem 0; padding: 0.55rem 0.75rem; border: 2px dashed #6B5A78; border-radius: 10px; background: #F1EEE8; color: #3F3A33; font-weight: 800; }
.badge-lane { background: var(--cream-2); color: var(--ink-soft); border: 1px solid var(--line); }
.badge-era-vintage { background: var(--cream-2); color: var(--ink-soft); border: 1px solid var(--line); letter-spacing: 0.03em; }
.badge-era-modern { background: #E4EEF2; color: #1F3A5F; border: 1px solid #9BB6C4; letter-spacing: 0.03em; }
.badge-era-unknown { background: #E8E4DE; color: #3F3A33; border: 1px dashed #C4B9A8; letter-spacing: 0.03em; }
.badge-venue { letter-spacing: 0.03em; }
.badge-venue-fanatics { background: #F4D6D6; color: #7A1E12; border: 1px solid #C0392B; }
.badge-venue-alt { background: #1F1F1F; color: #F2C14E; border: 1px solid #1F1F1F; }
.badge-venue-goldin { background: #F2E6C8; color: #6B4A10; border: 1px solid #D9A12E; }
.badge-venue-ebay { background: #D8E4F2; color: #1F3A5F; border: 1px solid #4A6FA5; }
.card-actions {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  margin: 0.9rem 0 0.6rem;
}
.open-btn {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.15rem;
  margin: 0;
  width: 100%;
  min-height: 56px;
  padding: 0.75rem 1rem;
  border-radius: 14px;
  background: linear-gradient(180deg, var(--gold-2), var(--gold));
  color: var(--ink);
  text-decoration: none;
  font-weight: 800;
  font-size: 1.05rem;
  box-shadow: 0 4px 0 var(--gold-deep);
}
.open-btn-removed {
  background: #eee;
  color: var(--ink-soft);
  box-shadow: none;
  font-weight: 700;
  cursor: default;
}
.open-sub {
  font-weight: 500;
  font-size: 0.75rem;
  color: var(--ink-soft);
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.live-note, .note, .hint, .empty, .sold-note {
  color: var(--ink-soft);
  font-size: 0.92rem;
}
.sold-result { margin: 0.15rem 0 0.2rem; }
.sold-link {
  color: var(--ink);
  font-weight: 800;
  text-decoration: underline;
  text-underline-offset: 0.16em;
}
.sold-note { margin: 0.15rem 0 0.45rem; }
.history-btn {
  display: flex;
  align-items: center;
  justify-content: flex-start;
  width: 100%;
  min-height: 48px;
  padding: 0.65rem 1rem;
  border-radius: 14px;
  border: 2px solid var(--gold-deep);
  background: var(--cream-2);
  color: var(--ink);
  text-decoration: none;
  font-weight: 800;
  font-size: 1.05rem;
  box-sizing: border-box;
}
.history-btn:hover { background: var(--panel); }
.hint { margin: 0.5rem 0 0; }
.block { margin-top: 1rem; }
.block h3 {
  margin: 0 0 0.4rem;
  font-size: 0.78rem;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--gold-deep);
}
.solds { margin: 0; }
.kv {
  display: grid;
  grid-template-columns: 8.2rem 1fr;
  gap: 0.35rem 0.8rem;
  padding: 0.45rem 0;
  border-bottom: 1px solid color-mix(in srgb, var(--line) 70%, var(--panel));
}
.kv dt {
  margin: 0;
  font-weight: 700;
  color: var(--ink-soft);
  font-size: 0.92rem;
}
.kv dd { margin: 0; }
.kv.highlight dd { font-weight: 700; color: var(--pass-ink); }
.kv.miss dd { font-weight: 700; color: #7A2E2E; }
.kv.score-row {
  align-items: center;
  margin: 0.15rem 0 0.4rem;
  padding: 0.55rem 0.7rem;
  border-radius: 12px;
  border-bottom: none;
  border: 1.5px solid;
}
.kv.score-row dt {
  font-weight: 800;
  color: var(--ink);
}
.kv.score-row-high { background: #edf8ef; border-color: #86efac; }
.kv.score-row-mid { background: #fdf6e3; border-color: #e8c36a; }
.kv.score-row-low { background: #fdecee; border-color: #f1a8a8; }
.kv.score-row-none { background: #f3eef8; border-color: #c9b5ec; }
.score-dd {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.45rem 0.7rem;
}
.score-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.22rem;
  font-size: 16px;
  font-weight: 800;
  line-height: 1.15;
  padding: 0.28rem 0.72rem;
  border-radius: 999px;
  border: 1.5px solid currentColor;
  letter-spacing: 0.01em;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.score-pill-high { background: #dcfce7; color: #14532d; border-color: #14532d; }
.score-pill-mid { background: #fef3c7; color: #92400e; border-color: #92400e; }
.score-pill-low { background: #fee2e2; color: #991b1b; border-color: #991b1b; }
.score-pill-none { background: #F1EEE8; color: #4A3563; border-color: #6B5A78; }
.card-head .score-pill {
  font-size: 0.95rem;
  padding: 0.2rem 0.65rem;
  letter-spacing: 0.02em;
}
.score-detail {
  flex: 1 1 14rem;
  min-width: 0;
  color: var(--ink);
  font-size: 0.95rem;
  line-height: 1.45;
}
.risks ul { margin: 0; padding-left: 1.15rem; }
.warn-red { margin: 0.5rem 0; padding: 0.55rem 0.75rem; border: 2px solid #B00020; border-radius: 10px; background: #FDE7EA; color: #B00020; font-weight: 800; }
.risks li { margin: 0.2rem 0; }
.shots {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.65rem;
}
.shot[data-kind="centering"],
.shot[data-kind="corners"],
.shot[data-kind="closeup"] {
  grid-column: 1 / -1;
}
.shot {
  margin: 0;
  background: var(--cream-2);
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--line);
}
.shot-btn {
  appearance: none;
  border: 0;
  background: #EFE3CF;
  width: 100%;
  padding: 0;
  cursor: zoom-in;
  display: block;
}
.shot img {
  width: 100%;
  height: auto;
  object-fit: contain;
  background: #EFE3CF;
}
.shot figcaption {
  padding: 0.4rem 0.6rem 0.55rem;
  font-size: 0.82rem;
  color: var(--ink-soft);
}
.empty {
  background: var(--lavender-soft);
  border-radius: var(--radius);
  padding: 1rem;
  margin-top: 1rem;
}
.site-footer {
  margin-top: 2rem;
  color: var(--muted);
  font-size: 0.85rem;
  border-top: 1px solid var(--line);
  padding-top: 1rem;
}
dialog.lightbox {
  border: 0;
  padding: 0;
  background: transparent;
  width: min(1100px, 100vw);
  max-height: 100vh;
}
dialog.lightbox::backdrop {
  background: rgb(46 26 71 / 0.84);
}
.lb-frame {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.6rem;
  padding: 0.6rem;
}
.lb-frame img {
  max-height: calc(100vh - 7rem);
  width: auto;
  max-width: 100%;
  border-radius: 8px;
  background: #EFE3CF;
}
.lb-cap {
  color: var(--cream);
  text-align: center;
  margin: 0;
  font-size: 0.92rem;
}
.lb-bar {
  display: flex;
  gap: 0.5rem;
  justify-content: center;
  flex-wrap: wrap;
}
.lb-nav {
  appearance: none;
  border: 1px solid var(--lavender);
  background: var(--ink);
  color: var(--cream);
  min-height: 44px;
  min-width: 44px;
  padding: 0.4rem 0.9rem;
  border-radius: 999px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}
.filters .ended-hint,
.filters .era-hint {
  margin: 0.35rem 0 0;
  font-size: 0.88rem;
  color: var(--ink-soft);
}
@media (max-width: 520px) {
  .kv { grid-template-columns: 1fr; gap: 0.1rem; }
  .kv.score-row { align-items: start; }
  .score-pill { font-size: 15px; }
  .wrap { width: min(100% - 1.1rem, 980px); }
}
@media (min-width: 800px) {
  .card { padding: 1.4rem 1.4rem 1.6rem; }
}
"""

JS = r"""
(function () {
  const cards = Array.from(document.querySelectorAll(".card"));
  const empty = document.getElementById("empty");
  const statusBtns = Array.from(document.querySelectorAll("[data-filter-status]"));
  const laneBtns = Array.from(document.querySelectorAll("[data-filter-lane]"));
  const eraBtns = Array.from(document.querySelectorAll("[data-filter-era]"));
  const venueBtns = Array.from(document.querySelectorAll("[data-filter-venue]"));
  let status = "ALL";
  let lane = "ALL";
  let era = "ALL";
  let venue = "ALL";
  const q = document.getElementById("q");
  const qClear = document.getElementById("q-clear");
  const qCount = document.getElementById("q-count");
  const STATUSES = ["pass", "watch", "alert", "unverified", "fail", "owned", "ended"];
  const norm = (t) => (t || "").toLowerCase().replace(/\s+/g, " ");
  for (const card of cards) {
    const h2 = card.querySelector(".card-head h2");
    const laneLbl = card.querySelector(".card-meta span");
    const eraLbl = card.querySelector(".badge-era-vintage, .badge-era-modern, .badge-era-unknown");
    const venueLbl = card.querySelector(".badge-venue");
    card.dataset.search = norm([
      h2 ? h2.textContent : "",
      laneLbl ? laneLbl.textContent : "",
      eraLbl ? eraLbl.textContent : "",
      venueLbl ? venueLbl.textContent : "",
      card.dataset.status,
      "lane " + card.dataset.lane,
      card.dataset.era || "",
      card.dataset.venue || "",
    ].join(" "));
  }

  function apply() {
    const terms = norm(q.value).trim().split(" ").filter(Boolean);
    let shown = 0;
    let elsewhere = 0;
    for (const card of cards) {
      const isEnded = card.dataset.status === "ENDED";
      const okStatus = status === "ENDED" ? isEnded
        : status === "ALL" ? !isEnded
        : card.dataset.status === status;
      const okLane = lane === "ALL" || card.dataset.lane === lane;
      const okEra = era === "ALL" || card.dataset.era === era;
      const okVenue = venue === "ALL" || card.dataset.venue === venue;
      const okText = terms.every((t) => STATUSES.includes(t)
        ? card.dataset.status.toLowerCase() === t
        : card.dataset.search.includes(t));
      const on = okStatus && okLane && okEra && okVenue && okText;
      card.hidden = !on;
      if (on) shown += 1;
      else if (okText && terms.length) elsewhere += 1;
    }
    for (const sec of document.querySelectorAll(".status-section")) {
      const any = Array.from(sec.querySelectorAll("article.card")).some((c) => !c.hidden);
      sec.hidden = !any;
    }
    qClear.hidden = q.value === "";
    if (terms.length) {
      let msg = shown + (shown === 1 ? " card matches" : " cards match") + " \u201c" + q.value.trim() + "\u201d";
      if (elsewhere) msg += " (" + elsewhere + " more under other tabs)";
      qCount.textContent = msg;
      empty.textContent = "No cards match \u201c" + q.value.trim() + "\u201d" + (elsewhere ? " in this tab. Try ALL or ENDED." : ".");
    } else {
      qCount.textContent = shown + (shown === 1 ? " card" : " cards");
      empty.textContent = "No cards match these filters.";
    }
    empty.hidden = shown !== 0;
  }
  q.addEventListener("input", apply);
  q.addEventListener("keydown", (ev) => { if (ev.key === "Enter") q.blur(); });
  qClear.addEventListener("click", () => { q.value = ""; apply(); q.focus(); });

  const endedHint = document.getElementById("ended-hint");
  const eraHint = document.getElementById("era-hint");
  const ERA_LABELS = { ALL: "All", vintage: "Vintage", modern: "Modern" };
  function relabelLanes() {
    for (const btn of laneBtns) {
      const id = btn.dataset.filterLane;
      const n = status === "ENDED" ? btn.dataset.countEnded : btn.dataset.countOpen;
      const base = id === "ALL" ? "All lanes" : "Lane " + id;
      btn.textContent = base + " (" + n + ")";
    }
    if (endedHint) endedHint.hidden = status !== "ENDED";
  }
  function relabelEras() {
    for (const btn of eraBtns) {
      const id = btn.dataset.filterEra;
      const n = status === "ENDED" ? btn.dataset.countEnded : btn.dataset.countOpen;
      const base = ERA_LABELS[id] || id;
      btn.textContent = base + " (" + n + ")";
    }
    if (eraHint) {
      const unknown = status === "ENDED"
        ? Number(eraHint.dataset.unknownEnded || 0)
        : Number(eraHint.dataset.unknownOpen || 0);
      eraHint.hidden = unknown === 0;
    }
  }
  function relabelVenues() {
    for (const btn of venueBtns) {
      const id = btn.dataset.filterVenue;
      const n = status === "ENDED" ? btn.dataset.countEnded : btn.dataset.countOpen;
      const base = id === "ALL" ? "All" : id;
      btn.textContent = base + " (" + n + ")";
    }
  }
  function wire(btns, key, set) {
    for (const btn of btns) {
      btn.addEventListener("click", () => {
        set(btn.dataset[key]);
        for (const b of btns) b.setAttribute("aria-pressed", String(b === btn));
        if (key === "filterStatus") { relabelLanes(); relabelEras(); relabelVenues(); }
        apply();
      });
    }
  }
  wire(statusBtns, "filterStatus", (v) => { status = v; });
  wire(laneBtns, "filterLane", (v) => { lane = v; });
  wire(eraBtns, "filterEra", (v) => { era = v; });
  wire(venueBtns, "filterVenue", (v) => { venue = v; });
  relabelLanes();
  relabelEras();
  relabelVenues();
  apply();

  const shots = Array.from(document.querySelectorAll(".shot-btn"));
  const dialog = document.getElementById("lightbox");
  const lbImg = document.getElementById("lb-img");
  const lbCap = document.getElementById("lb-cap");
  let cur = 0;

  function openAt(i) {
    const btn = shots[i];
    if (!btn) return;
    cur = i;
    const img = btn.querySelector("img");
    const cap = btn.parentElement.querySelector("figcaption");
    lbImg.src = img.src;
    lbImg.alt = img.alt || "";
    lbCap.textContent = cap ? cap.textContent : "";
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  shots.forEach((btn) => {
    btn.addEventListener("click", () => openAt(Number(btn.dataset.idx)));
  });
  document.getElementById("lb-prev").addEventListener("click", (ev) => {
    ev.preventDefault();
    openAt((cur - 1 + shots.length) % shots.length);
  });
  document.getElementById("lb-next").addEventListener("click", (ev) => {
    ev.preventDefault();
    openAt((cur + 1) % shots.length);
  });
  document.getElementById("lb-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (ev) => {
    if (ev.target === dialog) dialog.close();
  });
  document.addEventListener("keydown", (ev) => {
    if (!dialog.open) return;
    if (ev.key === "ArrowLeft") openAt((cur - 1 + shots.length) % shots.length);
    if (ev.key === "ArrowRight") openAt((cur + 1) % shots.length);
  });
})();
"""


def render_page(
    cards: list[dict],
    articles: list[str],
    built: str,
    c: dict[str, int],
    data: dict,
    footer: str,
    needs_html: str = "",
) -> str:
    sectioned = wrap_status_sections(cards, articles)
    status_btns = []
    for key, label in (
        ("ALL", f"All ({c['ALL']})"),
        ("PASS", f"PASS ({c['PASS']})"),
        ("WATCH", f"WATCH ({c['WATCH']})"),
        ("ALERT", f"ALERT ({c['ALERT']})"),
        ("UNVERIFIED", f"UNVERIFIED ({c['UNVERIFIED']})"),
        ("FAIL", f"FAIL ({c['FAIL']})"),
        ("OWNED", f"OWNED ({c['OWNED']})"),
        ("ENDED", f"ENDED ({c['ENDED']})"),
    ):
        pressed = "true" if key == "ALL" else "false"
        status_btns.append(
            f"<button type='button' data-filter-status='{key}' "
            f"aria-pressed='{pressed}'>{e(label)}</button>"
        )

    lane_btns = [
        "<button type='button' data-filter-lane='ALL' aria-pressed='true' "
        f"data-count-open='{c['ALL']}' data-count-ended='{c['ENDED']}'>"
        f"All lanes ({c['ALL']})</button>"
    ]
    for lane in LANE_ORDER:
        lane_btns.append(
            f"<button type='button' data-filter-lane='{lane}' aria-pressed='false' "
            f"data-count-open='{c[f'lane-{lane}']}' "
            f"data-count-ended='{c[f'lane-{lane}-ended']}'>"
            f"Lane {lane} ({c[f'lane-{lane}']})</button>"
        )

    era_btns = [
        "<button type='button' data-filter-era='ALL' aria-pressed='true' "
        f"data-count-open='{c['era-ALL']}' data-count-ended='{c['era-ALL-ended']}'>"
        f"All ({c['era-ALL']})</button>",
        "<button type='button' data-filter-era='vintage' aria-pressed='false' "
        f"data-count-open='{c['era-vintage']}' data-count-ended='{c['era-vintage-ended']}'>"
        f"Vintage ({c['era-vintage']})</button>",
        "<button type='button' data-filter-era='modern' aria-pressed='false' "
        f"data-count-open='{c['era-modern']}' data-count-ended='{c['era-modern-ended']}'>"
        f"Modern ({c['era-modern']})</button>",
    ]

    venue_btns = [
        "<button type='button' data-filter-venue='ALL' aria-pressed='true' "
        f"data-count-open='{c['venue-ALL']}' data-count-ended='{c['venue-ALL-ended']}'>"
        f"All ({c['venue-ALL']})</button>"
    ]
    for venue in VENUE_ORDER:
        venue_btns.append(
            f"<button type='button' data-filter-venue='{e(venue)}' aria-pressed='false' "
            f"data-count-open='{c[f'venue-{venue}']}' "
            f"data-count-ended='{c[f'venue-{venue}-ended']}'>"
            f"{e(venue)} ({c[f'venue-{venue}']})</button>"
        )

    disclaimer = e(
        data.get("disclaimer")
        or "CT-verified cards only. Prices are last solds — no max bid."
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#F8F1E4">
  <meta name="color-scheme" content="light">
  <title>Mint 11 – Verified Cards</title>
  <style>{CSS}{needs_photos.CSS}</style>
</head>
<body>
  <div class="wrap">
    <header class="site-header">
      <nav class="site-nav" aria-label="Site">
        <a href="./" aria-current="page">Cards</a>
        <a href="money/">Money</a>
      </nav>
      <p class="eyebrow">Mint 11 Hobby Hub</p>
      <h1>Mint 11 – Verified Cards</h1>
      <p class="updated">Last updated {e(built)}</p>
      <div class="counts" aria-label="Counts by status">
        <span>{c['ALL']} cards</span>
        <span>{c['PASS']} PASS</span>
        <span>{c['WATCH']} WATCH</span>
        <span>{c['ALERT']} ALERT</span>
        <span>{c['UNVERIFIED']} UNVERIFIED</span>
        <span>{c['FAIL']} FAIL</span>
        <span>{c['OWNED']} OWNED</span>
        <span>{c['ENDED']} ENDED</span>
      </div>
      <p class="intro">{disclaimer}</p>
    </header>

    <nav class="filters" aria-label="Filter cards">
      <div class="search-wrap" role="search">
        <label for="q" class="sr-only">Search cards</label>
        <input id="q" type="search" placeholder="Search name, set, #, grade, lane, status, era, venue" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="search">
        <button type="button" class="search-clear" id="q-clear" aria-label="Clear search" hidden>&times;</button>
      </div>
      <p class="search-count" id="q-count" aria-live="polite"></p>
      <p>Status</p>
      <div class="row">{''.join(status_btns)}</div>
      <p>Venue</p>
      <div class="row">{''.join(venue_btns)}</div>
      <p>Era</p>
      <div class="row">{''.join(era_btns)}</div>
      <p class="era-hint" id="era-hint" data-unknown-open="{c['era-unknown']}" data-unknown-ended="{c['era-unknown-ended']}" hidden>Cards with no known year show under All only.</p>
      <p>Lane</p>
      <div class="row">{''.join(lane_btns)}</div>
      <p class="ended-hint" id="ended-hint" hidden>ENDED cards stay on the site for 7 days after the auction closes. Pick a lane to view that lane&rsquo;s ended cards.</p>
    </nav>

    <main id="list">
      {sectioned}
      <p class="empty" id="empty" hidden>No cards match these filters.</p>
    </main>

    {needs_html}

    <footer class="site-footer">
      <p>{footer}</p>
    </footer>
  </div>

  <dialog class="lightbox" id="lightbox" aria-label="Enlarged photo">
    <div class="lb-frame">
      <img id="lb-img" alt="">
      <p class="lb-cap" id="lb-cap"></p>
      <div class="lb-bar">
        <button type="button" class="lb-nav" id="lb-prev">Previous</button>
        <button type="button" class="lb-nav" id="lb-next">Next</button>
        <button type="button" class="lb-nav" id="lb-close">Close</button>
      </div>
    </div>
  </dialog>
  <script>{JS}</script>
</body>
</html>
"""



FRONT_CROPPED_CAPTION = "Card front (label cropped)"
BACK_CROPPED_CAPTION = "Card back (label cropped)"


def warn_missing_label_cropped(cards: list[dict]) -> list[str]:
    """Frank standing rule (24 Nov 2026): every card on the site must
    open with a clean label-cropped front and back photo (no slab
    labels, cert numbers, or barcodes). Print a loud WARNING for any
    card that is missing them or has them out of order, but do not
    fail the build — archived or legacy cards may still lack a pair.
    """
    warnings: list[str] = []
    for card in cards:
        cid = card.get("id") or "?"
        imgs = card.get("images") or []
        caps = [(im.get("caption") or "").strip() for im in imgs]
        has_front = FRONT_CROPPED_CAPTION in caps
        has_back = BACK_CROPPED_CAPTION in caps
        first_ok = (
            len(caps) >= 2
            and caps[0] == FRONT_CROPPED_CAPTION
            and caps[1] == BACK_CROPPED_CAPTION
        )
        if first_ok:
            continue
        if not has_front or not has_back:
            missing = []
            if not has_front:
                missing.append("front")
            if not has_back:
                missing.append("back")
            msg = (
                f"WARNING: missing front/back — {cid} is missing "
                f"label-cropped {' and '.join(missing)} photo(s). "
                "Every card should start with "
                f"{FRONT_CROPPED_CAPTION!r} and {BACK_CROPPED_CAPTION!r}."
            )
        else:
            msg = (
                f"WARNING: missing front/back — {cid} has label-cropped "
                "front and back photos, but they are not the first two images."
            )
        warnings.append(msg)
        print(msg, file=sys.stderr)
        print(msg)
    if warnings:
        print(
            f"WARNING: missing front/back on {len(warnings)}/"
            f"{len(cards)} cards (build continues)."
        )
    else:
        print(
            f"Label-cropped front/back: {len(cards)}/{len(cards)} cards OK"
        )
    return warnings



# Centering-sheet hard gate (24 Nov 2026). Open cards must carry a
# centering.py JSON for each face; a broken or mismatched sheet cannot
# be published silently.
_RATIO_LR = re.compile(r"L/R\s+~?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", re.I)
_RATIO_TB = re.compile(r"T/B\s+~?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", re.I)
_RATIO_TR = re.compile(r"T/R\s+~?\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", re.I)
# "e-card thin pair top vs right 50.7/49.3" or "thin pair 50.7/49.3"
_RATIO_PAIR = re.compile(
    r"(?:thin pair[^0-9]{0,40}|top vs right\s+)(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)",
    re.I,
)
CENTERING_RATIO_TOL_PPT = 1.0


def _load_centering_json(path: object) -> dict | None:
    if not path:
        return None
    pth = Path(str(path))
    if not pth.is_file():
        return None
    try:
        return json.loads(pth.read_text(encoding="utf-8"))
    except Exception:
        return None


def _ratios_from_text(text: object) -> dict[str, tuple[float, float]]:
    s = str(text or "")
    out: dict[str, tuple[float, float]] = {}
    m = _RATIO_LR.search(s)
    if m:
        out["LR"] = (float(m.group(1)), float(m.group(2)))
    m = _RATIO_TB.search(s)
    if m:
        out["TB"] = (float(m.group(1)), float(m.group(2)))
    m = _RATIO_TR.search(s)
    if m:
        out["TR"] = (float(m.group(1)), float(m.group(2)))
    if "TR" not in out:
        m = _RATIO_PAIR.search(s)
        if m:
            out["TR"] = (float(m.group(1)), float(m.group(2)))
    return out


def _pair_from_json(data: dict, key: str) -> tuple[float, float] | None:
    mapping = {"LR": "lr_pair", "TB": "tb_pair", "TR": "tr_pair"}
    pair = data.get(mapping[key])
    if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] is not None and pair[1] is not None:
        return float(pair[0]), float(pair[1])
    return None


def centering_check(cards: list[dict], *, open_only: bool = True) -> list[str]:
    """Hard-fail checks for every open card's centering JSON + caption ratios.

    Fails if an open card is missing a centering JSON (once the card uses
    centering_json_front / centering_json_back), if sanity_ok is not true,
    if any station width is <= 0, if an unverified same-side spread exceeds
    5 px, or if the L/R, T/B or T/R ratio in cards.json differs from the
    JSON by more than 1.0 percentage point.
    """
    errors: list[str] = []
    checked = 0
    for card in cards:
        status = str(card.get("status") or "")
        if open_only and status in {"ENDED", "ended", "SOLD"}:
            continue
        cid = card.get("id") or "?"
        unm = card.get("centering_unmeasurable") or {}
        for face, field in (("front", "centering_json_front"), ("back", "centering_json_back")):
            unm_reason = unm.get(face) or unm.get(face.upper()) or unm.get(face.capitalize())
            watch_unm = str(card.get("status") or "") == "WATCH" and bool(unm_reason)
            path = card.get(field)
            if not path:
                if watch_unm:
                    print(
                        f"NOTE: {cid} {face} centering unmeasurable "
                        f"({unm_reason}); JSON omitted."
                    )
                    continue
                errors.append(
                    f"ERROR: centering JSON missing — {cid} has no {field}."
                )
                continue
            data = _load_centering_json(path)
            if data is None:
                errors.append(
                    f"ERROR: centering JSON invalid or unreadable — {cid} {face}: {path}"
                )
                continue
            checked += 1
            if data.get("sanity_ok") is not True:
                if watch_unm:
                    print(
                        f"NOTE: {cid} {face} centering unmeasurable "
                        f"({unm_reason}); skipped sanity gate."
                    )
                    continue
                errs = data.get("errors") or ["sanity_ok is not true"]
                errors.append(
                    f"ERROR: centering sanity_ok!=true — {cid} {face}: "
                    + "; ".join(str(e) for e in errs)
                )
            stations = data.get("stations") or {}
            for name, st in stations.items():
                w = st.get("width") if isinstance(st, dict) else None
                if w is None:
                    errors.append(
                        f"ERROR: centering station missing width — {cid} {face} {name}"
                    )
                elif float(w) <= 0:
                    errors.append(
                        f"ERROR: centering width <= 0 — {cid} {face} {name} width={w}"
                    )
            # unverified spread > 5 px
            spread = data.get("spread_px") or {}
            note = (data.get("note") or "").strip()
            rail = set(s.upper() for s in (data.get("rail_occluded") or []))
            ratio_sides = [s.upper() for s in (data.get("ratio_sides") or list("LRTB"))]
            for side, sp in spread.items():
                if sp is None or side.upper() not in ratio_sides:
                    continue
                if float(sp) > 5.0 and not (note and (side.upper() in rail or "spread" in note.lower() or "taper" in note.lower() or "override" in note.lower() or "verified" in note.lower())):
                    # If sanity_ok is already true, a verified override is on the JSON;
                    # only fail here when the JSON itself did not record a note.
                    if not note:
                        errors.append(
                            f"ERROR: unverified width spread {float(sp):.2f} px > 5 — {cid} {face} side {side}"
                        )
            # ratio vs cards.json caption
            text_key = "centering_front" if face == "front" else "centering_back"
            text_ratios = _ratios_from_text(card.get(text_key))
            for key, text_pair in text_ratios.items():
                json_pair = _pair_from_json(data, key)
                if json_pair is None:
                    errors.append(
                        f"ERROR: cards.json has {key} text for {cid} {face} but JSON has no {key} pair"
                    )
                    continue
                d0 = abs(text_pair[0] - json_pair[0])
                d1 = abs(text_pair[1] - json_pair[1])
                if d0 > CENTERING_RATIO_TOL_PPT or d1 > CENTERING_RATIO_TOL_PPT:
                    errors.append(
                        f"ERROR: centering ratio mismatch > {CENTERING_RATIO_TOL_PPT} ppt — "
                        f"{cid} {face} {key} text {text_pair[0]:.1f}/{text_pair[1]:.1f} vs "
                        f"JSON {json_pair[0]:.1f}/{json_pair[1]:.1f}"
                    )
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
            print(e)
        print(f"ERROR: centering_check failed with {len(errors)} issue(s). Refusing to build.")
        return errors
    print(f"Centering JSON gate: {checked} open-card faces OK")
    return []



def selfcheck_override_reason(card: dict, face: str) -> str | None:
    """Per-card, per-face reason to accept a selfcheck FAIL after CT eye review.

    The selfcheck still runs. This does not change thresholds or skip other
    cards. A blank or missing reason is ignored, so the normal gate applies.
    """
    ov = card.get("selfcheck_override") or {}
    if not isinstance(ov, dict):
        return None
    reason = ov.get(face) or ov.get(face.upper()) or ov.get(face.capitalize())
    if isinstance(reason, str) and reason.strip():
        return reason.strip()
    return None


def centering_selfcheck_gate(cards: list[dict], *, open_only: bool = True) -> list[str]:
    """Run centering_selfcheck.py on every open card. Hard-fail on any FAIL.

    Warnings (spread, dark rim, low-res uncertainty) are printed but do not
    block the build. A missing script or a selfcheck exception is a fail.

    A card may set selfcheck_override.{front|back} to a non-empty reason.
    That face is still audited; if it FAILs, the reason is logged and that
    face alone is allowed through. Every other face still uses the normal gate.
    """
    tools = Path("/workspace/ct_kb/tools")
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    try:
        import centering_selfcheck as _csc
    except Exception as e:
        print(f"ERROR: cannot import centering_selfcheck: {e}", file=sys.stderr)
        return [f"cannot import centering_selfcheck: {e}"]

    errors: list[str] = []
    overridden: list[str] = []
    n = 0
    for card in cards:
        status = str(card.get("status") or "")
        if open_only and status in {"ENDED", "ended", "SOLD"}:
            continue
        cid = card.get("id") or "?"
        unm = card.get("centering_unmeasurable") or {}
        for face, field in (("front", "centering_json_front"), ("back", "centering_json_back")):
            unm_reason = unm.get(face) or unm.get(face.upper()) or unm.get(face.capitalize())
            if str(card.get("status") or "") == "WATCH" and unm_reason:
                continue
            path = card.get(field)
            if not path:
                continue
            try:
                res = _csc.audit_card(Path(path))
            except Exception as e:
                errors.append(f"ERROR: selfcheck crashed — {cid} {face}: {e}")
                continue
            n += 1
            if not res.get("ok", False):
                fails = res.get("fails") or ["selfcheck failed"]
                reason = selfcheck_override_reason(card, face)
                if reason:
                    fail_txt = "; ".join(str(f) for f in fails)
                    print(
                        f"NOTE: {cid} {face} selfcheck FAIL overridden "
                        f"({fail_txt}). Reason: {reason}"
                    )
                    overridden.append(f"{cid} {face}")
                    continue
                for f in fails:
                    errors.append(f"ERROR: selfcheck FAIL — {cid} {face}: {f}")
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
            print(e)
        print(f"ERROR: centering_selfcheck failed with {len(errors)} issue(s). Refusing to build.")
        return errors
    if overridden:
        print(
            f"Centering selfcheck gate: {n} open-card faces checked, "
            f"{n - len(overridden)} OK through the normal gate, "
            f"{len(overridden)} fail overridden ({', '.join(overridden)})"
        )
    else:
        print(f"Centering selfcheck gate: {n} open-card faces OK")
    return []


def build() -> None:
    data = load_cards()
    cards = data["cards"]
    now = datetime.now(ICT)
    apply_ended_status(cards, now)
    cards, pruned = archive_ended_older_than(cards, now)
    data["cards"] = cards
    built = now.strftime("%-d %b %Y, %H:%M Asia/Bangkok (ICT)")
    c = counts(cards)
    if pruned:
        print("Pruned ENDED >7d: " + ", ".join(str(x.get("id")) for x in pruned))
    print(
        "Status counts: "
        f"All {c['ALL']} (open), "
        f"PASS {c['PASS']}, WATCH {c['WATCH']}, "
        f"ALERT {c['ALERT']}, UNVERIFIED {c['UNVERIFIED']}, FAIL {c['FAIL']}, OWNED {c['OWNED']}, "
        f"ENDED {c['ENDED']}"
        f" (L1 {c['lane-1-ended']}, L2 {c['lane-2-ended']}, L5 {c['lane-5-ended']})"
    )
    print(
        "Era counts: "
        f"Vintage {c['era-vintage']} open / {c['era-vintage-ended']} ended, "
        f"Modern {c['era-modern']} open / {c['era-modern-ended']} ended, "
        f"Year unknown {c['era-unknown']} open / {c['era-unknown-ended']} ended"
    )

    warn_missing_label_cropped(cards)

    centering_errors = centering_check(cards, open_only=True)
    if centering_errors:
        sys.exit(1)
    selfcheck_errors = centering_selfcheck_gate(cards, open_only=True)
    if selfcheck_errors:
        sys.exit(1)
    link_errors = linkcheck.check_cards(cards)
    if link_errors:
        for err in link_errors:
            print(err, file=sys.stderr)
            print(err)
        print(f"ERROR: link check failed with {len(link_errors)} dead link(s). Refusing to build.")
        sys.exit(1)

    if PAGES_DIR.exists():
        shutil.rmtree(PAGES_DIR)
    PAGES_IMG.mkdir(parents=True)

    embedded_articles: list[str] = []
    pages_articles: list[str] = []
    gallery_count = 0
    skipped_missing: list[str] = []

    for card in cards:
        ready_embedded: list[dict] = []
        ready_pages: list[dict] = []
        img_index = 0
        for img in card.get("images") or []:
            path = Path(img["path"])
            if not path.is_file():
                skipped_missing.append(f"{card.get('id')}: missing {path}")
                print(f"SKIP missing: {path}", file=sys.stderr)
                continue
            jpeg, w, h = prepare_image(path)
            meta = {
                "width": w,
                "height": h,
                "alt": img.get("alt") or "",
                "caption": img.get("caption") or "",
                "kind": img.get("kind") or "photo",
                "path": str(path),
            }
            ready_embedded.append({**meta, "uri": data_uri_from_jpeg(jpeg)})
            dest_name = page_image_name(card, img_index, meta["kind"])
            (PAGES_IMG / dest_name).write_bytes(jpeg)
            ready_pages.append({**meta, "uri": f"images/{dest_name}"})
            img_index += 1
        start = gallery_count
        gallery_count += len(ready_embedded)
        embedded_articles.append(render_card(card, ready_embedded, start))
        pages_articles.append(render_card(card, ready_pages, start))

    np_entries = needs_photos.load_entries(now)
    np_embed: dict[str, str] = {}
    np_pages: dict[str, str] = {}
    for it in np_entries:
        tp = needs_photos.thumb_ok(it)
        if tp is None:
            continue
        jpeg, _w, _h = prepare_image(tp)
        lid = str(it.get("listing_id"))
        np_embed[lid] = data_uri_from_jpeg(jpeg)
        name = "needsphotos_" + re.sub(r"[^A-Za-z0-9]+", "_", lid) + ".jpg"
        (PAGES_IMG / name).write_bytes(jpeg)
        np_pages[lid] = f"images/{name}"
    np_html_embed = needs_photos.render_section(np_entries, np_embed)
    np_html_pages = needs_photos.render_section(np_entries, np_pages)
    print(f"Needs photos: {len(np_entries)} listed "
          f"({sum(1 for x in np_entries if not x['_ended'])} live), "
          f"{len(np_pages)} with label-cropped thumbnails")

    offline_footer = (
        "Open this file in any browser, including a phone. "
        "It does not need the internet once saved. "
        "Add a card by appending it to cards.json and re-running build.py."
    )
    hosted_footer = (
        "Open this page in any browser, including a phone. "
        "Add a card by appending it to cards.json and re-running build.py."
    )

    OUT_PATH.write_text(
        render_page(cards, embedded_articles, built, c, data, offline_footer, np_html_embed),
        encoding="utf-8",
    )
    pages_index = PAGES_DIR / "index.html"
    pages_index.write_text(
        render_page(cards, pages_articles, built, c, data, hosted_footer, np_html_pages),
        encoding="utf-8",
    )
    (PAGES_DIR / ".nojekyll").write_text("", encoding="utf-8")
    for extra in STATIC_EXTRAS:
        src = HERE / extra
        if src.is_dir():
            shutil.copytree(src, PAGES_DIR / extra, dirs_exist_ok=True)

    size = OUT_PATH.stat().st_size
    pages_size = pages_index.stat().st_size
    img_bytes = sum(p.stat().st_size for p in PAGES_IMG.glob("*.jpg"))
    img_count = len(list(PAGES_IMG.glob("*.jpg")))
    print(f"Wrote {OUT_PATH} ({size} bytes, {size/1024/1024:.2f} MB)")
    print(
        f"Wrote {pages_index} ({pages_size} bytes) and "
        f"{img_count} images ({img_bytes/1024/1024:.2f} MB)"
    )
    print(f"Cards: {len(cards)}; images: {gallery_count}")
    if skipped_missing:
        print("Missing images:")
        for line in skipped_missing:
            print(" ", line)



if __name__ == "__main__":
    build()
