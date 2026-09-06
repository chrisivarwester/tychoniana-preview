#!/usr/bin/env python3
"""preview_watch.py — is Tychoniana up and intact?

Watches EITHER deployment (since 2026-09-06): the unlisted preview, and the
public site at tychoniana.com. `--base` (or TYCHO_WATCH_BASE) chooses; the
default is the preview, so every existing invocation is unchanged. Runs from
GitHub Actions in a public repo (free minutes) every 30 minutes — see
preview-watch.yml beside this file; the publish scripts install both files into
their repo on every deploy, so this copy in organon is the source of truth.

Eight probes (nine on the public site), each retried three times twenty seconds
apart before it counts as failed (GitHub Pages does blip for a second or two):

  root            200 and the Tychoniana title
  not found       an unmatched address returns OUR 404 page, not the host's
  loca map        loca/index.html 200 and loca/data.json parses with >= 60 places
  iconographia    the two records the Jens mail links to, each on its own page
  census explorer 200 with the Esri basemap and NO CARTO reference (regression guard
                  for the 2026-09-02 "API KEY REQUIRED" watermark)
  epistolarium    data.json 200 and > 5 MB (the largest asset actually served)
  indexing        robots.txt and the pages AGREE about being crawlable — in
                  whichever direction the deploy chose (see probe_indexing)
  www             www.tychoniana.com reaches the site  [public site only]
  map tiles       one Esri tile 200 (outside our control — DEGRADED, not DOWN)

Two of these were measuring the wrong thing after the layered roll-out of
2026-09-06 and were repaired the same day: the iconographia records had moved
from anchors on one long page to pages of their own, and the epistolarium
payload had moved from data.js (left a 97 KB stub) to data.json.

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

# The site under watch. One script serves both deployments (2026-09-06): the
# unlisted preview, and the public site at tychoniana.com. `--base` / the
# TYCHO_WATCH_BASE environment variable overrides; the default keeps every
# existing preview invocation working unchanged.
PREVIEW_BASE = "https://chrisivarwester.github.io/tychoniana-preview/"
PUBLIC_BASE = "https://tychoniana.com/"
BASE = PREVIEW_BASE
SITE = "preview"          # the word the alerts use; set by _set_base()


def _set_base(base: str) -> None:
    """Point the probes at one deployment.

    SITE names the deployment for the alerts; it is the PREVIEW only for the
    preview repo's URL, so a pre-DNS run of the public build against
    chrisivarwester.github.io/tychoniana/ is still labelled the public site —
    which is what it is. Whether the www probe runs is a separate question,
    answered by the hostname actually being tychoniana.com.
    """
    global BASE, SITE
    BASE = base if base.endswith("/") else base + "/"
    SITE = "preview" if "tychoniana-preview" in BASE else "tychoniana.com"


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
        # the body of an error response is content too — a 404 page is the whole
        # point of probe_not_found, and discarding it made that probe unanswerable
        try:
            return e.code, e.read()
        except Exception:  # noqa: BLE001
            return e.code, b""


def _text(b: bytes) -> str:
    return b.decode("utf-8", errors="replace")


# ── the probes ─────────────────────────────────────────────────────────────
def probe_root():
    st, b = _get(BASE)
    t = _text(b)
    if st != 200: return False, f"HTTP {st}"
    if "<title>Tychoniana</title>" not in t: return False, "title missing"
    return True, f"200, {len(b)//1024} KB"


def probe_not_found():
    """An unmatched address must return OUR 404 page, not the host's.

    GitHub Pages serves /404.html for anything it cannot match, so this is the
    cheapest proof that the deploy is the site and not a stray default page.
    """
    st, b = _get(BASE + "this-address-does-not-exist-watch-probe")
    t = _text(b)
    if st != 404: return False, f"expected HTTP 404, got {st}"
    if "This address is not part of Tychoniana" not in t:
        return False, "the host's own 404 is being served, not ours"
    return True, "404, our own page"


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
    """The two records Jens Vellev's link names must still render.

    Until 2026-09-06 both were anchors on the pillar's one long page. The
    layered form gave every record of a paged section its own page, so the ids
    now live at iconographia/r/<id>.html — the probe followed them there rather
    than being weakened to a page-exists check.
    """
    for rid in ("icon-mechanica-watercolours", "icon-arms-supralibros"):
        st, b = _get(f"{BASE}iconographia/r/{rid}.html")
        if st != 200: return False, f"iconographia/r/{rid}.html HTTP {st}"
        if f'id="{rid}"' not in _text(b): return False, f"record {rid} missing from its own page"
    return True, "200, both record pages present"


def probe_census():
    st, b = _get(BASE + "census/brahe-mechanica-1598-wandsbek/explorer.html")
    t = _text(b)
    if st != 200: return False, f"HTTP {st}"
    if "cartocdn" in t: return False, "CARTO basemap back in the page (API-key watermark)"
    if "World_Light_Gray_Base" not in t: return False, "Esri basemap reference missing"
    return True, "200, Esri basemap"


def probe_epistolarium():
    """The largest asset actually served — 11 MB of letters.

    It was data.js until the layered rebuild moved the payload to data.json and
    left data.js a 97 KB stub; the old probe was measuring the stub.
    """
    st, b = _get(BASE + "epistolarium/data.json")
    if st != 200: return False, f"HTTP {st}"
    if len(b) < 5_000_000: return False, f"data.json only {len(b)//1024} KB"
    return True, f"200, {len(b)//1024//1024} MB"


def probe_indexing():
    """robots.txt and the pages must agree about whether the site is indexable.

    Not "Disallow must be present": the public site is published noindex only
    while LAUNCH.md D1 is open, and _publish_public.ps1 -Indexed will one day
    flip both halves at once. What must never happen is HALF a flip — robots
    inviting crawlers to pages that carry a noindex tag, or the reverse — and
    that is what this checks, in whichever direction the deploy chose.
    """
    st, b = _get(BASE + "robots.txt")
    if st != 200: return False, f"robots.txt HTTP {st}"
    disallowed = "Disallow: /" in _text(b)
    st, b = _get(BASE)
    if st != 200: return False, f"root HTTP {st}"
    noindexed = "noindex" in _text(b)
    if disallowed and not noindexed:
        return False, "robots.txt disallows crawling but the pages carry no noindex tag"
    if noindexed and not disallowed:
        return False, "pages carry noindex but robots.txt invites crawling"
    return True, "unlisted (robots + noindex agree)" if disallowed else "public and indexable"


def probe_tiles():
    st, b = _get(ESRI_TILE)
    if st != 200 or len(b) < 1000: return False, f"Esri tile HTTP {st}, {len(b)} B"
    return True, f"200, {len(b)//1024} KB"


def probe_www():
    """www.tychoniana.com must reach the site (GitHub redirects it to the apex).

    Public site only — the preview has no second hostname.
    """
    st, b = _get("https://www.tychoniana.com/")
    if st != 200: return False, f"HTTP {st}"
    if "<title>Tychoniana</title>" not in _text(b): return False, "not the site"
    return True, "200 via www"


CORE = [("root", probe_root), ("not found", probe_not_found), ("loca map", probe_loca),
        ("iconographia", probe_iconographia), ("census explorer", probe_census),
        ("epistolarium", probe_epistolarium), ("indexing", probe_indexing)]
SOFT = [("map tiles (Esri)", probe_tiles)]


def _probes() -> tuple[list, list]:
    """The probe set for the site under watch.

    The www probe runs only when the base really is the custom domain — a
    pre-DNS run against the github.io address has no second hostname to check.
    """
    core = list(CORE)
    if "tychoniana.com" in BASE:
        core.append(("www", probe_www))
    return core, list(SOFT)


def run_probes() -> tuple[str, list[tuple[str, bool, str]]]:
    CORE, SOFT = _probes()
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
    public = SITE == "tychoniana.com"
    what = "Tychoniana" if public else "Tychoniana preview"
    repo = "chrisivarwester/tychoniana" if public else "chrisivarwester/tychoniana-preview"
    script = "_publish_public.ps1" if public else "_publish_preview.ps1"
    if status == "OK":
        title = f"{what}: back up"
        text = f"The site is serving again (was {prev} since {since}).\n\n" + "\n".join(lines)
        prio = -1
    elif status == "DEGRADED":
        title = f"{what}: map tiles unavailable"
        text = ("The site itself is up, but the Esri basemap tiles are not being served — "
                "maps will show blank backgrounds until Esri recovers.\n\n" + "\n".join(lines))
        prio = 0
    else:
        title = f"{what}: DOWN"
        who = ("This is the public address people have been given."
               if public else "Jens Vellev has this link.")
        text = (f"{BASE} failed its core checks (three attempts, 20 s apart).\n\n{who} "
                f"Check GitHub Pages (repo {repo} → Settings → Pages) and re-run "
                f"docs/tychoniana/{script} if the site needs redeploying.\n\n" + "\n".join(lines))
        prio = 1
    text += f"\n\nChecked {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}."
    delivered = False
    tok, usr = os.environ.get("PUSHOVER_TOKEN", ""), os.environ.get("PUSHOVER_USER", "")
    if tok and usr:
        code, resp = _post_form(PUSHOVER_URL, {"token": tok, "user": usr, "title": title,
                                               "message": text[:1024], "priority": str(prio),
                                               "url": BASE, "url_title": f"Open {SITE}"})
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
    ap.add_argument("--base", default=os.environ.get("TYCHO_WATCH_BASE", PREVIEW_BASE),
                    help=f"the site to watch (default {PREVIEW_BASE}; the public site is {PUBLIC_BASE})")
    a = ap.parse_args()
    _set_base(a.base)

    if a.test_alert:
        now = dt.datetime.now(dt.timezone.utc)
        fake = [("channel test", True, f"sent {now:%Y-%m-%d %H:%M UTC} from the {SITE} watch — no site problem")]
        ok = alert("OK", "TEST", fake, now.isoformat())
        print("test alert delivered" if ok else "test alert: NO channel configured (set the three secrets)")
        return 0 if ok else 1

    status, results = run_probes()
    now = dt.datetime.now(dt.timezone.utc)
    print(f"== Tychoniana watch [{SITE}] — {status} — {now:%Y-%m-%d %H:%M UTC}")
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
