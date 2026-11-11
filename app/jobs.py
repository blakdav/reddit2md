"""Conversion queue: threads are fetched one at a time, oldest first, with a pause between them.

Every request to convert a thread becomes a job in an ordered list. A single worker takes
the oldest queued job, runs it, then waits QUEUE_PAUSE seconds (longer, RATE_LIMIT_PAUSE,
if Reddit throttled that job) before starting the next, so a burst of sends doesn't hammer
Reddit. The list is mirrored to queue.json so queued threads survive a container restart
(a job that was running when the container stopped goes back to the front of the line).

Each job keeps a short log of its progress messages, so a failed job shows why it failed.
"""

import json
import logging
import os
import threading
import time
import uuid

from scraper import Cancelled, ScrapeError

log = logging.getLogger("reddit2md")

QUEUE_PAUSE = float(os.environ.get("QUEUE_PAUSE", "15"))  # seconds between threads
RATE_LIMIT_PAUSE = float(os.environ.get("RATE_LIMIT_PAUSE", "120"))  # after a job Reddit throttled
HISTORY = 50  # finished jobs kept in the list
LOG_LINES = 40  # progress lines kept per job
ACTIVE = ("queued", "running")


class Queue:
    def __init__(self, path, runner):
        """runner(job, progress, cancelled, stats) does the work and returns fields to merge into the job.
        It fills stats["counter"] (see scraper.scrape) so throttling is known even when it fails."""
        self.path = path
        self.runner = runner
        self.jobs = []  # oldest -> newest
        self.cond = threading.Condition()
        self.waiting_until = 0  # end of the current pause between threads, for the UI
        self.waiting_why = ""
        self._load()
        threading.Thread(target=self._worker, daemon=True, name="queue").start()

    # ------------------------------------------------------------ persistence

    def _save(self):
        """Caller holds cond."""
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([self._public(j) for j in self.jobs], f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("Could not save queue: %s", e)

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
        except (FileNotFoundError, ValueError):
            return
        running = []
        for j in saved:
            if j.get("status") == "running":
                j["status"] = "queued"
                j["progress"] = "Re-queued after restart"
                running.append(j)
            else:
                self.jobs.append(j)
        # The interrupted job goes first so restarts don't reorder the line.
        self.jobs[:0] = running
        log.info("Queue loaded: %d queued, %d finished",
                 sum(j["status"] == "queued" for j in self.jobs), sum(j["status"] not in ACTIVE for j in self.jobs))

    @staticmethod
    def _public(job):
        return {k: v for k, v in job.items() if not k.startswith("_")}

    # ------------------------------------------------------------ worker

    def _worker(self):
        while True:
            with self.cond:
                while not any(j["status"] == "queued" for j in self.jobs):
                    self.cond.wait()
                job = next(j for j in self.jobs if j["status"] == "queued")
                job.update(status="running", started_at=time.time(), progress="Starting")
                self._save()
            throttled = self._run(job)
            pause = RATE_LIMIT_PAUSE if throttled else QUEUE_PAUSE
            with self.cond:
                more = any(j["status"] == "queued" for j in self.jobs)
            if more and pause > 0:
                why = "Reddit was rate limiting" if throttled else "pause between threads"
                log.info("Next thread in %.0fs (%s)", pause, why)
                self.waiting_until = time.time() + pause
                self.waiting_why = why
                time.sleep(pause)
                self.waiting_until = 0

    def _run(self, job):
        jid = job["id"][:8]

        def progress(msg):
            if job.get("_cancel"):
                raise Cancelled()
            with self.cond:
                job["progress"] = msg
                lines = job.setdefault("log", [])
                if not lines or lines[-1][1] != msg:
                    lines.append([round(time.time()), msg])
                    del lines[:-LOG_LINES]
            log.info("[%s] %s", jid, msg)

        def cancelled():
            return bool(job.get("_cancel"))

        stats = {}
        try:
            fields = self.runner(job, progress, cancelled, stats) or {}
            self._finish(job, "done", progress="Done", **fields)
        except Cancelled:
            self._finish(job, "cancelled", progress="Cancelled")
        except Exception as e:
            if isinstance(e, ScrapeError):
                msg = str(e)
            else:
                log.exception("Job %s failed", jid)
                msg = f"{type(e).__name__}: {e}"
            log.info("[%s] Failed: %s", jid, msg)
            self._finish(job, "error", error=msg, progress="Failed")
        return bool(stats.get("counter", {}).get("throttled"))

    def _finish(self, job, status, **fields):
        with self.cond:
            job.update(status=status, finished_at=time.time(), **fields)
            if status == "error":
                job.setdefault("log", []).append([round(time.time()), "Failed: " + fields.get("error", "")])
            finished = [j for j in self.jobs if j["status"] not in ACTIVE]
            for j in finished[:max(len(finished) - HISTORY, 0)]:
                self.jobs.remove(j)
            self._save()

    # ------------------------------------------------------------ public API

    def add(self, url, sort, mode, same=None):
        """Queue a thread. If same(url_a, url_b) finds it already queued or running, return that job."""
        with self.cond:
            if same:
                for j in self.jobs:
                    if j["status"] in ACTIVE and same(j["url"], url):
                        return self.view(j), False
            job = {"id": uuid.uuid4().hex, "url": url, "sort": sort, "mode": mode, "status": "queued",
                   "progress": "Queued", "added_at": time.time(), "started_at": None, "finished_at": None,
                   "error": None, "log": []}
            self.jobs.append(job)
            self._save()
            self.cond.notify_all()
            return self.view(job), True

    def find(self, jid):
        return next((j for j in self.jobs if j["id"] == jid), None)

    def view(self, job):
        """A job as the API shows it: position in line, elapsed time, and what a queued job waits on."""
        out = self._public(job)
        start = job.get("started_at") or job["added_at"]
        end = job.get("finished_at") or time.time()
        out["elapsed"] = round(end - start)
        if job["status"] == "queued":
            queued = [j for j in self.jobs if j["status"] == "queued"]
            pos = queued.index(job) + 1
            out["position"] = pos
            running = any(j["status"] == "running" for j in self.jobs)
            ahead = pos - 1 + running
            if not ahead and self.waiting_until > time.time():
                out["progress"] = f"Next up, starting in {self.waiting_until - time.time():.0f}s ({self.waiting_why})"
            elif ahead:
                out["progress"] = f"Queued, {ahead} ahead"
        return out

    def list(self):
        with self.cond:
            return [self.view(j) for j in self.jobs]

    def remove(self, jid):
        """Queued or finished: drop from the list. Running: cancel (takes effect within a second or so)."""
        with self.cond:
            job = self.find(jid)
            if not job:
                return False
            if job["status"] == "running":
                job["_cancel"] = True
                job["progress"] = "Cancelling"
            else:
                self.jobs.remove(job)
                self._save()
            return True

    def retry(self, jid):
        with self.cond:
            job = self.find(jid)
            if not job or job["status"] in ACTIVE:
                return None
            self.jobs.remove(job)
        view, _ = self.add(job["url"], job["sort"], job["mode"])
        return view

    def clear_finished(self):
        with self.cond:
            self.jobs[:] = [j for j in self.jobs if j["status"] in ACTIVE]
            self._save()
