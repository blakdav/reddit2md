import json
import logging
import os
import threading
import time
import uuid

from flask import Flask, jsonify, make_response, request, send_from_directory

import settings
import storage
from cookies import CookieError, from_values, to_storage_state
from render import render, to_html
from scraper import POST_ID_RE, SHORT_RE, STATE_FILE, ScrapeError, scrape, session_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reddit2md")

app = Flask(__name__, static_folder="static")

JOBS = {}
JOB_TTL = 3600
SORTS = ("confidence", "top", "new", "old", "controversial", "qa")
browser_lock = threading.Lock()  # one Chromium at a time

storage.start_purger(lambda: settings.load()["retention_days"])

# Last known Reddit login state, so the header pill never has to launch a browser on page load.
SESSION_FILE = os.path.join(os.path.dirname(STATE_FILE), "session_status.json")
SESSION_MAX_AGE = 6 * 3600


def _session_cache(update=None):
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            cache = json.load(f)
    except (FileNotFoundError, ValueError):
        cache = {}
    if update is not None:
        cache = {**cache, "checked_at": time.time(), **update}
        try:
            with open(SESSION_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(SESSION_FILE + ".tmp", SESSION_FILE)
        except OSError as e:
            log.warning("Could not write session status: %s", e)
    cache["state_file"] = os.path.exists(STATE_FILE)
    return cache


def _cleanup_jobs():
    cutoff = time.time() - JOB_TTL
    for jid in [j for j, v in JOBS.items() if v["started"] < cutoff]:
        JOBS.pop(jid, None)


def _cached_id(url):
    """Find a saved thread for this URL without touching Reddit."""
    m = POST_ID_RE.search(url) or SHORT_RE.search(url)
    tid = m.group(1).lower() if m else storage.lookup_alias(url)
    return tid if tid and storage.get(tid) else None


def _run(jid, url, sort, mode):
    job = JOBS[jid]

    def progress(msg):
        job["progress"] = msg
        log.info("[%s] %s", jid[:8], msg)

    try:
        progress("Waiting for browser" if browser_lock.locked() else "Starting browser")
        with browser_lock:
            result = scrape(url, sort=sort, mode=mode, progress=progress)
        has_id = POST_ID_RE.search(url) or SHORT_RE.search(url)
        storage.save(result, source_url=None if has_id else url)
        update = {"logged_in": bool(result.get("user")), "user": result.get("user")}
        if result["mode"] == "json":
            update["json_ok"] = True
        _session_cache(update)
        job["id"] = result["id"]
        job["status"] = "done"
        progress("Done")
    except ScrapeError as e:
        job["status"], job["error"] = "error", str(e)
    except Exception as e:
        log.exception("Job %s failed", jid)
        job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/convert")
def convert():
    _cleanup_jobs()
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify(error="URL required"), 400
    if not body.get("refresh"):
        tid = _cached_id(url)
        if tid:
            return jsonify(cached=True, id=tid)
    sort = body.get("sort") if body.get("sort") in SORTS else "confidence"
    mode = body.get("mode") if body.get("mode") in ("auto", "json", "dom") else "auto"
    jid = uuid.uuid4().hex
    JOBS[jid] = {"status": "running", "progress": "Queued", "started": time.time()}
    threading.Thread(target=_run, args=(jid, url, sort, mode), daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/job/<jid>")
def job(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(error="Unknown job"), 404
    out = dict(j)
    out["elapsed"] = round(time.time() - j["started"])
    return jsonify(out)


# ---------------------------------------------------------------- saved threads

@app.get("/api/threads")
def threads_list():
    cfg = settings.load()
    return jsonify(threads=storage.list_threads(), retention_days=cfg["retention_days"], timezone=cfg["timezone"])


@app.get("/api/threads/<tid>")
def thread_get(tid):
    try:
        data = storage.get(tid)
    except ValueError:
        return jsonify(error="Bad id"), 400
    if not data:
        return jsonify(error="Not found (it may have been auto-deleted)"), 404
    scores = request.args.get("scores", "1") != "0"
    md = render({"post": data["post"], "comments": data["comments"], "count": data["count"]},
                scores=scores, tz=settings.load()["timezone"])
    meta = {k: data.get(k) for k in storage.META_KEYS}
    return jsonify(meta=meta, markdown=md, html=to_html(md))


@app.delete("/api/threads/<tid>")
def thread_delete(tid):
    try:
        storage.delete(tid)
    except ValueError:
        return jsonify(error="Bad id"), 400
    return jsonify(ok=True)


# ---------------------------------------------------------------- settings

@app.get("/api/settings")
def settings_get():
    return jsonify(settings=settings.load(), timezones=settings.timezones())


@app.put("/api/settings")
def settings_put():
    try:
        data = settings.save(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    deleted = storage.purge(data["retention_days"])
    return jsonify(settings=data, deleted=deleted)


# ---------------------------------------------------------------- Reddit session

@app.get("/api/session")
def session():
    """?cached=1 returns the last known state (checking only if none or stale); otherwise checks Reddit now."""
    if not os.path.exists(STATE_FILE):
        return jsonify(_session_cache({"logged_in": False, "user": None, "json_ok": None}))
    cache = _session_cache()
    fresh = cache.get("checked_at") and time.time() - cache["checked_at"] < SESSION_MAX_AGE
    if request.args.get("cached") and fresh and "logged_in" in cache:
        return jsonify(cache)
    try:
        with browser_lock:
            info = session_status()
        return jsonify(_session_cache(info))
    except Exception as e:
        return jsonify(error=str(e)), 500


def _write_state(state):
    tmp = STATE_FILE + ".tmp"
    with browser_lock:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(state, out)
        os.replace(tmp, STATE_FILE)
    log.info("Session file replaced (%d reddit cookies)", len(state["cookies"]))
    _session_cache({"logged_in": None, "user": None, "json_ok": None, "checked_at": 0})


@app.post("/api/session/upload")
def session_upload():
    f = request.files.get("file")
    if not f:
        return jsonify(error="No file uploaded"), 400
    try:
        state, n = to_storage_state(f.read(2_000_000))
    except CookieError as e:
        return jsonify(error=str(e)), 400
    _write_state(state)
    return jsonify(ok=True, cookies=n)


@app.post("/api/session/cookie")
def session_cookie():
    try:
        state, n = from_values(request.get_json(silent=True) or {})
    except CookieError as e:
        return jsonify(error=str(e)), 400
    _write_state(state)
    return jsonify(ok=True, cookies=n)


@app.delete("/api/session")
def session_delete():
    with browser_lock:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    return jsonify(ok=True)


@app.get("/api/health")
def health():
    resp = make_response("ok")
    resp.headers["Content-Type"] = "text/plain"
    return resp
