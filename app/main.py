import json
import logging
import os
import threading
import time
import uuid

from flask import Flask, jsonify, make_response, request, send_from_directory

from cookies import CookieError, from_values, to_storage_state
from render import render, to_html
from scraper import STATE_FILE, ScrapeError, scrape, session_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reddit2md")

app = Flask(__name__, static_folder="static")

JOBS = {}
JOB_TTL = 3600
browser_lock = threading.Lock()  # one Chromium at a time


def _cleanup():
    cutoff = time.time() - JOB_TTL
    for jid in [j for j, v in JOBS.items() if v["started"] < cutoff]:
        JOBS.pop(jid, None)


def _run(jid, url, sort, mode, scores):
    job = JOBS[jid]

    def progress(msg):
        job["progress"] = msg
        log.info("[%s] %s", jid[:8], msg)

    try:
        progress("Waiting for browser" if browser_lock.locked() else "Starting browser")
        with browser_lock:
            result = scrape(url, sort=sort, mode=mode, progress=progress)
        job["markdown"] = render(result, scores=scores)
        job["html"] = to_html(job["markdown"])
        job["title"] = result["post"]["title"]
        job["count"] = result["count"]
        job["expected"] = result["post"].get("num_comments")
        job["mode"] = result["mode"]
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
    _cleanup()
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not url:
        return jsonify(error="URL required"), 400
    sort = body.get("sort", "confidence")
    if sort not in ("confidence", "top", "new", "old", "controversial", "qa"):
        sort = "confidence"
    mode = body.get("mode", "auto")
    if mode not in ("auto", "json", "dom"):
        mode = "auto"
    jid = uuid.uuid4().hex
    JOBS[jid] = {"status": "running", "progress": "Queued", "started": time.time()}
    threading.Thread(target=_run, args=(jid, url, sort, mode, bool(body.get("scores", True))), daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/job/<jid>")
def job(jid):
    j = JOBS.get(jid)
    if not j:
        return jsonify(error="Unknown job"), 404
    out = {k: v for k, v in j.items() if k not in ("markdown", "html")}
    out["elapsed"] = round(time.time() - j["started"])
    if j["status"] == "done":
        out["markdown"] = j["markdown"]
        out["html"] = j["html"]
    return jsonify(out)


@app.get("/api/session")
def session():
    try:
        with browser_lock:
            return jsonify(session_status())
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
