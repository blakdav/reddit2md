"""Turn the saved thread structure into Markdown (clean for copy/download) or HTML (with badges for the web view)."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt

# html=False escapes any raw HTML in comments, so the rendered view cannot inject markup.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])

# Badge markers use Unicode private-use characters, stripped from comment text first, so comment text can never fake one.
B0, B1 = "", ""
BADGES = {
    "op": "OP",
    "new": "new",
    "edited": "edited",
    "deleted": "deleted on Reddit",
    "removed": "removed by mods",
    "gone": "no longer on Reddit",
}
_BADGE_RE = re.compile(B0 + "([a-z]+)" + B1)
# Anchor markers: each comment in the web view gets an id (c-<comment id>), so
# AI answers and search results can link to it. Same private-use trick.
A0, A1 = "\ue002", "\ue003"
_ANCHOR_RE = re.compile(A0 + "([a-z0-9]+)" + A1)


def _clean(text):
    for ch in (B0, B1, A0, A1):
        text = text.replace(ch, "")
    return text


def _date(ts, tz):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M %Z")


def _quote(text, depth):
    if depth == 0:
        return text
    prefix = "> " * depth
    return "\n".join((prefix + line).rstrip() for line in text.split("\n"))


def _is_new(node, since):
    return since is not None and (node.get("first_seen") or 0) > since


def _prune_new(nodes, since):
    """Keep new comments plus the parent chain above them, for context."""
    out = []
    for n in nodes:
        kids = _prune_new(n["replies"], since)
        if kids or _is_new(n, since):
            out.append({**n, "replies": kids})
    return out


# Re-sort the saved copy for display. "saved" keeps the order Reddit returned when fetched.
ORDERS = {
    "top": (lambda n: n.get("score") if n.get("score") is not None else float("-inf"), True),
    "new": (lambda n: n.get("created") or 0, True),
    "old": (lambda n: n.get("created") or float("inf"), False),
}


def _sorted(nodes, order):
    key, rev = ORDERS[order]
    return [{**n, "replies": _sorted(n["replies"], order)} for n in sorted(nodes, key=key, reverse=rev)]


def _count(nodes):
    return sum(1 + _count(n["replies"]) for n in nodes)


def _comment(node, depth, o, out):
    author = node.get("author") or "[deleted]"
    is_op = o["op"] and author == o["op"]
    meta = f"**u/{author}**"
    if is_op:
        meta += f" {B0}op{B1}" if o["web"] else " (OP)"
    if o["scores"] and node.get("score") is not None:
        meta += f" · {node['score']} pts"
    if node.get("created"):
        meta += f" · {_date(node['created'], o['tz'])}"
    body = (node.get("body") or "").strip() or "*[empty]*"
    if o["web"]:
        body = _clean(body)
        if node.get("id"):
            meta = f"{A0}{str(node['id']).lower()}{A1}" + meta
        for key in (["new"] if _is_new(node, o["since"]) else []) + ([node["status"]] if node.get("status") else []) \
                + (["edited"] if node.get("edited") else []):
            meta += f" {B0}{key}{B1}"
    out.append(_quote(meta + "\n\n" + body, depth))
    for r in node["replies"]:
        out.append(_quote("", depth).rstrip())
        _comment(r, depth + 1, o, out)


def render(data, scores=True, tz="UTC", web=False, new_only=False, order="saved"):
    """data: saved thread dict (post, comments, count, prev_fetch)."""
    o = {"scores": scores, "tz": ZoneInfo(tz or "UTC"), "web": web, "since": data.get("prev_fetch"),
         # Authors that can't identify OP: deleted accounts all share the same placeholder name.
         "op": data["post"].get("author") if data["post"].get("author") not in (None, "", "[deleted]") else None}
    post = data["post"]
    comments = data["comments"]
    if new_only:
        comments = _prune_new(comments, o["since"])
    if order in ORDERS:
        comments = _sorted(comments, order)

    lines = [f"# {post['title']}", ""]
    meta = [f"r/{post['subreddit']}", f"u/{post['author']}"]
    if scores and post.get("score") is not None:
        meta.append(f"{post['score']} pts")
    if post.get("created"):
        meta.append(_date(post["created"], o["tz"]))
    if new_only:
        new_n = sum(1 for _ in _iter_new(comments, o["since"]))
        meta.append(f"showing {new_n} new of {data['count']} comments")
    else:
        meta.append(f"{data['count']} comments captured")
    lines.append(" · ".join(meta))
    lines.append("")
    lines.append(f"Source: {post['permalink']}")
    if post.get("url"):
        lines.append(f"Link: {post['url']}")
    lines.append("")
    if post.get("body"):
        body = post["body"].strip()
        if web:
            body = _clean(body)
            if post.get("status"):
                body = f"{B0}{post['status']}{B1}\n\n" + body
        lines += [body, ""]
    lines += ["---", "", "## New comments" if new_only else "## Comments", ""]

    if new_only and not comments:
        lines += ["*No new comments since the previous update.*", ""]
    for top in comments:
        out = []
        _comment(top, 0, o, out)
        lines.append("\n".join(out))
        lines += ["", "---", ""]
    return "\n".join(lines).rstrip() + "\n"


def _iter_new(nodes, since):
    for n in nodes:
        if _is_new(n, since):
            yield n
        yield from _iter_new(n["replies"], since)


def count_new(data):
    return sum(1 for _ in _iter_new(data["comments"], data.get("prev_fetch")))


def to_html(markdown_text):
    html = _md.render(markdown_text)
    html = html.replace("<a href=", '<a target="_blank" rel="noopener noreferrer" href=')
    html = _ANCHOR_RE.sub(lambda m: f'<span class="anchor" id="c-{m.group(1)}"></span>', html)
    return _BADGE_RE.sub(lambda m: f'<span class="badge b-{m.group(1)}">{BADGES.get(m.group(1), m.group(1))}</span>', html)
