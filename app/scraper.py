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
from urllib.parse import quote, urlencode

from markdownify import markdownify as html_to_md
from playwright.sync_api import sync_playwright

log = logging.getLogger("reddit2md")

STATE_FILE = os.environ.get("STATE_FILE", "/data/reddit_state.json")
BASE = "https://old.reddit.com"
DELAY = float(os.environ.get("REQUEST_DELAY", "1.0"))
MAX_REQUESTS = int(os.environ.get("MAX_REQUESTS", "400"))
USER_AGENT = os.environ.get("USER_AGENT", "").strip()
FETCH_TIMEOUT = float(os.environ.get("FETCH_TIMEOUT", "30"))  # seconds per Reddit request
MAX_SECONDS = float(os.environ.get("MAX_SECONDS", "1800"))  # hard cap per conversion

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


class RetryLater(ScrapeError):
    """Reddit rate limited us before anything useful was fetched; the queue retries the thread later."""
    def __init__(self, msg, retry_after=None):
        super().__init__(msg)
        self.retry_after = retry_after


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


def _fetch_json(page, path, counter, form=None, modhash=""):
    """GET path, or POST it as a form when `form` is given (with the modhash, like Reddit's own page)."""
    _check_deadline(counter)
    counter["n"] += 1
    if counter["n"] > MAX_REQUESTS:
        raise Incomplete(f"Hit MAX_REQUESTS ({MAX_REQUESTS}); the thread is huge (raise the limit in compose)")
    for attempt in range(4):
        # Abort a stalled request so one hung connection can't hold the browser forever.
        res = page.evaluate(
            """async ([p, ms, form, uh]) => {
                const ctl = new AbortController();
                const timer = setTimeout(() => ctl.abort(), ms);
                try {
                    const opts = {credentials: 'include', headers: {'Accept': 'application/json'}, signal: ctl.signal};
                    if (form !== null) {
                        opts.method = 'POST';
                        opts.body = form;
                        opts.headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8';
                        if (uh) opts.headers['X-Modhash'] = uh;
                    }
                    const r = await fetch(p, opts);
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
            [path, int(FETCH_TIMEOUT * 1000), urlencode(form) if form else None, modhash],
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
            node = nodes.get(d["id"])
            if node is None:
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
        if sort != "new":
            _sweep_newest(page, post_id, counter, progress, root, nodes)
    except (Incomplete, RateLimited) as e:
        if isinstance(e, RateLimited):
            _note_json_limited(e)
        _mark_partial(counter, str(e), progress)
    return post, root["replies"], len(nodes)


def _sweep_newest(page, post_id, counter, progress, root, nodes):
    """Second pass sorted by New, fetching only comments the first pass didn't reach.

    In Best/Top order, new low-score comments sit at the very end, inside Reddit's last
    "load more" stubs; if anything about those stubs goes wrong they are lost without an
    error. Sorted by New they come first. Comments already loaded cost nothing extra, and
    "continue this thread" is only followed under comments this pass added."""
    before = len(nodes)
    known = set(nodes)
    progress(f"Checking for comments the first pass missed ({before} loaded so far)")
    qs = "limit=500&depth=100&raw_json=1&sort=new"
    data = _fetch_json(page, f"/comments/{post_id}.json?{qs}", counter)
    pending = []
    _parse_listing(data[1]["data"]["children"], root, nodes, pending)
    _expand_json(page, post_id, "new", qs, counter, progress, root, nodes, pending, skip_deep=known)
    found = len(nodes) - before
    if found:
        log.warning("Thread %s: the sort=new pass found %d comments the first pass missed", post_id, found)
        progress(f"Found {found} comments the first pass missed")


MODHASH_JS = """() => (window.r && r.config && r.config.modhash)
    || (document.querySelector('input[name=uh]') || {}).value || ''"""
MORE_BATCH = 100  # comment ids per morechildren request (Reddit's maximum)

# The POSTed morechildren answers in old.reddit's "api" style: each thing's data is
# {id: "t1_x", parent: "t1_y", content: <escaped HTML of the rendered comment>}, not the usual
# JSON fields. Parse that HTML in the page (it has a DOM) into the usual shape.
API_THINGS_JS = """
(things) => things.map(t => {
  const d = t.data || {};
  const ta = document.createElement('textarea');
  ta.innerHTML = d.content || '';
  const doc = new DOMParser().parseFromString(ta.value, 'text/html');
  const id = (d.id || '').replace(/^t[0-9]_/, '');
  const parent_id = d.parent || d.parent_id || '';
  const thing = doc.querySelector('.thing');
  if (t.kind === 'more' || (thing && thing.classList.contains('morechildren'))) {
    const a = doc.querySelector('.morecomments a');
    const args = a ? [...(a.getAttribute('onclick') || '').matchAll(/'([^']*)'/g)].map(m => m[1]) : [];
    const ids = args.find((x, i) => i >= 2 && /^[a-z0-9]+(,[a-z0-9]+)*$/.test(x)) || '';
    return {kind: 'more', id, parent_id, children: ids ? ids.split(',') : [],
            raw: ids ? '' : (a ? a.outerHTML : ta.value).slice(0, 400)};
  }
  if (!thing || !thing.classList.contains('comment')) return {kind: t.kind, id, parent_id, unknown: true};
  const entry = thing.querySelector(':scope > .entry') || thing.querySelector('.entry');
  const md = entry && entry.querySelector('.usertext-body .md');
  const score = entry && entry.querySelector('.score.unvoted');
  const time = entry && entry.querySelector('time');
  return {kind: 't1', id, parent_id,
          author: thing.getAttribute('data-author') || '[deleted]',
          score: score ? parseInt(score.getAttribute('title')) : null,
          created: time ? Date.parse(time.getAttribute('datetime')) / 1000 : null,
          html: md ? md.innerHTML : null,
          removed: !md && !!entry && entry.innerText.includes('[removed]')};
})
"""


def _normalize_things(page, things):
    """morechildren things as (kind, data) in the usual JSON shape, whichever style Reddit answered in."""
    if not things:
        return []
    if all("body" in t.get("data", {}) or t.get("kind") == "more" and "children" in t.get("data", {})
           for t in things):
        return [(t.get("kind"), t.get("data", {})) for t in things]
    if not any("content" in t.get("data", {}) for t in things):
        raise Blocked("Unrecognized morechildren response")
    parsed = page.evaluate(API_THINGS_JS, things)
    comments = [p for p in parsed if p["kind"] == "t1" and not p.get("unknown")]
    if comments and sum(p["html"] is None and not p["removed"] for p in comments) > len(comments) / 2:
        # Most comments with no readable text: the HTML isn't what we expect. Don't save blanks.
        raise Blocked("Couldn't read comment text in morechildren response")
    out = []
    for p in parsed:
        if p.get("unknown"):
            raise Blocked("Unrecognized comment in morechildren response")
        if p["kind"] == "more":
            if not p["children"]:
                log.warning("morechildren: 'more' stub %s with no readable ids: %s", p["id"], p.get("raw"))
            out.append(("more", {"id": p["id"], "parent_id": p["parent_id"], "children": p["children"]}))
            continue
        if p["html"] is not None:
            body = html_to_md(p["html"]).strip()
        else:
            body = "[removed]" if p["removed"] else "[deleted]"
        out.append(("t1", {"id": p["id"], "parent_id": p["parent_id"], "author": p["author"], "score": p["score"],
                           "created_utc": p["created"], "body": body}))
    return out


def _expand_json(page, post_id, sort, qs, counter, progress, root, nodes, pending, skip_deep=()):
    """Expand every "more" stub. Ids from many stubs share one request (up to MORE_BATCH), which
    keeps a big thread inside Reddit's 100 requests / 10 minutes. morechildren is POSTed with the
    page's modhash, the way old.reddit's own "load more" does it (a plain GET gets HTTP 403)."""
    modhash = page.evaluate(MODHASH_JS) or ""
    while pending:
        stubs, pending[:] = list(pending), []
        want = []  # (comment id, parent node of its stub)
        for parent, more in stubs:
            if more.get("children"):
                want += [(c, parent) for c in more["children"] if c not in nodes]
        seen = set()
        want = [w for w in want if not (w[0] in seen or seen.add(w[0]))]
        for i in range(0, len(want), MORE_BATCH):
            batch = want[i:i + MORE_BATCH]
            fallback = dict(batch)
            left = len(want) - i - len(batch)
            progress(f"Expanding {len(batch)} hidden comments ({len(nodes)} loaded so far"
                     + (f", {left} more waiting)" if left else ")"))
            r = _fetch_json(page, "/api/morechildren", counter, modhash=modhash, form={
                "api_type": "json", "raw_json": "1", "limit_children": "false", "sort": sort,
                "link_id": f"t3_{post_id}", "children": ",".join(c for c, _ in batch), "uh": modhash})
            things = _normalize_things(page, r.get("json", {}).get("data", {}).get("things", []))
            orphans = []
            for kind, d in things:
                pid = d.get("parent_id", "")
                target = root if pid.startswith("t3_") else nodes.get(pid[3:])
                if kind == "more":
                    pending.append((target or fallback.get(pid[3:]) or root, d))
                    continue
                if kind != "t1" or d.get("id") in nodes:
                    continue
                node = new_node(d["id"], d.get("author"), d.get("score"), d.get("created_utc"), d.get("body", ""))
                nodes[d["id"]] = node
                if target is None:
                    orphans.append((pid[3:], node, fallback.get(d["id"])))
                else:
                    target["replies"].append(node)
            for pid, node, fb in orphans:
                (nodes.get(pid) or fb or root)["replies"].append(node)
        for parent, more in stubs:
            if more.get("children"):
                continue
            # "Continue this thread": depth limit reached, fetch the subtree rooted at parent.
            pid = parent["id"]
            if pid.startswith("t3_"):
                if more.get("count"):
                    log.warning("Thread %s: top-level 'more' stub with no comment ids (%s); its comments "
                                "can't be loaded this way: %s", post_id, more.get("count"), str(more)[:300])
                continue
            if pid in skip_deep:
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


LINK_TRIES = 3  # failed clicks before a "load more" link counts as dead (only when Reddit isn't limiting)
LINK_SEL = ".thing.morechildren .morecomments a:visible:not([data-r2m-skip])"
LIMIT_BACKOFF = (30, 60, 120, 240, 300)  # waits when Reddit limits clicks without saying for how long


def _watch_morechildren(page):
    """Record the status and rate-limit headers of the requests old.reddit makes for 'load more'."""
    state = {"seq": 0, "status": None, "remaining": None, "reset": None, "used": None, "seen_headers": False}

    def on_response(r):
        if "/api/morechildren" not in r.url:
            return
        h = r.headers
        state.update(seq=state["seq"] + 1, status=r.status, remaining=_num(h.get("x-ratelimit-remaining")),
                     reset=_num(h.get("retry-after")) or _num(h.get("x-ratelimit-reset")),
                     used=_num(h.get("x-ratelimit-used")))
        if state["remaining"] is not None and not state["seen_headers"]:
            state["seen_headers"] = True
            log.info("Reddit rate limit for 'load more': %s used, %s left, resets in %ss",
                     state["used"], state["remaining"], state["reset"])

    page.on("response", on_response)
    return state, lambda: page.remove_listener("response", on_response)


def _expand_all(page, progress, counter):
    """Click every "load more comments" link.

    Reddit limits how fast these load. When a click fails, the cause is usually that limit,
    not the link: so failures while Reddit is limiting (HTTP 429, or two different links failing
    in a row) never count against a link; the whole loop waits instead (Reddit's reset time when
    it sends one). A link is only given up on after LINK_TRIES failures while other links load.
    """
    state, unwatch = _watch_morechildren(page)
    try:
        _expand_loop(page, progress, counter, state)
    finally:
        unwatch()


def _expand_loop(page, progress, counter, state):
    rounds = fails_in_row = limited_waits = skipped = 0
    while rounds < MAX_REQUESTS:
        _check_deadline(counter)
        fresh = page.locator(LINK_SEL + ":not([data-r2m-defer])")
        n_fresh = fresh.count()
        links = fresh if n_fresh else page.locator(LINK_SEL)
        n = n_fresh or links.count()
        if n == 0:
            break
        rounds += 1
        progress(f"Clicking 'load more comments' ({n} remaining links, round {rounds})")
        seq0 = state["seq"]
        try:
            handle = links.first.element_handle(timeout=10000)
            handle.click(timeout=10000)
        except Exception as e:
            log.info("load-more click issue: %s", e)
            handle = None
        loaded, status = False, None
        if handle:
            # On success old.reddit replaces the whole "more" stub, removing the link. Stop waiting
            # early if Reddit answered with an error.
            end = time.time() + 15
            while time.time() < end:
                try:
                    if handle.evaluate("e => !e.isConnected || !e.closest('.thing.morechildren')"):
                        loaded = True
                        break
                except Exception:
                    loaded = True  # element gone with its frame: replaced
                    break
                if state["seq"] > seq0 and (state["status"] or 0) >= 400:
                    status = state["status"]
                    break
                page.wait_for_timeout(250)
        if loaded:
            fails_in_row = limited_waits = 0
            if state["remaining"] is not None and state["remaining"] < 1 and state["reset"]:
                wait = min(state["reset"], 600) + 1
                counter["throttled"] += 1
                progress(f"Reddit's 'load more' limit is used up; waiting {wait:.0f}s for it to reset")
                _sleep(counter, wait)
            else:
                _sleep(counter, DELAY)
            continue

        fails_in_row += 1
        counter["throttled"] += 1
        limited = status == 429 or state["remaining"] == 0 or (fails_in_row >= 2 and n_fresh > 0)
        if limited:
            wait = state["reset"] if status == 429 and state["reset"] else \
                LIMIT_BACKOFF[min(limited_waits, len(LIMIT_BACKOFF) - 1)]
            limited_waits += 1
            why = f"HTTP {status}" if status else "clicks not loading"
            progress(f"Reddit is rate limiting 'load more' ({why}); waiting {wait:.0f}s, nothing skipped")
            # Let deferred links be tried again once the limit lifts.
            page.evaluate("() => document.querySelectorAll('[data-r2m-defer]').forEach(e => delete e.dataset.r2mDefer)")
            _sleep(counter, wait)
            continue
        if handle:
            # Count it against this link and try the others first; if they load, this one is dead.
            tries = handle.evaluate(
                "(e, max) => { const t = +(e.dataset.r2mTries || 0) + 1; e.dataset.r2mTries = t;"
                " e.dataset.r2mDefer = '1'; if (t >= max) e.dataset.r2mSkip = '1'; return t; }", LINK_TRIES)
            if tries >= LINK_TRIES:
                skipped += 1
                counter["skipped"] += 1
                log.info("Skipping a 'load more' link that failed %d times while others loaded", tries)
        if n_fresh:
            _sleep(counter, DELAY)
        else:
            # Only links that already failed are left: give them a slower last try.
            wait = LIMIT_BACKOFF[min(fails_in_row - 1, len(LIMIT_BACKOFF) - 1)]
            progress(f"Retrying 'load more' links that failed before; waiting {wait:.0f}s")
            _sleep(counter, wait)
    if skipped:
        progress(f"Gave up on {skipped} 'load more' link(s); some comments may be missing")
    if rounds >= MAX_REQUESTS:
        raise Incomplete(f"Hit MAX_REQUESTS ({MAX_REQUESTS}) clicking 'load more' (raise the limit in compose)")


def _goto(page, url):
    """Open a Reddit page; a 429 means retry the whole thread later rather than fail it."""
    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    if resp is not None and resp.status == 429:
        retry = _num(resp.headers.get("retry-after")) or _num(resp.headers.get("x-ratelimit-reset"))
        raise RetryLater("Reddit is rate limiting page loads (HTTP 429)" + (f", reset in {retry:.0f}s" if retry else ""),
                         retry)
    return resp


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
    _goto(page, f"{BASE}/comments/{post_id}/?limit=500&sort={quote(sort)}")
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
        try:
            _goto(page, url)
        except RetryLater as e:
            _mark_partial(counter, str(e), progress)
            break
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
            _goto(page, f"{BASE}/comments/{post_id}/")
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
        reasons.append(f"skipped {counter['skipped']} 'load more' link(s) that kept failing while others loaded")
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
