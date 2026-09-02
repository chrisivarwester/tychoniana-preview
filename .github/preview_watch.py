#!/usr/bin/env python3
"""preview_watch.py — is the Tychoniana preview site up and intact?

Runs from GitHub Actions in the public preview repo (free minutes) every
30 minutes — see preview-watch.yml beside this file; both are installed into
the preview repo by docs/tychoniana/_publish_preview.ps1, so this copy in
organon is the source of truth.

Seven probes against the live site, each retried three times twenty seconds
apart before it counts as failed (GitHub Pages does blip for a second or two):

  root            200, the Tychoniana title, the noindex meta still present
  loca map        loca/index.html 200 and loca/data.json parses with >= 60 places
  iconographia    200 with the two record anchors the Jens mail links to
  census explorer 200 with the Esri basemap and NO CARTO reference (regression guard
                  for the 2026-09-02 "API KEY REQUIRED" watermark)
  epistolarium    data.js 200 and > 5 MB (the largest asset actually served)
  robots          Disallow still in place (the site stays unlisted)
  map tiles       one Esri tile 200 (outside our control — DEGRADED, not DOWN)

State (last status + last alert time) lives in a small JSON file the workflow
persists with actions/cache, so an outage alerts ONCE, then every six hours
while it lasts, then once more on recovery — never every half hour.

Alert channels: the process exits non-zero when an alert is due, so GitHub's
own "workflow failed" e-mail reaches the repo owner with no secrets at all;
with PUSHOVER_TOKEN / PUSHOVER_USER / RESEND_API_KEY set (the same three
organon uses for shiptrack), it also pushes to the phone and mails
chris@sophiarb.com from the house sender.

Local dry run (no state, no alerts, exit code = status):
    py -3.14 docs/tychoniana/preview-watch/preview_watch.py --local
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://chrisivarwester.github.io/tychoniana-preview/"
ESRI_TILE = ("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
             "World_Light_Gray_Base/MapServer/tile/4/5/8")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36 tychoniana-preview-watch")
RETRIES, RETRY_GAP_S, TIMEOUT_S = 3, 20, 30
REALERT_AFTER_H = 6
MAIL_FROM = "ORGANON Tychoniana <wants@sophiarb.com>"
MAIL_TO = "chris@sophiarb.com"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
RESEND_URL = "https://api.resend.com/emails"


def _get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


# ── the probes ─────────────────────────────────────────────────────────────
def probe_root():
    st, b = _get(BASE)
    t = _text(b)
    if st != 200: return False, f"HTTP {st}"
    if "<title>Tychoniana</title>" not in t: return False, "title missing"
    if 'name="robots"' not in t: return False, "noindex meta missing"
    return True, f"200, {len(b)//1024} KB"


def probe_loca():
    st, _ = _get(BASE + "loca/index.html?view=mappa")
    if st != 200: return False, f"loca/index.html HTTP {st}"
    st, b = _get(BASE + "loca/data.json")
    if st != 200: return False, f"loca/data.json HTTP {st}"
    try:
        d = json.loads(_text(b))
    except Exception as e:  # noqa: BLE001
        return False, f"loca/data.json not JSON ({e.__class__.__name__})"
    n = len(d.get("places", [])) if isinstance(d, dict) else 0
    if n < 60: return False, f"loca/data.json has {n} places"
    return True, f"200, {n} places"


def probe_iconographia():
    st, b = _get(BASE + "iconographia/index.html")
    t = _text(b)
    if st != 200: return False, f"HTTP {st}"
    for anchor in ("icon-mechanica-watercolours", "icon-arms-supralibros"):
        if f'id="{anchor}"' not in t: return False, f"anchor #{anchor} missing"
    return True, "200, both anchors present"


def probe_census():
    st, b = _get(BASE + "census/brahe-mechanica-1598-wandsbek/explorer.html")
    t = _text(b)
    if st != 200: return False, f"HTTP {st}"
    if "cartocdn" in t: return False, "CARTO basemap back in the page (API-key watermark)"
    if "World_Light_Gray_Base" not in t: return False, "Esri basemap reference missing"
    return True, "200, Esri basemap"


def probe_epistolarium():
    st, b = _get(BASE + "epistolarium/data.js")
    if st != 200: return False, f"HTTP {st}"
    if len(b) < 5_000_000: return False, f"data.js only {len(b)//1024} KB"
    return True, f"200, {len(b)//1024//1024} MB"


def probe_robots():
    st, b = _get(BASE + "robots.txt")
    if st != 200: return False, f"HTTP {st}"
    if "Disallow: /" not in _text(b): return False, "Disallow missing — site no longer unlisted"
    return True, "Disallow in place"


def probe_tiles():
    st, b = _get(ESRI_TILE)
    if st != 200 or len(b) < 1000: return False, f"Esri tile HTTP {st}, {len(b)} B"
    return True, f"200, {len(b)//1024} KB"


CORE = [("root", probe_root), ("loca map", probe_loca), ("iconographia", probe_iconographia),
        ("census explorer", probe_census), ("epistolarium", probe_epistolarium), ("robots", probe_robots)]
SOFT = [("map tiles (Esri)", probe_tiles)]


def run_probes() -> tuple[str, list[tuple[str, bool, str]]]:
    results = []
    for name, fn in CORE + SOFT:
        ok, detail = False, ""
        for attempt in range(1, RETRIES + 1):
            try:
                ok, detail = fn()
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"{e.__class__.__name__}: {e}"[:160]
            if ok: break
            if attempt < RETRIES:
                time.sleep(RETRY_GAP_S)
        results.append((name, ok, detail if ok else f"{detail} (after {RETRIES} attempts)"))
    core_ok = all(ok for name, ok, _ in results if name in dict(CORE))
    soft_ok = all(ok for name, ok, _ in results if name in dict(SOFT))
    status = "OK" if core_ok and soft_ok else ("DEGRADED" if core_ok else "DOWN")
    return status, results


# ── alerting ───────────────────────────────────────────────────────────────
def _post_form(url: str, data: dict) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, _text(r.read())[:200]
    except urllib.error.HTTPError as e:
        return e.code, _text(e.read())[:200]


def _post_json(url: str, data: dict, bearer: str) -> tuple[int, str]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Content-Type": "application/json", "Authorization": f"Bearer {bearer}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, _text(r.read())[:200]
    except urllib.error.HTTPError as e:
        return e.code, _text(e.read())[:200]


def alert(status: str, prev: str, results: list, since: str) -> bool:
    """Push + mail. Returns True if at least one channel accepted the alert,
    False if none was configured (the workflow exit code is then the only channel)."""
    lines = [f"{'OK ' if ok else 'FAIL'}  {name}: {detail}" for name, ok, detail in results]
    if status == "OK":
        title = "Tychoniana preview: back up"
        text = f"The preview site is serving again (was {prev} since {since}).\n\n" + "\n".join(lines)
        prio = -1
    elif status == "DEGRADED":
        title = "Tychoniana preview: map tiles unavailable"
        text = ("The site itself is up, but the Esri basemap tiles are not being served — "
                "maps will show blank backgrounds until Esri recovers.\n\n" + "\n".join(lines))
        prio = 0
    else:
        title = "Tychoniana preview: DOWN"
        text = (f"The preview site at {BASE} failed its core checks (three attempts, "
                f"20 s apart).\n\nJens Vellev has this link; check GitHub Pages "
                f"(repo chrisivarwester/tychoniana-preview → Settings → Pages) and re-run "
                f"docs/tychoniana/_publish_preview.ps1 if the site needs redeploying.\n\n" + "\n".join(lines))
        prio = 1
    text += f"\n\nChecked {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}."
    delivered = False
    tok, usr = os.environ.get("PUSHOVER_TOKEN", ""), os.environ.get("PUSHOVER_USER", "")
    if tok and usr:
        code, resp = _post_form(PUSHOVER_URL, {"token": tok, "user": usr, "title": title,
                                               "message": text[:1024], "priority": str(prio),
                                               "url": BASE, "url_title": "Open the preview"})
        print(f"pushover: HTTP {code} {resp}")
        delivered |= code == 200
    else:
        print("pushover: no credentials — skipped")
    key = os.environ.get("RESEND_API_KEY", "")
    if key:
        code, resp = _post_json(RESEND_URL, {"from": MAIL_FROM, "to": [MAIL_TO], "subject": title,
                                             "text": text}, key)
        print(f"resend: HTTP {code} {resp}")
        delivered |= code in (200, 201)
    else:
        print("resend: no credentials — skipped")
    return delivered


# ── main ───────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", help="JSON state file persisted between runs (actions/cache)")
    ap.add_argument("--local", action="store_true", help="dry run: no state, no alerts; exit code = status")
    ap.add_argument("--test-alert", action="store_true",
                    help="send a low-priority test push + mail through the configured channels and exit "
                         "(proves the secrets and the transport; no probes, no state)")
    a = ap.parse_args()

    if a.test_alert:
        now = dt.datetime.now(dt.timezone.utc)
        fake = [("channel test", True, f"sent {now:%Y-%m-%d %H:%M UTC} from the preview watch — no site problem")]
        ok = alert("OK", "TEST", fake, now.isoformat())
        print("test alert delivered" if ok else "test alert: NO channel configured (set the three secrets)")
        return 0 if ok else 1

    status, results = run_probes()
    now = dt.datetime.now(dt.timezone.utc)
    print(f"== Tychoniana preview watch — {status} — {now:%Y-%m-%d %H:%M UTC}")
    for name, ok, detail in results:
        print(f"  {'OK ' if ok else 'FAIL'}  {name:<18} {detail}")

    if a.local:
        return 0 if status == "OK" else (2 if status == "DEGRADED" else 1)

    state = {"status": "OK", "since": now.isoformat(), "last_alert": ""}
    if a.state and os.path.exists(a.state):
        try:
            state.update(json.load(open(a.state, encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            print(f"state unreadable ({e}); starting fresh")
    prev = state.get("status", "OK")
    changed = status != prev
    stale = False
    if not changed and status != "OK" and state.get("last_alert"):
        last = dt.datetime.fromisoformat(state["last_alert"])
        stale = (now - last) >= dt.timedelta(hours=REALERT_AFTER_H)
    due = changed or stale
    if changed:
        state["since"] = now.isoformat()
    print(f"previous {prev} → now {status}; alert {'DUE' if due else 'not due'}"
          + (" (6-hour reminder)" if stale else ""))

    rc = 0
    if due:
        delivered = alert(status, prev, results, state.get("since", ""))
        state["last_alert"] = now.isoformat()
        if status != "OK":
            rc = 1   # the failed run is itself an alert (GitHub's workflow-failure mail)
        elif not delivered:
            print("recovered, but no push/mail channel is configured — only this log records it")
    state["status"] = status
    if a.state:
        os.makedirs(os.path.dirname(a.state) or ".", exist_ok=True)
        json.dump(state, open(a.state, "w", encoding="utf-8"), indent=1)
    return rc


if __name__ == "__main__":
    sys.exit(main())
