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
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36",
)

POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)", re.I)
SHORT_RE = re.compile(r"redd\.it/([a-z0-9]+)", re.I)


class ScrapeError(Exception):
    pass


class Blocked(Exception):
    pass


def new_node(cid, author, score, created, body):
    return {"id": cid, "author": author, "score": score, "created": created,
            "body": body, "replies": []}


# ---------------------------------------------------------------- browser helpers

def _launch(p):
    browser = p.chromium.launch(
        headless=True,
        args=["--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    )
    kwargs = {"user_agent": USER_AGENT, "viewport": {"width": 1280, "height": 900}}
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


def _fetch_json(page, path, counter):
    counter["n"] += 1
    if counter["n"] > MAX_REQUESTS:
        raise ScrapeError(f"Hit MAX_REQUESTS ({MAX_REQUESTS}); thread is huge. Raise the limit in compose.")
    for attempt in range(4):
        res = page.evaluate(
            """async (p) => {
                const r = await fetch(p, {credentials: 'include', headers: {'Accept': 'application/json'}});
                const t = await r.text();
                return {status: r.status, text: t};
            }""",
            path,
        )
        status = res["status"]
        if status == 200:
            time.sleep(DELAY)
            try:
                return json.loads(res["text"])
            except ValueError:
                raise Blocked("Non-JSON response")
        if status == 429 or status >= 500:
            wait = DELAY * (2 ** (attempt + 2))
            log.info("HTTP %s on %s, retrying in %.0fs", status, path, wait)
            time.sleep(wait)
            continue
        raise Blocked(f"HTTP {status}")
    raise ScrapeError(f"Gave up after repeated rate limiting on {path}")


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
    return post, root["replies"], len(nodes)


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


def _expand_all(page, progress):
    rounds = 0
    while rounds < MAX_REQUESTS:
        links = page.locator(".thing.morechildren .morecomments a:visible")
        n = links.count()
        if n == 0:
            return
        rounds += 1
        progress(f"Clicking 'load more comments' ({n} remaining links, round {rounds})")
        try:
            links.first.click(timeout=10000)
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception as e:
            log.info("load-more click issue: %s", e)
        time.sleep(DELAY)


def _to_nodes(items):
    out = []
    for it in items:
        body = it["html"] if it["html"] in ("[removed]", "[deleted]") else html_to_md(it["html"]).strip()
        node = new_node(it["id"], it["author"], it["score"], it["created"], body)
        node["deep"] = it.get("deep")
        node["replies"] = _to_nodes(it["replies"])
        out.append(node)
    return out


def scrape_dom(page, post_id, sort, progress):
    page.goto(f"{BASE}/comments/{post_id}/?limit=500&sort={quote(sort)}", wait_until="domcontentloaded", timeout=60000)
    if page.locator("#siteTable > .thing.link").count() == 0:
        raise ScrapeError("Thread page did not render. Session may be logged out or blocked.")
    raw = page.evaluate(POST_JS)
    post = {k: v for k, v in raw.items() if k != "html"}
    post["body"] = html_to_md(raw["html"]).strip() if raw["html"] else ""
    _expand_all(page, progress)
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
    while queue:
        node = queue.pop(0)
        url = node.pop("deep")
        if url in seen:
            continue
        seen.add(url)
        progress(f"Opening deep thread under comment {node['id']}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(DELAY)
        _expand_all(page, progress)
        sub = _to_nodes(page.evaluate(EXTRACT_JS, ".commentarea > .sitetable"))
        # The deep page's root is the parent comment itself.
        root = next((s for s in sub if s["id"] == node["id"]), sub[0] if sub else None)
        if root:
            node["replies"].extend(root["replies"])
            collect(root["replies"])

    def count(nodes):
        return sum(1 + count(n["replies"]) for n in nodes)

    return post, comments, count(comments)


# ---------------------------------------------------------------- entry points

def scrape(url, sort="confidence", mode="auto", progress=lambda msg: None):
    with sync_playwright() as p:
        browser, context = _launch(p)
        try:
            page = context.new_page()
            post_id = resolve_post_id(page, url)
            progress(f"Thread ID {post_id}")
            page.goto(f"{BASE}/comments/{post_id}/", wait_until="domcontentloaded", timeout=60000)
            used = mode
            if mode in ("auto", "json"):
                try:
                    post, comments, n = scrape_json(page, post_id, sort, {"n": 0}, progress)
                    used = "json"
                except Blocked as e:
                    if mode == "json":
                        raise ScrapeError(f"JSON endpoints blocked ({e}). Try DOM mode or refresh the session.")
                    progress(f"JSON blocked ({e}), falling back to DOM mode")
                    post, comments, n = scrape_dom(page, post_id, sort, progress)
                    used = "dom"
            else:
                post, comments, n = scrape_dom(page, post_id, sort, progress)
            _save_state(context)
            return {"post": post, "comments": comments, "count": n, "mode": used}
        finally:
            context.close()
            browser.close()


def session_status():
    """Report whether a session file exists and whether Reddit sees us as logged in."""
    info = {"state_file": os.path.exists(STATE_FILE), "logged_in": False, "user": None, "json_ok": None}
    with sync_playwright() as p:
        browser, context = _launch(p)
        try:
            page = context.new_page()
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=60000)
            user = page.locator("#header-bottom-right .user a").first
            if user.count() and "login" not in (user.get_attribute("href") or ""):
                info["logged_in"] = True
                info["user"] = user.inner_text().strip()
            try:
                _fetch_json(page, "/api/me.json", {"n": 0})
                info["json_ok"] = True
            except Blocked:
                info["json_ok"] = False
            _save_state(context)
        finally:
            context.close()
            browser.close()
    return info
