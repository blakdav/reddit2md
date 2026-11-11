"""Full-text search over saved threads, and finding the comments that best
match a question (for "Ask across all threads" and local chat on big threads).

The index lives in memory and is rebuilt per thread when its file changes, so
it never goes stale and needs no files of its own. Ranking is BM25, the
standard keyword-relevance formula: rare words count more than common ones,
and a word repeated in a short comment counts more than in a long one.
"""

import html
import math
import os
import re
import threading
from collections import Counter

import storage
from thread_text import walk

WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.U)
# Words too common to help find anything. Only used when ranking for a
# question; a typed search matches exactly what was typed.
STOP = set("""a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her
here hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of
off on once only or other our ours ourselves out over own same she should so some such than that the their theirs
them themselves then there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours yourself yourselves people think say said says thread
reddit comment comments anyone someone something thing things get got really also like one would much many even
still know way""".split())

K1, B = 1.2, 0.75


def words(text):
    return [w.lower().replace("’", "'") for w in WORD.findall(text or "")]


class Index:
    def __init__(self):
        self._lock = threading.Lock()
        self._threads = {}  # tid -> {"mtime", "title", "subreddit", "docs": [...]}

    # ------------------------------------------------------------------ build

    def _refresh(self):
        metas = {m["id"]: m for m in storage.list_threads()}
        for tid in list(self._threads):
            if tid not in metas:
                del self._threads[tid]
        for tid, meta in metas.items():
            try:
                mtime = os.path.getmtime(storage._path(tid))
            except OSError:
                continue
            cached = self._threads.get(tid)
            if cached and cached["mtime"] == mtime:
                continue
            data = storage.get(tid)
            if data:
                self._threads[tid] = self._build(tid, data, mtime)

    def _build(self, tid, data, mtime):
        post = data["post"]
        docs = [{"tid": tid, "cid": None, "author": post.get("author"), "score": post.get("score"),
                 "text": f"{post.get('title') or ''}\n{post.get('body') or ''}", "parent": None, "depth": -1}]
        for n, depth, parent in walk(data.get("comments") or []):
            docs.append({"tid": tid, "cid": n.get("id"), "author": n.get("author"), "score": n.get("score"),
                         "text": n.get("body") or "", "parent": parent, "depth": depth})
        for d in docs:
            w = words(d["text"])
            d["tf"] = Counter(w)
            d["len"] = len(w) or 1
        return {"mtime": mtime, "title": post.get("title"), "subreddit": post.get("subreddit"),
                "docs": docs, "by_id": {d["cid"]: d for d in docs if d["cid"]}}

    # ------------------------------------------------------------------ rank

    def _rank(self, terms, require_all, phrase=None, tid=None):
        with self._lock:
            self._refresh()
            threads = {tid: self._threads[tid]} if tid else dict(self._threads)
        docs = [d for t in threads.values() for d in t["docs"]]
        if not docs or not terms:
            return [], threads
        n = len(docs)
        avg = sum(d["len"] for d in docs) / n
        df = {t: sum(1 for d in docs if t in d["tf"]) for t in terms}
        scored = []
        for d in docs:
            have = [t for t in terms if t in d["tf"]]
            if not have or (require_all and len(have) < len(terms)):
                continue
            if phrase and phrase not in d["text"].lower():
                continue
            s = 0.0
            for t in have:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                tf = d["tf"][t]
                s += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * d["len"] / avg))
            scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        return scored, threads

    # ------------------------------------------------------------------ search box

    def search(self, query, limit=200):
        """Comments (and posts) containing every word typed. "Quoted text" must appear as is.
        Returns threads with their best matching comments, best thread first."""
        phrases = re.findall(r'"([^"]+)"', query)
        terms = list(dict.fromkeys(words(query)))
        phrase = phrases[0].lower() if phrases else None
        scored, threads = self._rank(terms, require_all=True, phrase=phrase)
        groups = {}
        for s, d in scored[:limit]:
            g = groups.setdefault(d["tid"], {"id": d["tid"], "title": threads[d["tid"]]["title"],
                                             "subreddit": threads[d["tid"]]["subreddit"], "score": 0, "hits": []})
            g["score"] += s
            g["hits"].append({"cid": d["cid"], "author": d["author"], "points": d["score"],
                              "snippet": snippet(d["text"], terms)})
        out = sorted(groups.values(), key=lambda g: -g["score"])
        return {"threads": out, "total": sum(len(g["hits"]) for g in out), "limited": len(scored) > limit}

    # ------------------------------------------------------------------ for the AI

    def relevant(self, question, budget_chars, tid=None):
        """The comments that best match a question, with the comment each one
        answers for context, grouped by thread, as prompt text that fits
        budget_chars. Returns (text, {comment id: {"tid", "author", "title"}}, comments used)."""
        terms = [t for t in dict.fromkeys(words(question)) if t not in STOP and len(t) > 1]
        scored, threads = self._rank(terms, require_all=False, tid=tid)
        picked, used, seen = {}, 0, set()
        for _, d in scored:
            if d["cid"] in seen:
                continue
            t = threads[d["tid"]]
            parent = t["by_id"].get(d["parent"]) if d["parent"] else None
            piece = _fmt(d, t, parent)
            if used + len(piece) > budget_chars:
                if used > budget_chars * 0.9:
                    break
                continue
            picked.setdefault(d["tid"], []).append(piece)
            seen.add(d["cid"])
            used += len(piece) + 2
        ids = {}
        parts = []
        for tid_, pieces in picked.items():
            t = threads[tid_]
            parts.append(f"=== Thread: {t['title']} (r/{t['subreddit']}) ===\n\n" + "\n\n".join(pieces))
            for d in t["docs"]:
                if d["cid"]:
                    ids[d["cid"].lower()] = {"tid": tid_, "author": d["author"], "title": t["title"]}
        return "\n\n".join(parts), ids, len(seen)


def _fmt(d, t, parent):
    def one(x, indent=""):
        head = f"[c:{x['cid']}] u/{x['author'] or '[deleted]'}" if x["cid"] else f"Original post by u/{x['author']}"
        if x["score"] is not None:
            head += f" · {x['score']} pts"
        return "\n".join(indent + line for line in (head + "\n" + (x["text"].strip() or "[empty]")).split("\n"))
    if parent:
        ptext = parent["text"].strip()
        if len(ptext) > 600:
            parent = {**parent, "text": ptext[:600] + " …"}
        return f"(in reply to)\n{one(parent, '  ')}\n{one(d)}"
    return one(d)


def snippet(text, terms, width=220):
    """A short piece of text around the first match, HTML-escaped, matches in <mark>."""
    text = " ".join((text or "").split())
    low = text.lower()
    pos = min((p for p in (low.find(t) for t in terms) if p >= 0), default=0)
    start = max(0, pos - width // 3)
    piece = text[start:start + width]
    piece = ("… " if start else "") + piece + (" …" if start + width < len(text) else "")
    out = html.escape(piece)
    for t in sorted(terms, key=len, reverse=True):
        out = re.sub(r"(?<![\w])(" + re.escape(html.escape(t)) + r")", r"<mark>\1</mark>", out, flags=re.I)
    return out


INDEX = Index()
