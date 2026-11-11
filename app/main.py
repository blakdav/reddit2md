import json
import logging
import os
import threading
import time

from flask import Flask, jsonify, make_response, request, send_from_directory

import ai
import jobs
import llm
import settings
import storage
from search import INDEX
from cookies import CookieError, from_values, to_storage_state
from render import count_new, render, to_html
from scraper import POST_ID_RE, SHORT_RE, STATE_FILE, scrape, session_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reddit2md")

app = Flask(__name__, static_folder="static")

SORTS = ("confidence", "top", "new", "old", "controversial", "qa")
browser_lock = threading.Lock()  # one Chromium at a time (the queue worker, or a session check)

storage.start_purger(lambda: settings.load()["retention_days"])
storage.repair_all()

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


def _cached_id(url):
    """Find a saved thread for this URL without touching Reddit."""
    m = POST_ID_RE.search(url) or SHORT_RE.search(url)
    tid = m.group(1).lower() if m else storage.lookup_alias(url)
    return tid if tid and storage.get(tid) else None


def _auto_summary(tid, stats):
    """Queue a summary after a new thread, or after an Update that brought new comments."""
    kind = settings.load()["auto_summary"]
    if kind != "local":  # paid models never run on their own, whatever an old settings file says
        return
    if not llm.available()[kind]["enabled"] or (stats and not stats.get("new")):
        return
    try:
        ai.RUNNER.summarize(tid, kind)
        log.info("Auto-summary (%s) started for %s", kind, tid)
    except (ValueError, LookupError) as e:
        log.info("Auto-summary for %s skipped: %s", tid, e)


def _run(job, progress, cancelled, stats):
    """Queue runner: fetch one thread, save it, and return what the queue should show."""
    url = job["url"]
    if not browser_lock.acquire(blocking=False):
        progress("Waiting for the browser (session check)")
        browser_lock.acquire()
    try:
        progress("Starting browser")
        result = scrape(url, sort=job["sort"], mode=job["mode"], progress=progress, cancelled=cancelled, stats=stats)
    finally:
        browser_lock.release()
    has_id = POST_ID_RE.search(url) or SHORT_RE.search(url)
    _, update = storage.save(result, source_url=None if has_id else url)
    session = {"logged_in": bool(result.get("user")), "user": result.get("user")}
    if result["mode"] == "json":
        session["json_ok"] = True
    _session_cache(session)
    _auto_summary(result["id"], update)
    fields = {"id_saved": result["id"], "title": result["post"].get("title"), "count": result["count"],
              "update": update, "mode_used": result["mode"]}
    if result.get("partial"):
        fields["partial"] = True
        fields["warning"] = f"Partial: {result['partial']}. Run Update later to fetch the rest."
    else:
        expected = result["post"].get("num_comments")
        short = storage.shortfall(expected, result["count"])
        if short:
            log.warning("Thread %s: found %d comments but Reddit counts %d (%d short)",
                        result["id"], result["count"], expected, short)
            fields["warning"] = (f"Found {result['count']} comments but Reddit counts {expected}: about {short} "
                                 "missing. Some may be removed comments nobody can see; check the log.")
    return fields


QUEUE = jobs.Queue(os.path.join(os.path.dirname(STATE_FILE), "queue.json"), _run)


def _job_out(view):
    """Older clients (the browser extension) read the saved thread's id as "id" on a finished job."""
    out = dict(view)
    out["job"] = out["id"]
    if out.get("id_saved"):
        out["id"] = out["id_saved"]
    return out


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/convert")
def convert():
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
    view, added = QUEUE.add(url, sort, mode, same=_same_thread)
    # A ready-to-show line for clients that just display it (the iPhone Shortcut).
    pos = view.get("position")
    if not added:
        message = "Already in the reddit2md queue"
    else:
        what = "Update queued" if _cached_id(url) else "Queued"
        message = f"{what} in reddit2md" + (f" (#{pos} in line)" if pos and pos > 1 else "")
    return jsonify(job=view["id"], already_queued=not added, position=pos, message=message)


@app.get("/api/job/<jid>")
def job(jid):
    j = QUEUE.find(jid)
    if not j:
        return jsonify(error="Unknown job"), 404
    return jsonify(_job_out(QUEUE.view(j)))


@app.get("/api/queue")
def queue_list():
    return jsonify(jobs=[_job_out(v) for v in QUEUE.list()], pause=jobs.QUEUE_PAUSE,
                   rate_limit_pause=jobs.RATE_LIMIT_PAUSE)


@app.delete("/api/queue/<jid>")
def queue_remove(jid):
    if not QUEUE.remove(jid):
        return jsonify(error="Unknown job"), 404
    return jsonify(ok=True)


@app.post("/api/queue/<jid>/retry")
def queue_retry(jid):
    view = QUEUE.retry(jid)
    if not view:
        return jsonify(error="Not retryable"), 400
    return jsonify(job=view["id"])


@app.post("/api/queue/clear")
def queue_clear():
    QUEUE.clear_finished()
    return jsonify(ok=True)


# ---------------------------------------------------------------- saved threads

def _same_thread(a, b):
    ma, mb = POST_ID_RE.search(a or ""), POST_ID_RE.search(b or "")
    return a == b or bool(ma and mb and ma.group(1).lower() == mb.group(1).lower())


@app.get("/api/lookup")
def lookup():
    """State of one Reddit URL, for the browser extension's icon: saved, converting, failed, or none."""
    url = (request.args.get("url") or "").strip()
    mine = [v for v in QUEUE.list() if _same_thread(v.get("url"), url)]
    last = mine[-1] if mine else None
    if last and last["status"] in jobs.ACTIVE:
        return jsonify(state="running", progress=last["progress"])
    tid = _cached_id(url) if url else None
    if tid:
        data = storage.get(tid)
        return jsonify(state="saved", id=tid, title=data["post"].get("title"), count=data.get("count"),
                       saved_at=data.get("saved_at"))
    if last and last["status"] == "error":
        return jsonify(state="error", error=last["error"])
    return jsonify(state="none")


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
    new_only = request.args.get("new") == "1"
    order = request.args.get("order", "saved")
    tz = settings.load()["timezone"]
    md = render(data, scores=scores, tz=tz, new_only=new_only, order=order)
    web = render(data, scores=scores, tz=tz, web=True, new_only=new_only, order=order)
    meta = {k: data.get(k) for k in storage.META_KEYS}
    meta["new_count"] = count_new(data)
    return jsonify(meta=meta, markdown=md, html=to_html(web))


@app.delete("/api/threads/<tid>")
def thread_delete(tid):
    try:
        storage.delete(tid)
    except ValueError:
        return jsonify(error="Bad id"), 400
    return jsonify(ok=True)


# ---------------------------------------------------------------- AI: summaries, questions, search

def _ai_error(e):
    if isinstance(e, LookupError):
        return jsonify(error=str(e)), 404
    return jsonify(error=str(e)), 400


@app.get("/api/ai")
def ai_status():
    return jsonify(llm.available())


@app.get("/api/threads/<tid>/ai")
def thread_ai(tid):
    try:
        storage._path(tid)
    except ValueError:
        return jsonify(error="Bad id"), 400
    return jsonify(summaries=ai.RUNNER.summary_state(tid), chat=ai.RUNNER.chat_state(tid))


@app.post("/api/threads/<tid>/summary")
def summary_start(tid):
    body = request.get_json(silent=True) or {}
    try:
        ai.RUNNER.summarize(tid, body.get("kind"))
    except (ValueError, LookupError) as e:
        return _ai_error(e)
    return jsonify(ok=True)


@app.delete("/api/threads/<tid>/summary/<kind>")
def summary_delete(tid, kind):
    try:
        storage._path(tid)
        ai.RUNNER.delete_summary(tid, kind)
    except ValueError as e:
        return _ai_error(e)
    return jsonify(ok=True)


def _ask(key):
    body = request.get_json(silent=True) or {}
    try:
        ai.RUNNER.ask(key, body.get("kind"), body.get("question"), whole=bool(body.get("whole")))
    except (ValueError, LookupError) as e:
        return _ai_error(e)
    return jsonify(ok=True)


def _clear(key):
    try:
        ai.RUNNER.clear_chat(key)
    except ValueError as e:
        return _ai_error(e)
    return jsonify(ok=True)


@app.post("/api/threads/<tid>/chat")
def thread_chat(tid):
    try:
        storage._path(tid)
    except ValueError:
        return jsonify(error="Bad id"), 400
    return _ask(tid)


@app.delete("/api/threads/<tid>/chat")
def thread_chat_clear(tid):
    try:
        storage._path(tid)
    except ValueError:
        return jsonify(error="Bad id"), 400
    return _clear(tid)


@app.post("/api/ai/stop")
def ai_stop():
    """Stop a running summary or answer: {"key": thread id or "_all", "what": "chat" or "summary:<kind>"}."""
    body = request.get_json(silent=True) or {}
    try:
        ai.RUNNER.stop(str(body.get("key") or ""), str(body.get("what") or ""))
    except LookupError as e:
        return _ai_error(e)
    return jsonify(ok=True)


@app.get("/api/ask")
def ask_all_state():
    return jsonify(ai.RUNNER.chat_state(ai.ALL))


@app.post("/api/ask")
def ask_all():
    return _ask(ai.ALL)


@app.delete("/api/ask")
def ask_all_clear():
    return _clear(ai.ALL)


@app.get("/api/search")
def search():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(threads=[], total=0, limited=False)
    return jsonify(INDEX.search(q[:500]))


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
