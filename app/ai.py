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
from concurrent.futures import ThreadPoolExecutor

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

# A local job that runs longer than this is stopped (a stuck Ollama would otherwise block every
# later local job). Waiting for a turn or for the GPU has its own, longer limit.
LOCAL_MAX_SECONDS = int(os.environ.get("LOCAL_AI_MAX_SECONDS", "3600"))
WAIT_MAX_SECONDS = int(os.environ.get("LOCAL_AI_MAX_WAIT", "7200"))


class Stopped(Exception):
    """The user pressed Stop, or a time limit ran out."""


def _check(run):
    if run.get("cancel"):
        raise Stopped("Stopped")
    if run.get("deadline") and now() > run["deadline"]:
        raise Stopped(f"Stopped after {LOCAL_MAX_SECONDS // 60} minutes; the local model seems stuck "
                      "(check that Ollama is running)")


def _streamer(run):
    """on_text callback: show the answer so far, and stop the stream if Stop was pressed."""
    def on_text(t):
        _check(run)
        run["text"] = t
    return on_text

# Focused answers (Claude and OpenAI, big threads): a cheap scout model reads the whole thread
# in parts and lists the comments that bear on the question; the main model answers from those
# plus the thread's summary. About 1/7 the cost of sending everything to the main model.
FOCUS_MIN_CHARS = 100_000   # ~30K tokens: smaller threads are always sent whole
SCOUT_PART_CHARS = 200_000  # ~57K tokens per scout request; parts run in parallel
SCOUT_WORKERS = 4
FOCUS_MAX_CHARS = 80_000    # ~23K tokens of picked comments for the main model

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

SCOUT_SYSTEM = """You help answer a question about a long Reddit thread by finding the comments that matter.
You see the post and one part of the comments. Each comment starts with a reference like [c:k3x9a];
replies are indented under the comment they answer.

List the reference of every comment in this part that helps answer the question: direct answers,
first-hand experiences, facts, numbers, advice, and replies that confirm or dispute them. Comments may
describe the same thing in different words than the question; include those. Leave out comments that are
clearly off-topic. If earlier questions are given, the latest question may refer back to them.

Reply with only the references, comma-separated, like: c:k3x9a, c:m2p8q
If nothing in this part is relevant, reply: NONE"""

FOCUS_NOTE = """

Note: this thread is long, so a quick first pass picked out the comments that bear on the user's latest
question, and only those are included below, each under the comments it replies to (those are shown for
context and may be shortened). A summary of the whole thread comes first when one exists; use it for
questions about the thread as a whole. Say so if the answer might depend on comments that aren't shown."""

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
        self.running = {}  # (key, what) -> {"kind", "stage", "text", "started", "label", "has_turn", "cancel"}

    def busy(self, key, what):
        return (key, what) in self.running

    def _start(self, key, what, kind, target, *args):
        llm.check(kind)
        with self._lock:
            if (key, what) in self.running:
                raise ValueError("Already running")
            self.running[(key, what)] = {"kind": kind, "stage": "Starting", "text": "", "started": now(),
                                         "label": _label(key, what)}
        threading.Thread(target=self._wrap, args=(key, what, kind, target, args), daemon=True).start()

    def stop(self, key, what):
        run = self.running.get((key, what))
        if not run:
            raise LookupError("Nothing is running")
        run["cancel"] = True
        run["stage"] = "Stopping"

    def _wait_turn(self, run, kind):
        """One job per kind at a time. While waiting, say which job we're waiting for."""
        lock, since = self._kind_locks[kind], now()
        while not lock.acquire(timeout=2):
            _check(run)
            if now() - since > WAIT_MAX_SECONDS:
                raise Stopped(f"Gave up after waiting {WAIT_MAX_SECONDS // 60} minutes for another {llm.NAMES[kind]} job")
            other = next((r for r in list(self.running.values()) if r is not run and r["kind"] == kind and r.get("has_turn")), None)
            run["stage"] = (f"Waiting for another {llm.NAMES[kind]} job to finish: {other['label']} "
                            f"({other['stage']}, {int((now() - other['started']) // 60)} min so far)"
                            if other else f"Waiting for another {llm.NAMES[kind]} job to finish")
        run["has_turn"] = True
        return lock

    def _wait_gpu(self, run, detail):
        since = now()
        while not GPU.try_acquire(detail):
            _check(run)
            if now() - since > WAIT_MAX_SECONDS:
                raise Stopped(f"Gave up after waiting {WAIT_MAX_SECONDS // 60} minutes for the GPU")
            run["stage"] = f"Waiting for the GPU ({GPU.holder() or 'in use'})"
            time.sleep(2)

    def _wrap(self, key, what, kind, target, args):
        run = self.running[(key, what)]
        try:
            lock = self._wait_turn(run, kind)
            try:
                if kind != "local":
                    run["stage"] = "Thinking"
                    target(run, *args)
                    return
                self._wait_gpu(run, "summarizing" if what.startswith("summary") else "answering a question")
                try:
                    run["stage"] = "Thinking"
                    run["deadline"] = now() + LOCAL_MAX_SECONDS
                    target(run, *args)
                finally:
                    GPU.release()
            finally:
                lock.release()
        except Stopped as e:
            # Stopped before the job itself started: record it where the answer or summary would go.
            log.info("%s %s for %s: %s", kind, what, key, e)
            self._record_stop(key, what, kind, str(e))
        except Exception:
            log.exception("%s %s for %s failed", kind, what, key)
        finally:
            with self._lock:
                self.running.pop((key, what), None)

    def _record_stop(self, key, what, kind, msg):
        if what == "chat":
            entry = {"role": "assistant", "kind": kind, "text": "", "error": msg, "created": now()}
            _update(key, lambda d: d.setdefault("chat", []).append(entry))
        elif what.startswith("summary:") and storage.get(key):
            k = what.split(":", 1)[1]
            entry = {**(stored(key).get("summaries", {}).get(k) or {}), "error": msg, "error_at": now()}
            _update(key, lambda d: d.setdefault("summaries", {}).__setitem__(k, entry))

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
                                                on_text=_streamer(run))
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
                                            on_text=_streamer(run))
            return text, usage, model
        # Too long for the local context: notes per part, then a summary of the notes.
        head = thread_text.header(data, tz)
        parts = thread_text.chunks(data, tz, budget - len(head) - 200)
        notes, tin, tout = [], 0, 0
        for i, part in enumerate(parts, 1):
            _check(run)
            run["stage"] = f"Reading part {i} of {len(parts)}"
            note, usage, _, _ = llm.ask("local", CHUNK_SYSTEM,
                                        [{"role": "user", "content": f"{head}\n\nComments, part {i} of {len(parts)}:\n\n{part}"}],
                                        on_text=lambda t: _check(run),
                                        # Stay loaded for the next part; unloading and reloading 14 GB per
                                        # part is slow and once left Ollama stuck at "Stopping...".
                                        # The final merge below unloads it (keep_alive 0).
                                        keep_alive="5m")
            notes.append(f"### Part {i}\n{note}")
            tin += usage.get("input") or 0
            tout += usage.get("output") or 0
        run["stage"] = "Merging parts"
        merged = f"{head}\n\nNotes on the comments, written part by part:\n\n" + "\n\n".join(notes)
        text, usage, model, _ = llm.ask("local", SUMMARY_SYSTEM, [{"role": "user", "content": merged[:budget]}],
                                        on_text=_streamer(run))
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

    def ask(self, key, kind, question, whole=False):
        question = (question or "").strip()[:8000]
        if not question:
            raise ValueError("Type a question")
        if key != ALL and not storage.get(key):
            raise LookupError("Thread not found")
        if self.busy(key, "chat"):
            raise ValueError("Still answering the previous question")
        _update(key, lambda d: d.setdefault("chat", []).append({"role": "user", "text": question, "created": now()}))
        try:
            self._start(key, "chat", kind, self._answer, key, kind, bool(whole))
        except Exception:
            _update(key, lambda d: d["chat"].pop())
            raise

    def _answer(self, run, key, kind, whole=False):
        started = now()
        chat = stored(key).get("chat", [])
        question = chat[-1]["text"]
        pairs = _pairs(chat[:-1])
        entry = {"role": "assistant", "kind": kind}
        scout = None
        try:
            if key == ALL:
                system, messages, refs, note = self._all_prompt(kind, question, pairs)
            else:
                system, messages, refs, note, scout = self._thread_prompt(run, key, kind, question, pairs, whole)
            run["refs"] = refs
            run["stage"] = "Thinking"
            # A focused prompt changes with every question, so caching it would only add Claude's cache-write cost.
            text, usage, model, blocks = llm.ask(kind, system, messages, on_text=_streamer(run),
                                                 keep_alive="10m", cache=(key != ALL and not scout), max_tokens=32000)
            if scout:
                usage = dict(usage, scout=scout)
                if usage.get("cost_usd") is not None and scout.get("cost_usd") is not None:
                    usage["cost_usd"] = round(usage["cost_usd"] + scout["cost_usd"], 4)
                if scout.get("cost_is_max"):
                    usage["cost_is_max"] = True
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

    def _thread_prompt(self, run, tid, kind, question, pairs, whole=False):
        data = storage.get(tid)
        tz = settings.load()["timezone"]
        full = thread_text.full(data, tz)
        budget = CLOUD_THREAD_CHARS if kind != "local" else llm.local_chars() - CONVERSATION_CHARS
        note = scout = None
        focused = None
        if kind != "local" and not whole and len(full) > FOCUS_MIN_CHARS:
            # If the scout fails, stop: never fall back to the much pricier whole-thread request on its own.
            try:
                focused = self._focus(run, tid, data, kind, question, pairs, tz)
            except Exception as e:
                log.warning("Scout pass for %s failed: %s", tid, e)
                raise RuntimeError(f"The first pass that picks comments failed ({e}). Nothing was sent to the "
                                   "main model. Ask again, or choose Whole thread to send everything.") from e
        if focused:
            system, note, scout = focused
        elif len(full) + len(THREAD_CHAT_SYSTEM) <= budget:
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
        return system, messages, None, note, scout

    def _focus(self, run, tid, data, kind, question, pairs, tz):
        """Scout pass: returns (system prompt with the picked comments, note, scout usage)."""
        head = thread_text.header(data, tz)
        parts = thread_text.chunks(data, tz, SCOUT_PART_CHARS)
        earlier = "\n".join(f"- {q['text'][:500]}" for q, _ in pairs[-3:])
        ask_text = (f"Earlier questions in this conversation:\n{earlier}\n\n" if earlier else "") \
            + f"Question: {question}"
        run["stage"] = f"Finding the relevant comments ({len(parts)} part{'s' if len(parts) != 1 else ''})"

        def one(item):
            i, part = item
            msg = f"{head}\n\nComments, part {i} of {len(parts)}:\n\n{part}\n\n{ask_text}"
            return llm.ask(kind, SCOUT_SYSTEM, [{"role": "user", "content": msg}], scout=True, max_tokens=16000)

        with ThreadPoolExecutor(max_workers=SCOUT_WORKERS) as pool:
            results = list(pool.map(one, enumerate(parts, 1)))

        ids = []
        for text, _, _, _ in results:
            ids += re.findall(r"c:([a-z0-9]+)", text, re.I)
        usage = {"model": results[0][2] if results else None, "parts": len(parts), "input": 0, "output": 0}
        costs = [u.get("cost_usd") for _, u, _, _ in results]
        for _, u, _, _ in results:
            usage["input"] += u.get("input") or 0
            usage["output"] += u.get("output") or 0
            usage["cached"] = usage.get("cached", 0) + (u.get("cached") or 0)
            if u.get("cost_is_max"):
                usage["cost_is_max"] = True
        if costs and all(c is not None for c in costs):
            usage["cost_usd"] = round(sum(costs), 4)

        excerpt, picked, cut = thread_text.focused(data, ids, tz, FOCUS_MAX_CHARS)
        usage["picked"] = picked
        summaries = stored(tid).get("summaries", {})
        summary = next((summaries[k]["text"] for k in (kind, *llm.KINDS)
                        if (summaries.get(k) or {}).get("text")), None)
        system = f"{THREAD_CHAT_SYSTEM}{FOCUS_NOTE}\n\n<thread>\n{head}"
        if summary:
            system += f"\n\n<summary of the whole thread>\n{summary}\n</summary of the whole thread>"
        system += ("\n\nComments picked for this question:\n\n" + excerpt if picked
                   else "\n\n(The first pass found no comments about this question.)") + "\n</thread>"
        note = (f"Focused: {usage['model']} read all {data.get('count')} comments and picked {picked}"
                + (f" ({cut} more left out for length)" if cut else "")
                + (", plus the thread summary" if summary else "") + ". Pick \"Whole thread\" to send everything.")
        return system, note, usage

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


def _label(key, what):
    """Short description of a job, shown to other jobs waiting for their turn."""
    kind = "summary" if what.startswith("summary") else "question"
    if key == ALL:
        return "question across all threads"
    data = storage.get(key) or {}
    title = (data.get("title") or key)[:50]
    return f"{kind} of \u201c{title}\u201d" if kind == "summary" else f"question on \u201c{title}\u201d"


def _pairs(messages):
    """Completed question/answer pairs, skipping failed or unanswered ones."""
    return [(messages[i - 1], m) for i, m in enumerate(messages)
            if i and m["role"] == "assistant" and not m.get("error") and messages[i - 1]["role"] == "user"]


RUNNER = Runner()
