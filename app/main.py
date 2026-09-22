import logging
import threading
import time
import uuid

from flask import Flask, jsonify, make_response, request, send_from_directory

from render import render
from scraper import ScrapeError, scrape, session_status

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
    out = {k: v for k, v in j.items() if k != "markdown"}
    out["elapsed"] = round(time.time() - j["started"])
    if j["status"] == "done":
        out["markdown"] = j["markdown"]
    return jsonify(out)


@app.get("/api/session")
def session():
    try:
        with browser_lock:
            return jsonify(session_status())
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.get("/api/health")
def health():
    resp = make_response("ok")
    resp.headers["Content-Type"] = "text/plain"
    return resp
