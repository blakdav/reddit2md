"""Summaries, per-thread questions, and questions across all saved threads.

Results are saved in /data/ai/<thread id>.json ({"summaries": {"cloud"|"openai"|"local":
{...}}, "chat": [...]}) and, for questions across threads, /data/ai/_all.json.
Deleting a thread deletes its file (see storage.delete).

Work runs in background threads and the page polls for progress and the
answer so far, so a slow model or a page reload doesn't cut anything off.
Each kind (local, cloud) runs one job at a time: local because the GPU is
shared with Vidscribe, cloud to keep rate limits and costs predictable.

Comment references: the model cites comments as [c:<id>]; cite_links() turns
them into links the page knows how to follow.
"""

import json
import logging
import os
import re
import threading
import time

import gpulock
import llm
import settings
import storage
import thread_text
from render import to_html
from search import INDEX

log = logging.getLogger("reddit2md.ai")

AI_DIR = storage.AI_DIR
ALL = "_all"
# Shared with Vidscribe when GPU_LOCK_FILE is set, so local AI here never
# runs at the same time as a transcription or local AI there (see gpulock.py).
GPU = gpulock.GpuLock("reddit2md")

# How much text goes to the model. Claude and OpenAI can read a whole thread
# (1M-token context), capped here so one request can't cost more than about
# $3 on Opus (more on OpenAI's flagship, which costs 2.5x as much per token).
# Questions across threads send the best-matching comments, which also keeps
# each question to roughly $0.25.
CLOUD_THREAD_CHARS = 2_500_000
CLOUD_ALL_CHARS = 200_000
CONVERSATION_CHARS = 12_000  # room kept for earlier questions in local chat

SUMMARY_SYSTEM = """You summarize Reddit threads that the user saved. The text has the post, then every
comment with its author, score (upvotes minus downvotes) and a reference like [c:k3x9a].
Replies are indented under the comment they answer. "(OP)" marks the person who posted the thread.

Write the summary in Markdown, in the thread's language, with these sections:

## TL;DR
Two or three sentences: what OP asked or said, and what the thread concluded.

## What people said
The main viewpoints, answers or themes as bullet points, most supported first. Use scores and
how many comments say the same thing to judge support, and say so ("most replies", "a few people").
Note real disagreements and which side got more support.

## From OP
What OP added in the comments: answers, updates, corrections, follow-up questions. Leave this out if OP didn't comment.

## Useful details
Specific recommendations, products, numbers, steps, sources or links people gave. Leave this out if there are none.

## Notable comments
Up to five comments worth reading in full, each with u/author, score, a one-line reason, and its reference.

Cite comments by their reference in square brackets, e.g. [c:k3x9a], right after the point they support.
Be faithful to the thread: don't add facts that aren't in it, and keep jokes and sarcasm from reading as claims.
Be concise."""

CHUNK_SYSTEM = """You are summarizing one part of the comments of a longer Reddit thread. Write dense Markdown
bullet notes of everything important in this part: viewpoints and answers (with how well supported they are,
by score), recommendations, facts, numbers, links, and anything OP said. Keep the [c:...] references of the
comments you mention. These notes will be merged with notes from the other parts, so don't write an
introduction or conclusion."""

THREAD_CHAT_SYSTEM = """You answer questions about one Reddit thread the user saved. The text below has the
post, then the comments with author, score (upvotes minus downvotes) and a reference like [c:k3x9a].
Replies are indented under the comment they answer. "(OP)" marks the person who posted the thread.

How to answer:
- Base answers on the thread. If it doesn't cover something, say so; add general knowledge only when asked,
  and say it's not from the thread.
- Cite the comments you rely on with their reference in square brackets, e.g. [c:k3x9a]. The user can click
  a reference to jump to that comment.
- Say how well supported a view is (score, how many people say it) when that matters.
- Be concise; use Markdown (short paragraphs, bullet lists) when it helps.
- Answer in the language the user writes in."""

PARTIAL_NOTE = """

Note: this thread is too long for the model, so only the comments that best match the user's latest
question are included below, each with the comment it replies to. Say so if the answer might be in
comments you can't see."""

ALL_SYSTEM = """You answer questions across many Reddit threads the user saved. Each question comes with
excerpts: the comments from the saved threads that best match it (found by keyword search), grouped by
thread, each with author, score (upvotes minus downvotes) and a reference like [c:k3x9a]; some are shown
with the comment they reply to.

How to answer:
- Base answers on the excerpts. Mention which threads (by title) the information comes from.
- Cite the comments you rely on with their reference in square brackets, e.g. [c:k3x9a]. The user can click
  a reference to open that comment.
- The excerpts come from keyword search, so they can miss things or include irrelevant comments. If they
  don't answer the question, say so and suggest other words to search for.
- Say how well supported a view is (score, how many people say it) when that matters.
- Be concise; use Markdown when it helps. Answer in the language the user writes in."""


def now():
    return time.time()


# ---------------------------------------------------------------------- storage

def _path(key):
    if key != ALL:
        storage._path(key)  # validates the id
    return os.path.join(AI_DIR, f"{key}.json")


_file_lock = threading.Lock()


def stored(key):
    try:
        with open(_path(key), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _update(key, fn):
    with _file_lock:
        data = stored(key)
        fn(data)
        os.makedirs(AI_DIR, exist_ok=True)
        tmp = _path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _path(key))


# ---------------------------------------------------------------------- comment links

CITE = re.compile(r"\[((?:\s*c:[a-z0-9]+\s*[,;]?)+)\]", re.I)


def cite_links(text, refs):
    """[c:id] -> [u/author](#c-id) (same thread) or [u/author · title](#t=tid&c=id) (other threads).
    refs: comment id -> {"author", "tid"?, "title"?}. Unknown ids are left as they are."""
    def one(cid):
        r = refs.get(cid.lower())
        if not r:
            return f"c:{cid}"
        label = f"u/{r.get('author') or '[deleted]'}"
        if r.get("tid"):
            short = (r.get("title") or "")[:40] + ("…" if len(r.get("title") or "") > 40 else "")
            return f"[{label} · {short}](#t={r['tid']}&c={cid.lower()})"
        return f"[{label}](#c-{cid.lower()})"

    def sub(m):
        ids = re.findall(r"c:([a-z0-9]+)", m.group(1), re.I)
        return "(" + ", ".join(one(i) for i in ids) + ")"
    return CITE.sub(sub, text)


def thread_refs(data):
    return {(n.get("id") or "").lower(): {"author": n.get("author")}
            for n, _, _ in thread_text.walk(data.get("comments") or [])}


def html_of(text, refs):
    return to_html(cite_links(text, refs))


# ---------------------------------------------------------------------- runner

class Runner:
    def __init__(self):
        self._lock = threading.Lock()
        self._kind_locks = {k: threading.Lock() for k in llm.KINDS}
        self.running = {}  # (key, what) -> {"kind", "stage", "text", "started"}

    def busy(self, key, what):
        return (key, what) in self.running

    def _start(self, key, what, kind, target, *args):
        llm.check(kind)
        with self._lock:
            if (key, what) in self.running:
                raise ValueError("Already running")
            self.running[(key, what)] = {"kind": kind, "stage": "Waiting for the model", "text": "",
                                         "started": now()}
        threading.Thread(target=self._wrap, args=(key, what, kind, target, args), daemon=True).start()

    def _wrap(self, key, what, kind, target, args):
        run = self.running[(key, what)]
        try:
            with self._kind_locks[kind]:
                if kind != "local":
                    run["stage"] = "Thinking"
                    target(run, *args)
                    return
                other = GPU.holder()
                run["stage"] = f"Waiting for the GPU ({other})" if other else "Loading the model"
                GPU.acquire("summarizing" if what.startswith("summary") else "answering a question")
                try:
                    run["stage"] = "Thinking"
                    target(run, *args)
                finally:
                    GPU.release()
        except Exception:
            log.exception("%s %s for %s failed", kind, what, key)
        finally:
            with self._lock:
                self.running.pop((key, what), None)

    def pending(self, key, what):
        r = self.running.get((key, what))
        return dict(r) if r else None

    # ------------------------------------------------------------------ summaries

    def summarize(self, tid, kind):
        if not storage.get(tid):
            raise LookupError("Thread not found")
        self._start(tid, f"summary:{kind}", kind, self._summarize, tid, kind)

    def _summarize(self, run, tid, kind):
        started = now()
        data = storage.get(tid)
        tz = settings.load()["timezone"]
        try:
            if kind != "local":  # Claude and OpenAI read the whole thread at once
                text_in = thread_text.full(data, tz)
                if len(text_in) > CLOUD_THREAD_CHARS:
                    raise RuntimeError("This thread is too long to summarize in one request.")
                run["stage"] = "Summarizing"
                text, usage, model, _ = llm.ask(kind, SUMMARY_SYSTEM, [{"role": "user", "content": text_in}],
                                                on_text=lambda t: run.update(text=t))
            else:
                text, usage, model = self._summarize_local(run, data, tz)
            entry = {"text": text, "model": model, "created": now(), "elapsed": round(now() - started, 1),
                     "usage": usage, "count": data.get("count"), "saved_at": data.get("saved_at")}
        except Exception as e:
            entry = {**(stored(tid).get("summaries", {}).get(kind) or {}), "error": str(e), "error_at": now()}
            log.warning("%s summary of %s failed: %s", kind, tid, e)
        if storage.get(tid):  # not deleted meanwhile
            _update(tid, lambda d: d.setdefault("summaries", {}).__setitem__(kind, entry))

    def _summarize_local(self, run, data, tz):
        budget = llm.local_chars() - len(SUMMARY_SYSTEM)
        text_in = thread_text.full(data, tz)
        if len(text_in) <= budget:
            run["stage"] = "Summarizing"
            text, usage, model, _ = llm.ask("local", SUMMARY_SYSTEM, [{"role": "user", "content": text_in}],
                                            on_text=lambda t: run.update(text=t))
            return text, usage, model
        # Too long for the local context: notes per part, then a summary of the notes.
        head = thread_text.header(data, tz)
        parts = thread_text.chunks(data, tz, budget - len(head) - 200)
        notes, tin, tout = [], 0, 0
        for i, part in enumerate(parts, 1):
            run["stage"] = f"Reading part {i} of {len(parts)}"
            note, usage, _, _ = llm.ask("local", CHUNK_SYSTEM,
                                        [{"role": "user", "content": f"{head}\n\nComments, part {i} of {len(parts)}:\n\n{part}"}])
            notes.append(f"### Part {i}\n{note}")
            tin += usage.get("input") or 0
            tout += usage.get("output") or 0
        run["stage"] = "Merging parts"
        merged = f"{head}\n\nNotes on the comments, written part by part:\n\n" + "\n\n".join(notes)
        text, usage, model, _ = llm.ask("local", SUMMARY_SYSTEM, [{"role": "user", "content": merged[:budget]}],
                                        on_text=lambda t: run.update(text=t))
        return text, {"input": tin + (usage.get("input") or 0), "output": tout + (usage.get("output") or 0),
                      "parts": len(parts)}, model

    def delete_summary(self, tid, kind):
        _update(tid, lambda d: d.get("summaries", {}).pop(kind, None))

    def summary_state(self, tid):
        data = storage.get(tid)
        refs = thread_refs(data) if data else {}
        out = {}
        saved = stored(tid).get("summaries", {})
        for kind in llm.KINDS:
            entry = dict(saved.get(kind) or {})
            if entry.get("text"):
                entry["html"] = html_of(entry["text"], refs)
                if data:
                    entry["new_since"] = max(0, (data.get("count") or 0) - (entry.get("count") or 0)) \
                        if (data.get("saved_at") or 0) > (entry.get("saved_at") or 0) else 0
            p = self.pending(tid, f"summary:{kind}")
            if p:
                entry["running"] = {"stage": p["stage"], "started": p["started"],
                                    "html": html_of(p["text"], refs) if p["text"] else ""}
            out[kind] = entry or None
        return out

    # ------------------------------------------------------------------ chat (one thread or all)

    def ask(self, key, kind, question):
        question = (question or "").strip()[:8000]
        if not question:
            raise ValueError("Type a question")
        if key != ALL and not storage.get(key):
            raise LookupError("Thread not found")
        if self.busy(key, "chat"):
            raise ValueError("Still answering the previous question")
        _update(key, lambda d: d.setdefault("chat", []).append({"role": "user", "text": question, "created": now()}))
        try:
            self._start(key, "chat", kind, self._answer, key, kind)
        except Exception:
            _update(key, lambda d: d["chat"].pop())
            raise

    def _answer(self, run, key, kind):
        started = now()
        chat = stored(key).get("chat", [])
        question = chat[-1]["text"]
        pairs = _pairs(chat[:-1])
        entry = {"role": "assistant", "kind": kind}
        try:
            if key == ALL:
                system, messages, refs, note = self._all_prompt(kind, question, pairs)
            else:
                system, messages, refs, note = self._thread_prompt(key, kind, question, pairs)
            run["refs"] = refs
            text, usage, model, blocks = llm.ask(kind, system, messages, on_text=lambda t: run.update(text=t),
                                                 keep_alive="10m", cache=(key != ALL), max_tokens=32000)
            entry.update(text=text, model=model, usage=usage, elapsed=round(now() - started, 1), refs=refs)
            if note:
                entry["note"] = note
            if blocks and key != ALL:
                entry["blocks"] = blocks
        except Exception as e:
            log.warning("%s answer for %s failed: %s", kind, key, e)
            entry.update(text="", error=str(e))
        entry["created"] = now()
        if key == ALL or storage.get(key):
            _update(key, lambda d: d.setdefault("chat", []).append(entry))

    def _thread_prompt(self, tid, kind, question, pairs):
        data = storage.get(tid)
        tz = settings.load()["timezone"]
        full = thread_text.full(data, tz)
        budget = CLOUD_THREAD_CHARS if kind != "local" else llm.local_chars() - CONVERSATION_CHARS
        note = None
        if len(full) + len(THREAD_CHAT_SYSTEM) <= budget:
            system = f"{THREAD_CHAT_SYSTEM}\n\n<thread>\n{full}\n</thread>"
        else:
            head = thread_text.header(data, tz)
            excerpt, _, n = INDEX.relevant(question, budget - len(head) - len(THREAD_CHAT_SYSTEM) - 1000, tid=tid)
            system = f"{THREAD_CHAT_SYSTEM}{PARTIAL_NOTE}\n\n<thread>\n{head}\n\n{excerpt}\n</thread>"
            note = f"Thread too long for this model: answered from the {n} best-matching comments."
        if kind == "local":
            # Drop the oldest turns until the conversation fits.
            while pairs and sum(len(q["text"]) + len(a["text"]) for q, a in pairs) > CONVERSATION_CHARS:
                pairs = pairs[1:]
        messages = []
        for q, a in pairs:
            messages.append({"role": "user", "content": q["text"]})
            # Claude's own turns go back exactly as returned; other answers as text.
            messages.append({"role": "assistant", "content": a.get("blocks") if kind == "cloud" and a.get("blocks")
                             and a.get("kind") == "cloud" else a["text"]})
        messages.append({"role": "user", "content": question})
        return system, messages, None, note

    def _all_prompt(self, kind, question, pairs):
        budget = CLOUD_ALL_CHARS if kind != "local" else llm.local_chars() - CONVERSATION_CHARS - len(ALL_SYSTEM)
        # Earlier questions help with follow-ups like "and what about X?".
        recent = " ".join(q["text"] for q, _ in pairs[-2:])
        excerpt, ids, n = INDEX.relevant(f"{question} {recent}", budget)
        if not n:
            excerpt = "(No saved comments matched the words in this question.)"
        refs = ids
        while pairs and sum(len(q["text"]) + len(a["text"]) for q, a in pairs) > CONVERSATION_CHARS:
            pairs = pairs[1:]
        messages = []
        for q, a in pairs:
            # Earlier excerpts aren't resent, so earlier answers go back as plain text.
            messages += [{"role": "user", "content": q["text"]}, {"role": "assistant", "content": a["text"]}]
        messages.append({"role": "user", "content": f"<excerpts>\n{excerpt}\n</excerpts>\n\nQuestion: {question}"})
        threads = len({i["tid"] for i in ids.values()}) if n else 0
        return ALL_SYSTEM, messages, refs, f"Searched your saved threads: {n} matching comments from {threads} threads."

    def chat_state(self, key):
        data = storage.get(key) if key != ALL else None
        base_refs = thread_refs(data) if data else {}
        out = []
        for m in stored(key).get("chat", []):
            m = {k: v for k, v in m.items() if k != "blocks"}
            refs = m.pop("refs", None) or base_refs
            if m["role"] == "assistant" and m.get("text"):
                m["html"] = html_of(m["text"], refs)
            out.append(m)
        p = self.pending(key, "chat")
        if p:
            p = {"kind": p["kind"], "stage": p["stage"], "started": p["started"],
                 "html": html_of(p["text"], p.get("refs") or base_refs) if p["text"] else ""}
        return {"messages": out, "pending": p}

    def clear_chat(self, key):
        if self.busy(key, "chat"):
            raise ValueError("Still answering; wait for it to finish")
        _update(key, lambda d: d.pop("chat", None))


def _pairs(messages):
    """Completed question/answer pairs, skipping failed or unanswered ones."""
    return [(messages[i - 1], m) for i, m in enumerate(messages)
            if i and m["role"] == "assistant" and not m.get("error") and messages[i - 1]["role"] == "user"]


RUNNER = Runner()
