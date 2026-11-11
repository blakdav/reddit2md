"""Saved threads on disk: one JSON file per thread plus a small index, with age-based auto-delete."""

import json
import logging
import os
import re
import threading
import time

import merge

log = logging.getLogger("reddit2md")

THREADS_DIR = os.environ.get("THREADS_DIR", "/data/threads")
INDEX = os.path.join(THREADS_DIR, "index.json")
# Summaries and AI chats, one file per thread (see ai.py). Kept outside
# THREADS_DIR so the index rebuild doesn't read them as threads.
AI_DIR = os.environ.get("AI_DIR", os.path.join(os.path.dirname(THREADS_DIR), "ai"))
ID_RE = re.compile(r"^[a-z0-9]{1,16}$")
_lock = threading.Lock()

META_KEYS = ("id", "title", "subreddit", "author", "permalink", "created", "saved_at", "first_saved", "prev_fetch",
             "count", "expected", "sort", "mode", "new_count", "kept_deleted", "partial")


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
    """Save a fetch. If the thread is already saved, merge into it and return update stats."""
    now = time.time()
    post, comments, count = result["post"], result["comments"], result["count"]
    old = get(result["id"])
    stats = None
    if old:
        comments, post, stats, count = merge.merge(old, comments, post, now,
                                                   same_mode=old.get("mode") == result.get("mode"),
                                                   partial=bool(result.get("partial")))
        first_saved = old.get("first_saved") or old.get("saved_at") or now
        prev_fetch = old.get("saved_at")
    else:
        merge.stamp_new(comments, now)
        first_saved, prev_fetch = now, None
    data = {
        "id": result["id"], "title": post.get("title"), "subreddit": post.get("subreddit"),
        "author": post.get("author"), "permalink": post.get("permalink"), "created": post.get("created"),
        "saved_at": now, "first_saved": first_saved, "prev_fetch": prev_fetch,
        "count": count, "expected": post.get("num_comments"),
        "sort": result.get("sort"), "mode": result.get("mode"),
        "new_count": stats["new"] if stats else 0,
        "kept_deleted": _kept(comments),
        # Set while the saved copy may be missing comments; a later complete fetch clears it.
        "partial": result.get("partial") or None,
        "post": post, "comments": comments,
    }
    with _lock:
        _write(_path(data["id"]), data)
        idx = _load_index()
        idx["threads"][data["id"]] = _meta(data)
        if source_url:
            idx["aliases"][source_url.strip()] = data["id"]
        _write(INDEX, idx)
    return _meta(data), stats


def _kept(nodes):
    return sum((1 if n.get("status") else 0) + _kept(n["replies"]) for n in nodes)


def get(tid):
    try:
        with open(_path(tid), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return None
    return _repair(data)


def _junk(n):
    """Empty placeholders saved by commit 42c845e, which misread morechildren's "api"-style answer:
    id "t1_<id>", no author, no text. Their real text was never saved."""
    return str(n.get("id", "")).startswith("t1_") and not n.get("author") and not (n.get("body") or "").strip()


def _drop_junk(nodes):
    kept, dropped = [], 0
    for n in nodes:
        if _junk(n) and not n["replies"]:
            dropped += 1
            continue
        n["replies"], d = _drop_junk(n["replies"])
        dropped += d
        kept.append(n)
    return kept, dropped


def _repair(data):
    """Drop junk placeholders once, flag the thread Partial so an Update refetches those comments."""
    data["comments"], dropped = _drop_junk(data.get("comments", []))
    if not dropped:
        return data
    data["count"] = merge._count(data["comments"])
    data["kept_deleted"] = _kept(data["comments"])
    reason = f"{dropped} comments failed to load in an earlier fetch"
    data["partial"] = f"{data['partial']}; {reason}" if data.get("partial") else reason
    with _lock:
        _write(_path(data["id"]), data)
        idx = _load_index()
        idx["threads"][data["id"]] = _meta(data)
        _write(INDEX, idx)
    log.info("Thread %s: removed %d empty placeholder comments; flagged Partial", data["id"], dropped)
    return data


def repair_all():
    """Run the junk-placeholder repair over every saved thread (cheap; once per start)."""
    for m in list_threads():
        try:
            get(m["id"])
        except Exception as e:
            log.warning("Repair of %s failed: %s", m.get("id"), e)


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
        try:
            os.remove(os.path.join(AI_DIR, f"{tid}.json"))
        except FileNotFoundError:
            pass
        idx = _load_index()
        idx["threads"].pop(tid, None)
        idx["aliases"] = {u: i for u, i in idx["aliases"].items() if i != tid}
        _write(INDEX, idx)


def purge(days):
    """Delete threads last fetched more than `days` ago. 0 keeps everything."""
    if not days or days <= 0:
        return 0
    cutoff = time.time() - days * 86400
    old = [m["id"] for m in list_threads() if (m.get("saved_at") or 0) < cutoff]
    for tid in old:
        delete(tid)
    if old:
        log.info("Auto-deleted %d saved threads older than %g days", len(old), days)
    return len(old)


def start_purger(get_days, interval=3600):
    def loop():
        while True:
            try:
                purge(get_days())
            except Exception:
                log.exception("Purge failed")
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="purger").start()
