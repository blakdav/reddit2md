"""Fetch a full Reddit thread (all comments expanded) using a logged-in Playwright session.

Strategy:
  1. JSON mode: load old.reddit.com in Chromium, then call Reddit's JSON endpoints with
     fetch() from inside the page so the real browser sends the cookies. Every "more"
     stub is expanded via /api/morechildren and every "continue this thread" stub via
     a subtree request.
  2. DOM mode (fallback if JSON is blocked): click every "load more comments" link on
     old.reddit, open every "continue this thread" page, and read the rendered HTML.
"""

import json
import logging
import os
import re
import time
from urllib.parse import quote

from markdownify import markdownify as html_to_md
from playwright.sync_api import sync_playwright

log = logging.getLogger("reddit2md")

STATE_FILE = os.environ.get("STATE_FILE", "/data/reddit_state.json")
BASE = "https://old.reddit.com"
DELAY = float(os.environ.get("REQUEST_DELAY", "1.0"))
MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "400"))
USER_AGENT = os.environ.get("USER_AGENT", "").strip()
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "30"))  # seconds per Reddit request
MAX_SECONDS = float(os.environ.get("MAX_SECONDS", "900"))  # hard cap per conversion

POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)", re.I)
SHORT_RE = re.compile(r"redd\.it/([a-z0-9]+)", re.I)


class ScrapeError(Exception):
    pass


class Blocked(Exception):
    pass


class RateLimited(Blocked):
    """Reddit answered 429. retry_after: seconds Reddit asked us to wait (from its headers), if known."""
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


# After a 429 on the JSON endpoints, skip them (go straight to page mode) until this time.
JSON_COOLDOWN = float(os.environ.get("JSON_COOLDOWN", "900"))
_json_blocked_until = 0.0


class Incomplete(ScrapeError):
    """Hit MAX_SECONDS or MAX_REQUESTS. Callers that already have comments save them as a partial thread."""


def new_node(cid, author, score, created, body):
    return {"id": cid, "author": author, "score": score, "created": created,
            "body": body, "replies": []}


# ---------------------------------------------------------------- browser helpers

def _launch(p):
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    )
    # Match the bundled Chromium's real version (reduced form, like desktop Chrome) and
    # drop the "HeadlessChrome" marker, unless USER_AGENT is set explicitly.
    ua = USER_AGENT or (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{browser.version.split('.')[0]}.0.0.0 Safari/537.36"
    )
    kwargs = {"user_agent": ua, "viewport": {"width": 1280, "height": 900}}
    if os.path.exists(STATE_FILE):
        kwargs["storage_state"] = STATE_FILE
    context = browser.new_context(**kwargs)
    return browser, context


def _save_state(context):
    """Write refreshed cookies back so the session lives longer. Best effort."""
    if not os.path.exists(STATE_FILE):
        return
    try:
        context.storage_state(path=STATE_FILE)
    except Exception as e:  # read-only mount, permissions, etc.
        log.warning("Could not save refreshed session state: %s", e)


class Cancelled(Exception):
    pass


def _new_counter(cancelled=None):
    """Per-conversion state: request count, deadline, and how often Reddit throttled us."""
    return {"n": 0, "deadline": time.time() + MAX_SECONDS, "throttled": 0, "skipped": 0,
            "cancelled": cancelled or (lambda: False)}


def _sleep(counter, seconds):
    """time.sleep that a cancelled job can interrupt."""
    end = time.time() + seconds
    while True:
        if counter["cancelled"]():
            raise Cancelled()
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def _check_deadline(counter):
    if time.time() > counter["deadline"]:
        raise Incomplete(f"Stopped after {MAX_SECONDS:.0f}s (MAX_SECONDS); Reddit was slow or rate limiting")


def _mark_partial(counter, reason, progress):
    counter["partial"] = reason
    progress(f"{reason}. Saving what was collected")


def _fetch_json(page, path, counter):
    _check_deadline(counter)
    counter["n"] += 1
    if counter["n"] > MAX_REQUESTS:
        raise Incomplete(f"Hit MAX_REQUESTS ({MAX_REQUESTS}); the thread is huge (raise the limit in compose)")
    for attempt in range(4):
        # Abort a stalled request so one hung connection can't hold the browser forever.
        res = page.evaluate(
            """async ([p, ms]) => {
                const ctl = new AbortController();
                const timer = setTimeout(() => ctl.abort(), ms);
                try {
                    const r = await fetch(p, {credentials: 'include', headers: {'Accept': 'application/json'}, signal: ctl.signal});
                    const t = await r.text();
                    const h = (n) => r.headers.get(n);
                    return {status: r.status, text: t, remaining: h('x-ratelimit-remaining'),
                            reset: h('x-ratelimit-reset'), retry: h('retry-after')};
                } catch (e) {
                    return {status: 0, text: String(e)};
                } finally {
                    clearTimeout(timer);
                }
            }""",
            [path, int(FETCH_TIMEOUT * 1000)],
        )
        status = res["status"]
        reset = _num(res.get("retry")) or _num(res.get("reset"))
        if status == 200:
            remaining = _num(res.get("remaining"))
            if remaining is not None and remaining < 1 and reset:
                # Out of requests for this window: wait for it to reset rather than earn a 429.
                log.info("Reddit rate limit used up; waiting %.0fs for it to reset", reset)
                _sleep(counter, min(reset, 600) + 1)
            else:
                _sleep(counter, DELAY)
            try:
                return json.loads(res["text"])
            except ValueError:
                raise Blocked("Non-JSON response")
        if status == 429:
            counter["throttled"] += 1
            # One quick retry; after that, hand control back so the caller can switch to page mode or save
            # what it has, instead of burning a minute on doubling waits.
            if attempt == 0 and (reset or 0) <= 10:
                wait = max(reset or 0, DELAY * 4)
                log.info("HTTP 429 on %s, retrying in %.0fs", path, wait)
                _sleep(counter, wait)
                continue
            raise RateLimited(f"Reddit is rate limiting the JSON API (HTTP 429"
                              + (f", reset in {reset:.0f}s)" if reset else ")"), reset)
        if status == 0 or status >= 500:
            wait = DELAY * (2 ** (attempt + 2))
            log.info("HTTP %s on %s, retrying in %.0fs", status or "timeout/network error", path, wait)
            _sleep(counter, wait)
            continue
        raise Blocked(f"HTTP {status}")
    raise ScrapeError(f"Reddit kept timing out or erroring (HTTP {res['status'] or 'timeout'}) on {path}")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _note_json_limited(e):
    global _json_blocked_until
    _json_blocked_until = time.time() + max(JSON_COOLDOWN, e.retry_after or 0)
    log.info("JSON API rate limited; using page mode until %s",
             time.strftime("%H:%M", time.localtime(_json_blocked_until)))


def _logged_in_user(page):
    """Read the username from old.reddit's header, or None when logged out. No extra request."""
    try:
        user = page.locator("#header-bottom-right .user a").first
        if user.count() and "login" not in (user.get_attribute("href") or ""):
            return user.inner_text().strip() or None
    except Exception:
        pass
    return None


def resolve_post_id(page, url):
    url = url.strip()
    if not url:
        raise ScrapeError("No URL given")
    if not url.startswith("http"):
        url = "https://" + url
    m = POST_ID_RE.search(url) or SHORT_RE.search(url)
    if m:
        return m.group(1)
    # Share links (/r/sub/s/xxxx) redirect to the real thread.
    page.goto(url.replace("old.reddit.com", "www.reddit.com"), wait_until="domcontentloaded", timeout=60000)
    m = POST_ID_RE.search(page.url)
    if not m:
        raise ScrapeError(f"Could not find a thread ID in {url}")
    return m.group(1)


# ---------------------------------------------------------------- JSON mode

def _parse_listing(children, parent, nodes, pending):
    """Walk a listing's children into parent['replies']. Collect 'more' stubs into pending."""
    for child in children:
        kind, d = child.get("kind"), child.get("data", {})
        if kind == "t1":
            if d["id"] in nodes:
                continue
            node = new_node(d["id"], d.get("author"), d.get("score"), d.get("created_utc"), d.get("body", ""))
            nodes[d["id"]] = node
            parent["replies"].append(node)
            replies = d.get("replies")
            if isinstance(replies, dict):
                _parse_listing(replies["data"]["children"], node, nodes, pending)
        elif kind == "more":
            pending.append((parent, d))


def scrape_json(page, post_id, sort, counter, progress):
    qs = f"limit=500&depth=100&raw_json=1&sort={quote(sort)}"
    data = _fetch_json(page, f"/comments/{post_id}.json?{qs}", counter)
    p = data[0]["data"]["children"][0]["data"]
    post = {
        "title": p.get("title"), "author": p.get("author"), "subreddit": p.get("subreddit"),
        "score": p.get("score"), "created": p.get("created_utc"), "num_comments": p.get("num_comments"),
        "permalink": "https://www.reddit.com" + p.get("permalink", ""),
        "url": None if p.get("is_self") else p.get("url"),
        "body": p.get("selftext", ""),
    }
    root = {"id": "t3_" + post_id, "replies": []}
    nodes = {}
    pending = []
    _parse_listing(data[1]["data"]["children"], root, nodes, pending)
    try:
        _expand_json(page, post_id, sort, qs, counter, progress, root, nodes, pending)
    except (Incomplete, RateLimited) as e:
        if isinstance(e, RateLimited):
            _note_json_limited(e)
        _mark_partial(counter, str(e), progress)
    return post, root["replies"], len(nodes)


def _expand_json(page, post_id, sort, qs, counter, progress, root, nodes, pending):
    while pending:
        parent, more = pending.pop(0)
        all_ids = more.get("children", [])
        ids = [c for c in all_ids if c not in nodes]
        if all_ids:
            for i in range(0, len(ids), 100):
                batch = ids[i:i + 100]
                progress(f"Expanding {len(batch)} hidden comments ({len(nodes)} loaded so far)")
                r = _fetch_json(
                    page,
                    f"/api/morechildren.json?api_type=json&raw_json=1&limit_children=false"
                    f"&sort={quote(sort)}&link_id=t3_{post_id}&children={','.join(batch)}",
                    counter,
                )
                things = r.get("json", {}).get("data", {}).get("things", [])
                orphans = []
                for t in things:
                    d = t.get("data", {})
                    pid = d.get("parent_id", "")
                    target = root if pid.startswith("t3_") else nodes.get(pid[3:])
                    if t.get("kind") == "more":
                        pending.append((target or parent, d))
                        continue
                    if t.get("kind") != "t1" or d.get("id") in nodes:
                        continue
                    node = new_node(d["id"], d.get("author"), d.get("score"), d.get("created_utc"), d.get("body", ""))
                    nodes[d["id"]] = node
                    if target is None:
                        orphans.append((pid[3:], node))
                    else:
                        target["replies"].append(node)
                for pid, node in orphans:
                    nodes.get(pid, parent)["replies"].append(node)
        else:
            # "Continue this thread": depth limit reached, fetch the subtree rooted at parent.
            pid = parent["id"]
            if pid.startswith("t3_"):
                continue
            progress(f"Following deep thread under comment {pid} ({len(nodes)} loaded so far)")
            sub = _fetch_json(page, f"/comments/{post_id}/_/{pid}.json?{qs}", counter)
            for c in sub[1]["data"]["children"]:
                if c.get("kind") == "t1" and c["data"]["id"] == pid:
                    replies = c["data"].get("replies")
                    if isinstance(replies, dict):
                        _parse_listing(replies["data"]["children"], parent, nodes, pending)


# ---------------------------------------------------------------- DOM mode (fallback)

EXTRACT_JS = """
(rootSel) => {
  const walk = (table) => {
    const out = [];
    if (!table) return out;
    for (const thing of table.querySelectorAll(':scope > .thing')) {
      if (!thing.classList.contains('comment')) continue;
      if (thing.classList.contains('comment')) {
        const entry = thing.querySelector(':scope > .entry');
        const md = entry && entry.querySelector('.usertext-body .md');
        const score = entry && entry.querySelector('.score.unvoted');
        const time = entry && entry.querySelector('time');
        out.push({
          id: (thing.getAttribute('data-fullname') || '').replace('t1_', ''),
          author: thing.getAttribute('data-author') || '[deleted]',
          score: score ? parseInt(score.getAttribute('title')) : null,
          created: time ? Date.parse(time.getAttribute('datetime')) / 1000 : null,
          html: md ? md.innerHTML : (entry && entry.innerText.includes('[removed]') ? '[removed]' : '[deleted]'),
          deep: thing.querySelector(':scope > .child > .sitetable > .deepthread a, :scope > .child > .sitetable > .morerecursion a')?.href || null,
          replies: walk(thing.querySelector(':scope > .child > .sitetable')),
        });
      }
    }
    return out;
  };
  const table = document.querySelector(rootSel);
  return walk(table);
}
"""

POST_JS = """
() => {
  const t = document.querySelector('#siteTable > .thing.link');
  const md = t.querySelector('.expando .usertext-body .md');
  const time = t.querySelector('time');
  return {
    title: t.querySelector('a.title').innerText,
    author: t.getAttribute('data-author'),
    subreddit: t.getAttribute('data-subreddit'),
    score: parseInt(t.getAttribute('data-score')),
    created: time ? Date.parse(time.getAttribute('datetime')) / 1000 : null,
    num_comments: parseInt(t.getAttribute('data-comments-count')),
    permalink: 'https://www.reddit.com' + t.getAttribute('data-permalink'),
    url: t.classList.contains('self') ? null : t.getAttribute('data-url'),
    html: md ? md.innerHTML : '',
  };
}
"""


LINK_TRIES = 3  # clicks per "load more" link before giving up on it


def _expand_all(page, progress, counter):
    """Click every "load more comments" link.

    A failed click (usually Reddit rate limiting) leaves the link on the page. Instead of
    clicking it again every second, back off and retry; skip a link after LINK_TRIES so
    one dead link can't burn the whole request budget.
    """
    rounds = stalls = skipped = 0
    while rounds < MAX_REQUESTS:
        _check_deadline(counter)
        links = page.locator(".thing.morechildren .morecomments a:visible:not([data-r2m-skip])")
        n = links.count()
        if n == 0:
            break
        rounds += 1
        progress(f"Clicking 'load more comments' ({n} remaining links, round {rounds})")
        link = links.first
        try:
            handle = link.element_handle(timeout=10000)
            handle.click(timeout=10000)
        except Exception as e:
            log.info("load-more click issue: %s", e)
            handle = None
        loaded = False
        if handle:
            # On success old.reddit replaces the whole "more" stub, removing the link.
            try:
                page.wait_for_function("e => !e.isConnected || !e.closest('.thing.morechildren')",
                                       arg=handle, timeout=15000)
                loaded = True
            except Exception:
                pass
        if loaded:
            stalls = 0
            _sleep(counter, DELAY)
            continue
        stalls += 1
        counter["throttled"] += 1
        if handle:
            tries = handle.evaluate(
                "(e, max) => { const t = +(e.dataset.r2mTries || 0) + 1; e.dataset.r2mTries = t;"
                " if (t >= max) e.dataset.r2mSkip = '1'; return t; }", LINK_TRIES)
            if tries >= LINK_TRIES:
                skipped += 1
                counter["skipped"] += 1
                log.info("Skipping a 'load more' link after %d failed clicks", tries)
        wait = min(60, DELAY * 2 ** stalls)
        progress(f"'Load more' didn't load (Reddit may be rate limiting); waiting {wait:.0f}s")
        _sleep(counter, wait)
    if skipped:
        progress(f"Gave up on {skipped} 'load more' link(s); some comments may be missing")
    if rounds >= MAX_REQUESTS:
        raise Incomplete(f"Hit MAX_REQUESTS ({MAX_REQUESTS}) clicking 'load more' (raise the limit in compose)")


def _to_nodes(items):
    out = []
    for it in items:
        body = it["html"] if it["html"] in ("[removed]", "[deleted]") else html_to_md(it["html"]).strip()
        node = new_node(it["id"], it["author"], it["score"], it["created"], body)
        node["deep"] = it.get("deep")
        node["replies"] = _to_nodes(it["replies"])
        out.append(node)
    return out


def scrape_dom(page, post_id, sort, progress, counter):
    page.goto(f"{BASE}/comments/{post_id}/?limit=500&sort={quote(sort)}", wait_until="domcontentloaded", timeout=60000)
    if page.locator("#siteTable > .thing.link").count() == 0:
        raise ScrapeError("Thread page did not render. Session may be logged out or blocked.")
    raw = page.evaluate(POST_JS)
    post = {k: v for k, v in raw.items() if k != "html"}
    post["body"] = html_to_md(raw["html"]).strip() if raw["html"] else ""
    try:
        _expand_all(page, progress, counter)
    except Incomplete as e:
        # Keep whatever is on the page; deep threads are skipped below.
        _mark_partial(counter, str(e), progress)
    comments = _to_nodes(page.evaluate(EXTRACT_JS, ".commentarea > .sitetable"))

    # Follow "continue this thread" links breadth-first.
    queue = []

    def collect(nodes):
        for n in nodes:
            if n.get("deep"):
                queue.append(n)
            collect(n["replies"])

    collect(comments)
    seen = set()
    while queue and not counter.get("partial"):
        try:
            _check_deadline(counter)
        except Incomplete as e:
            _mark_partial(counter, str(e), progress)
            break
        node = queue.pop(0)
        url = node.pop("deep")
        if url in seen:
            continue
        seen.add(url)
        progress(f"Opening deep thread under comment {node['id']}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        _sleep(counter, DELAY)
        try:
            _expand_all(page, progress, counter)
        except Incomplete as e:
            _mark_partial(counter, str(e), progress)
        sub = _to_nodes(page.evaluate(EXTRACT_JS, ".commentarea > .sitetable"))
        # The deep page's root is the parent comment itself.
        root = next((s for s in sub if s["id"] == node["id"]), sub[0] if sub else None)
        if root:
            node["replies"].extend(root["replies"])
            collect(root["replies"])

    if queue:
        counter["deep_skipped"] = len(queue)
    for n in queue:  # deep threads never opened (stopped early)
        n.pop("deep", None)

    def count(nodes):
        return sum(1 + count(n["replies"]) for n in nodes)

    return post, comments, count(comments)


# ---------------------------------------------------------------- entry points

def scrape(url, sort="confidence", mode="auto", progress=lambda msg: None, cancelled=None, stats=None):
    """stats, if given, is filled with throttled/skipped counts even when the scrape fails."""
    counter = _new_counter(cancelled)
    if stats is not None:
        stats["counter"] = counter
    with sync_playwright() as p:
        browser, context = _launch(p)
        try:
            page = context.new_page()
            page.set_default_timeout(60000)
            post_id = resolve_post_id(page, url)
            progress(f"Thread ID {post_id}")
            page.goto(f"{BASE}/comments/{post_id}/", wait_until="domcontentloaded", timeout=60000)
            user = _logged_in_user(page)
            used = mode
            if mode == "auto" and time.time() < _json_blocked_until:
                progress("JSON API was rate limited recently; reading the page instead")
                post, comments, n = scrape_dom(page, post_id, sort, progress, counter)
                used = "dom"
            elif mode in ("auto", "json"):
                try:
                    post, comments, n = scrape_json(page, post_id, sort, counter, progress)
                    used = "json"
                except Blocked as e:
                    if isinstance(e, RateLimited):
                        _note_json_limited(e)
                    if mode == "json":
                        raise ScrapeError(f"JSON endpoints blocked ({e}). Try Browser clicks mode or refresh the session.")
                    progress(f"JSON blocked ({e}), reading the page instead")
                    post, comments, n = scrape_dom(page, post_id, sort, progress, counter)
                    used = "dom"
            else:
                post, comments, n = scrape_dom(page, post_id, sort, progress, counter)
            _save_state(context)
            return {"id": post_id.lower(), "post": post, "comments": comments, "count": n, "mode": used,
                    "sort": sort, "user": user, "partial": _partial_reason(counter)}
        finally:
            context.close()
            browser.close()


def _partial_reason(counter):
    """Why this fetch is missing comments, or None when it got everything it could find."""
    reasons = []
    if counter.get("partial"):
        reasons.append(counter["partial"])
    if counter.get("skipped"):
        reasons.append(f"skipped {counter['skipped']} 'load more' link(s) that kept failing (likely rate limiting)")
    if counter.get("deep_skipped"):
        reasons.append(f"{counter['deep_skipped']} 'continue this thread' link(s) not opened")
    return "; ".join(reasons) or None


def session_status():
    """Report whether a session file exists and whether Reddit sees us as logged in."""
    info = {"state_file": os.path.exists(STATE_FILE), "logged_in": False, "user": None, "json_ok": None}
    with sync_playwright() as p:
        browser, context = _launch(p)
        try:
            page = context.new_page()
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
            info["user"] = _logged_in_user(page)
            info["logged_in"] = bool(info["user"])
            try:
                _fetch_json(page, "/api/me.json", _new_counter())
                info["json_ok"] = True
            except Blocked:
                info["json_ok"] = False
            _save_state(context)
        finally:
            context.close()
            browser.close()
    return info
