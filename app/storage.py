"""Saved threads on disk: one JSON file per thread plus a small index, with age-based auto-delete."""

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("reddit2md")

THREADS_DIR = os.environ.get("THREADS_DIR", "/data/threads")
RETENTION_DAYS = float(os.environ.get("RETENTION_DAYS", "30"))  # 0 = keep forever
INDEX = os.path.join(THREADS_DIR, "index.json")
ID_RE = re.compile(r"^[a-z0-9]{1,16}$")
_lock = threading.Lock()

META_KEYS = ("id", "title", "subreddit", "author", "permalink", "created", "saved_at", "count", "expected", "sort", "mode")


def _path(tid):
    if not ID_RE.match(tid or ""):
        raise ValueError("Bad thread id")
    return os.path.join(THREADS_DIR, f"{tid}.json")


def _write(path, obj):
    os.makedirs(THREADS_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _meta(data):
    return {k: data.get(k) for k in META_KEYS}


def _load_index():
    try:
        with open(INDEX, encoding="utf-8") as f:
            idx = json.load(f)
        idx.setdefault("threads", {})
        idx.setdefault("aliases", {})
        return idx
    except FileNotFoundError:
        return _rebuild_index()
    except ValueError:
        log.warning("Thread index unreadable, rebuilding")
        return _rebuild_index()


def _rebuild_index():
    idx = {"threads": {}, "aliases": {}}
    if os.path.isdir(THREADS_DIR):
        for name in os.listdir(THREADS_DIR):
            if not name.endswith(".json") or name == "index.json":
                continue
            try:
                with open(os.path.join(THREADS_DIR, name), encoding="utf-8") as f:
                    d = json.load(f)
                idx["threads"][d["id"]] = _meta(d)
            except Exception as e:
                log.warning("Skipping unreadable saved thread %s: %s", name, e)
    _write(INDEX, idx)
    return idx


def save(result, source_url=None):
    post = result["post"]
    data = {
        "id": result["id"], "title": post.get("title"), "subreddit": post.get("subreddit"),
        "author": post.get("author"), "permalink": post.get("permalink"), "created": post.get("created"),
        "saved_at": time.time(), "count": result["count"], "expected": post.get("num_comments"),
        "sort": result.get("sort"), "mode": result.get("mode"),
        "post": post, "comments": result["comments"],
    }
    with _lock:
        _write(_path(data["id"]), data)
        idx = _load_index()
        idx["threads"][data["id"]] = _meta(data)
        if source_url:
            idx["aliases"][source_url.strip()] = data["id"]
        _write(INDEX, idx)
    return _meta(data)


def get(tid):
    try:
        with open(_path(tid), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def lookup_alias(url):
    with _lock:
        return _load_index()["aliases"].get(url.strip())


def list_threads():
    with _lock:
        items = list(_load_index()["threads"].values())
    return sorted(items, key=lambda m: m.get("saved_at") or 0, reverse=True)


def delete(tid):
    path = _path(tid)
    with _lock:
        if os.path.exists(path):
            os.remove(path)
        idx = _load_index()
        idx["threads"].pop(tid, None)
        idx["aliases"] = {u: i for u, i in idx["aliases"].items() if i != tid}
        _write(INDEX, idx)


def purge():
    if RETENTION_DAYS <= 0:
        return 0
    cutoff = time.time() - RETENTION_DAYS * 86400
    old = [m["id"] for m in list_threads() if (m.get("saved_at") or 0) < cutoff]
    for tid in old:
        delete(tid)
    if old:
        log.info("Auto-deleted %d saved threads older than %g days", len(old), RETENTION_DAYS)
    return len(old)


def start_purger(interval=3600):
    def loop():
        while True:
            try:
                purge()
            except Exception:
                log.exception("Purge failed")
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="purger").start()
