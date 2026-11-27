#!/usr/bin/env python3
"""Build-time and self-check for card listing / sold-history links.

Alt /itm/:id is a listing page (publicListing). Alt /itm/:id/research is an
asset page (asset(id:)). A listing UUID on a research URL renders
"Error loading asset". HTTP 200 on the SPA shell is not enough.

Open-card (not ENDED) dead links fail the self-check. Any dead href is
flagged; the build also refuses to emit one.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALT_GQL = "https://alt-platform-server.production.internal.onlyalt.com/graphql/Query"
ALT_ITM_RE = re.compile(
    r"^https?://(?:www\.)?alt\.xyz/itm/([0-9a-fA-F-]{36})"
    r"(?:/(research|sold|external))?/?(?:\?.*)?$",
    re.I,
)
OPEN_FAIL_STATUSES = {"ENDED", "ended", "SOLD"}
_LAST = [0.0]
MIN_GAP_S = 6.0
UA = "Mozilla/5.0 (compatible; mint11-cardsite-linkcheck/1.0)"


def _gate() -> None:
    wait = MIN_GAP_S - (time.time() - _LAST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST[0] = time.time()


def gql(query: str, variables: dict, what: str) -> dict:
    _gate()
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        ALT_GQL,
        data=body,
        headers={
            "content-type": "application/json",
            "user-agent": UA,
            "origin": "https://alt.xyz",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode()
            code = r.status
    except urllib.error.HTTPError as e:
        txt = e.read().decode() if e.fp else ""
        code = e.code
        raise RuntimeError(f"{what} HTTP {code}: {txt[:300]}") from e
    except Exception as e:
        raise RuntimeError(f"{what} failed: {e!r}") from e
    data = json.loads(txt)
    if data.get("errors") and not data.get("data"):
        raise RuntimeError(f"{what} graphql errors: {json.dumps(data['errors'])[:400]}")
    print(
        f"{datetime.now():%Y-%m-%d %H:%M:%S} ICT linkcheck {what} HTTP {code}",
        flush=True,
    )
    return data


def classify_alt(url: str) -> tuple[str, str] | None:
    m = ALT_ITM_RE.match((url or "").strip())
    if not m:
        return None
    kind = (m.group(2) or "listing").lower()
    return kind, m.group(1)


def card_is_open(card: dict) -> bool:
    return str(card.get("status") or "") not in OPEN_FAIL_STATUSES


def urls_for_card(card: dict) -> list[tuple[str, str]]:
    """URLs the site would emit for this card (no dead-link hrefs)."""
    # Imported lazily to avoid a circular import at module load when
    # build.py imports linkcheck.
    from build import alt_sold_history_url

    out: list[tuple[str, str]] = []
    if card.get("listing_removed"):
        pass
    else:
        link = (card.get("link") or "").strip()
        if link:
            out.append(("listing", link))
    hist = (alt_sold_history_url(card) or "").strip()
    if hist:
        out.append(("history", hist))
    sold = (card.get("sold_link") or "").strip()
    if sold:
        out.append(("sold_link", sold))
    return out


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def probe_alt_ids(listing_ids: list[str], asset_ids: list[str]) -> dict:
    """Return {listing_id: publicListing|None, asset_id: asset|None}."""
    listings: dict[str, dict | None] = {}
    assets: dict[str, dict | None] = {}
    unique_l = list(dict.fromkeys(listing_ids))
    unique_a = list(dict.fromkeys(asset_ids))

    for chunk in _chunks(unique_l, 8):
        decls, parts, variables = [], [], {}
        for i, uid in enumerate(chunk):
            k = f"L{i}"
            decls.append(f"${k}:ID!")
            parts.append(
                f"{k}: publicListing(id:${k}){{ publicListing {{ id state type }} }}"
            )
            variables[k] = uid
        q = "query(" + ",".join(decls) + "){" + " ".join(parts) + "}"
        data = gql(q, variables, f"publicListing x{len(chunk)}")
        d = data.get("data") or {}
        errs = {e.get("path", [None])[0]: e for e in (data.get("errors") or []) if e.get("path")}
        for i, uid in enumerate(chunk):
            if f"L{i}" in errs and not d.get(f"L{i}"):
                listings[uid] = None
            else:
                rec = d.get(f"L{i}") or {}
                listings[uid] = rec.get("publicListing")
    for chunk in _chunks(unique_a, 15):
        decls, parts, variables = [], [], {}
        for i, uid in enumerate(chunk):
            k = f"A{i}"
            decls.append(f"${k}:ID!")
            parts.append(f"{k}: asset(id:${k}){{ id name }}")
            variables[k] = uid
        q = "query(" + ",".join(decls) + "){" + " ".join(parts) + "}"
        data = gql(q, variables, f"asset x{len(chunk)}")
        d = data.get("data") or {}
        for i, uid in enumerate(chunk):
            assets[uid] = d.get(f"A{i}")
    return {"listings": listings, "assets": assets}


def probe_http(url: str) -> tuple[bool, str]:
    """Light check for non-Alt pages (Fanatics, Goldin, eBay)."""
    req = urllib.request.Request(url, method="GET", headers={"user-agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            code = r.status
            body = r.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, repr(e)
    if code >= 400:
        return False, f"HTTP {code}"
    low = body.lower()
    if "page not found" in low or "pages we can't find" in low:
        return False, "page-not-found body"
    return True, f"HTTP {code}"


def evaluate_url(url: str, probes: dict) -> tuple[bool, str]:
    kind_id = classify_alt(url)
    if kind_id:
        kind, uid = kind_id
        if kind == "research":
            rec = (probes.get("assets") or {}).get(uid)
            if rec and rec.get("id"):
                return True, f"asset {rec.get('name') or rec['id']}"
            return False, "Error loading asset (asset(id) is empty — not an Alt asset id)"
        if kind == "listing":
            rec = (probes.get("listings") or {}).get(uid)
            if rec and rec.get("id"):
                return True, f"listing {rec.get('state') or ''} {rec.get('type') or ''}".strip()
            return False, "listing page would not load (publicListing is empty)"
        if kind in {"sold", "external"}:
            # sold needs a transaction id; external is a listing-like id.
            rec = (probes.get("listings") or {}).get(uid)
            if rec and rec.get("id"):
                return True, f"{kind} via listing {rec.get('state')}"
            # A sold URL may be a transaction; we only accept if we have evidence.
            return False, f"/{kind} id is not a live public listing"
        return False, f"unhandled Alt path kind {kind}"
    ok, detail = probe_http(url)
    return ok, detail


def check_urls(urls: list[str]) -> list[dict]:
    listing_ids, asset_ids = [], []
    for url in urls:
        kind_id = classify_alt(url)
        if not kind_id:
            continue
        kind, uid = kind_id
        if kind == "research":
            asset_ids.append(uid)
        else:
            listing_ids.append(uid)
    probes = {"listings": {}, "assets": {}}
    if listing_ids or asset_ids:
        probes = probe_alt_ids(listing_ids, asset_ids)
    rows = []
    for url in urls:
        if classify_alt(url):
            ok, detail = evaluate_url(url, probes)
        else:
            ok, detail = probe_http(url)
        rows.append({"url": url, "ok": ok, "detail": detail})
        print(f"{'OK   ' if ok else 'ERROR'} {url} — {detail}", flush=True)
    return rows


def check_cards(cards: list[dict]) -> list[str]:
    """Flag every dead emitted link. Return errors that must fail the build."""
    planned: list[tuple[dict, str, str]] = []  # card, role, url
    for card in cards:
        for role, url in urls_for_card(card):
            planned.append((card, role, url))
    urls = list(dict.fromkeys(u for _c, _r, u in planned))
    print(f"Link check: {len(urls)} unique URL(s) from {len(cards)} card(s)", flush=True)
    rows = check_urls(urls)
    by_url = {r["url"]: r for r in rows}
    flags: list[str] = []
    errors: list[str] = []
    for card, role, url in planned:
        rec = by_url[url]
        if rec["ok"]:
            continue
        cid = card.get("id") or "?"
        status = card.get("status") or ""
        msg = (
            f"DEAD LINK {status} {cid} {role}: {url} — {rec['detail']}"
        )
        flags.append(msg)
        print(f"FLAG: {msg}", flush=True)
        # Never emit a dead href. Fail the build for any dead emitted link,
        # and especially for open cards (self-check).
        errors.append(f"ERROR: {msg}")
    if flags:
        print(f"Link check flagged {len(flags)} dead link(s).", flush=True)
    else:
        print("Link check: all emitted links OK", flush=True)
    return errors


def listing_button_html(card: dict, e) -> str:
    """Open-listing control: a real href, or 'Listing removed by Alt' with no link."""
    if card.get("listing_removed"):
        return (
            "<span class='open-btn open-btn-removed'>Listing removed by Alt</span>"
        )
    link = (card.get("link") or "").strip()
    if not link:
        return ""
    platform = e(card.get("platform") or "")
    return (
        f"<a class='open-btn' href='{e(link)}' target='_blank' "
        "rel='noopener noreferrer'>"
        "Open listing"
        f"<span class='open-sub'>{platform + ' · ' if platform else ''}{e(link)}</span>"
        "</a>"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check card links or one-off URLs")
    p.add_argument("urls", nargs="*", help="URLs to check (test mode)")
    p.add_argument("--cards", action="store_true", help="Check every emitted cards.json link")
    args = p.parse_args(argv)
    if args.cards:
        sys.path.insert(0, str(HERE))
        data = json.loads((HERE / "cards.json").read_text())
        errs = check_cards(data["cards"])
        if errs:
            for line in errs:
                print(line, file=sys.stderr)
            return 1
        return 0
    if not args.urls:
        p.print_help()
        return 2
    rows = check_urls(args.urls)
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
