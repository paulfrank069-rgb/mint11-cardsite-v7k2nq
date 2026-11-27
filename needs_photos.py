"""'Needs better photos' section (Frank approved 26 Nov 2026 23:44 ICT;
non-eBay venues added 27 Nov 2026).

Reads the lane bots' NEEDS_PHOTOS.md files (KB v2.23): money-screen-passing
cards (eBay, Fanatics, Alt, or Goldin) that are UNVERIFIED only because
listing photos are too small (card width < 1,100 px). These are NEVER PASS
cards; they get a separate 'NEEDS PHOTOS' badge and a provisional photo score.

NEEDS_PHOTOS.md format: any markdown, plus one fenced block that starts with
```json needs_photos  and holds a JSON list of entries:
  listing_id, url, name, grade, lane ("A"/"B"/"C"), strategy,
  price_text, ends_at_ict ("YYYY-MM-DD HH:MM ICT" or null for buy-now),
  photo_width_px, provisional_score, money_screen ("PASS" required),
  grade_verdict ("UNVERIFIED" required), money_outcome,
  photos_to_request (optional list), thumb_path (absolute, label-cropped),
  thumb_label_cropped (must be true to show the thumbnail),
  status ("LIVE"/"ENDED"), ended_at_ict (optional), checked_at_ict.
"""
from __future__ import annotations

import glob
import html
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ICT = ZoneInfo("Asia/Bangkok")
GLOBS = ["/workspace/laneA/*/NEEDS_PHOTOS.md", "/workspace/laneB/*/NEEDS_PHOTOS.md",
         "/workspace/laneC/*/NEEDS_PHOTOS.md"]
KEEP_DAYS = 7
DEFAULT_ASK = [
    "Front and back of the card, each filling the frame (card at least 1,100 px wide)",
    "Close-ups of all 4 corners, front and back",
    "Close-ups of all 4 edges, front and back",
    "Glare-free shots, straight on, out of the slab sleeve if possible (unslabbed or no reflections on the case)",
]
BLOCK_RE = re.compile(r"```json\s+needs_photos\s*\n(.*?)```", re.S)
CERTLIKE = re.compile(r"(?<![\d.,$])\d{8,10}(?![\d.,])")

VENUE_ORDER = ("Fanatics", "Alt", "Goldin", "eBay")


def venue_from_url(url: object) -> str | None:
    """Work the venue out from the listing URL."""
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


def e(x: object) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def scrub(x: object) -> str:
    """Never show cert-like numbers in text (URLs are handled separately)."""
    return CERTLIKE.sub("[number hidden]", "" if x is None else str(x))


def parse_ict(text: object) -> datetime | None:
    if not text:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", str(text))
    if not m:
        return None
    return datetime.strptime(f"{m[1]} {m[2]}:{m[3]}", "%Y-%m-%d %H:%M").replace(tzinfo=ICT)


def load_entries(now: datetime, log=print) -> list[dict]:
    files = []
    for g in GLOBS:
        files += glob.glob(g)
    files.sort(key=lambda p: Path(p).stat().st_mtime)  # newest last wins
    by_id: dict[str, dict] = {}
    for f in files:
        txt = Path(f).read_text(encoding="utf-8", errors="replace")
        for block in BLOCK_RE.findall(txt):
            try:
                items = json.loads(block)
            except json.JSONDecodeError as exc:
                log(f"NEEDS_PHOTOS: bad JSON in {f}: {exc}")
                continue
            for it in items:
                it = dict(it)
                it["_source"] = f
                by_id[str(it.get("listing_id"))] = it
    out = []
    for lid, it in by_id.items():
        url = str(it.get("url") or "")
        venue = venue_from_url(url)
        if not venue:
            log(f"NEEDS_PHOTOS skip {lid}: not a known listing URL")
            continue
        it["_venue"] = venue
        if str(it.get("money_screen", "")).upper() != "PASS":
            log(f"NEEDS_PHOTOS skip {lid}: money screen not passed")
            continue
        if str(it.get("grade_verdict", "")).upper() != "UNVERIFIED":
            log(f"NEEDS_PHOTOS skip {lid}: grade verdict is not UNVERIFIED")
            continue
        end = parse_ict(it.get("ends_at_ict")) or parse_ict(it.get("ended_at_ict"))
        ended = str(it.get("status", "")).upper() == "ENDED" or (end is not None and end <= now)
        if ended:
            if end is None:
                log(f"NEEDS_PHOTOS skip {lid}: ENDED with no end time")
                continue
            if now - end > timedelta(days=KEEP_DAYS):
                log(f"NEEDS_PHOTOS drop {lid}: ENDED more than {KEEP_DAYS} days ago")
                continue
        it["_ended"] = ended
        it["_end"] = end
        out.append(it)
    # live first, soonest-ending first, buy-nows (no end) after auctions
    out.sort(key=lambda x: (x["_ended"], x["_end"] is None, x["_end"] or now))
    return out


def thumb_ok(it: dict) -> Path | None:
    p = it.get("thumb_path")
    if not p or it.get("thumb_label_cropped") is not True:
        return None
    path = Path(p)
    return path if path.is_file() else None


def render_section(entries: list[dict], thumbs: dict[str, str]) -> str:
    rows = []
    for it in entries:
        lid = str(it.get("listing_id"))
        badge = ("<span class='np-badge np-ended'>ENDED</span>" if it["_ended"]
                 else "<span class='np-badge'>NEEDS PHOTOS</span>")
        uri = thumbs.get(lid)
        thumb = (f"<img class='np-thumb' src='{e(uri)}' alt='{e(scrub(it.get('name')))} (label cropped)' loading='lazy'>"
                 if uri else "<div class='np-thumb np-nothumb'>No label-cropped photo yet</div>")
        end_txt = it.get("ends_at_ict") or "Buy-now (no end time)"
        asks = it.get("photos_to_request") or DEFAULT_ASK
        asks_html = "".join(f"<li>{e(scrub(a))}</li>" for a in asks)
        rows.append(
            "<article class='np-item'>"
            f"{thumb}<div class='np-body'>"
            f"<div class='np-head'>{badge}<h3>{e(scrub(it.get('name')))}</h3></div>"
            "<dl class='np-dl'>"
            f"<dt>Grade</dt><dd>{e(scrub(it.get('grade')))}</dd>"
            f"<dt>Listing</dt><dd><a href='{e(it.get('url'))}' rel='noopener' target='_blank'>{e(it.get('_venue') or venue_from_url(it.get('url')) or 'Listing')} listing</a></dd>"
            f"<dt>Price / bid</dt><dd>{e(scrub(it.get('price_text')))}</dd>"
            f"<dt>Ends</dt><dd>{e(scrub(end_txt))}</dd>"
            f"<dt>Lane</dt><dd>Lane {e(it.get('lane'))} · {e(scrub(it.get('strategy')))}</dd>"
            f"<dt>Photo score</dt><dd><strong>Provisional – photos too small</strong>: {e(scrub(it.get('provisional_score') or 'not scored'))}"
            f" (card about {e(scrub(it.get('photo_width_px') or '?'))} px wide; 1,100 px needed)</dd>"
            f"<dt>Money</dt><dd>{e(scrub(it.get('money_outcome')))}</dd>"
            f"<dt>Checked</dt><dd>{e(scrub(it.get('checked_at_ict')))}</dd>"
            "</dl>"
            f"<p class='np-ask-h'>Photos to ask the seller for</p><ul class='np-ask'>{asks_html}</ul>"
            "</div></article>"
        )
    live = sum(1 for x in entries if not x["_ended"])
    body = "".join(rows) if rows else (
        "<p class='np-empty'>No cards need photos right now. Cards appear here when a lane "
        "finds a listing that clears the money screen but has photos too small to grade.</p>")
    return (
        "<section class='needs-photos' id='needs-photos' aria-labelledby='np-title'>"
        f"<h2 id='np-title'>Needs better photos ({live})</h2>"
        "<p class='np-intro'>These are not verified cards. Listing photos are below the 1,100 px "
        "needed to grade corners, edges and centering, so they stay unverified. Each one clears "
        "the money screen; the photo score is provisional until the seller sends better photos. "
        "Prices are last solds — no max bid.</p>"
        f"{body}</section>"
    )


CSS = """
.needs-photos { margin: 28px 0 8px; padding: 16px; border: 2px dashed #B7791F; border-radius: 12px; background: #FFF8EC; }
.needs-photos h2 { margin: 0 0 6px; font-size: 1.2rem; }
.np-intro, .np-empty { font-size: .92rem; color: #5A4A2F; }
.np-item { display: flex; gap: 14px; padding: 12px 0; border-top: 1px solid #E8D9BE; flex-wrap: wrap; }
.np-thumb { width: 140px; height: auto; border-radius: 6px; object-fit: contain; background: #fff; }
.np-nothumb { height: 190px; display: flex; align-items: center; justify-content: center; text-align: center; font-size: .8rem; color: #7A6A50; border: 1px dashed #C9B48F; }
.np-body { flex: 1 1 260px; min-width: 0; }
.np-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.np-head h3 { margin: 0; font-size: 1rem; }
.np-badge { background: #B7791F; color: #fff; font-weight: 700; font-size: .75rem; padding: 3px 8px; border-radius: 999px; letter-spacing: .03em; }
.np-badge.np-ended { background: #8A8172; }
.np-dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 10px; font-size: .9rem; margin: 8px 0; }
.np-dl dt { font-weight: 600; color: #5A4A2F; }
.np-dl dd { margin: 0; overflow-wrap: anywhere; }
.np-ask-h { font-weight: 600; margin: 6px 0 2px; font-size: .9rem; }
.np-ask { margin: 0; padding-left: 18px; font-size: .88rem; }
"""
