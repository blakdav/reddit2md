"""A saved thread as plain text for a model to read.

Every comment gets a short reference like [c:k3x9a] (its Reddit id), so an
answer can point at the comments it's based on; the page turns those into
links that jump to the comment. Replies are indented under their parent.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

STATUS = {"deleted": "deleted on Reddit, saved text", "removed": "removed by mods, saved text",
          "gone": "no longer on Reddit, saved text"}


def _date(ts, tz):
    return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d") if ts else ""


def walk(nodes, depth=0, parent=None):
    """Every comment, depth first, as (node, depth, parent_id)."""
    for n in nodes:
        yield n, depth, parent
        yield from walk(n.get("replies") or [], depth + 1, n.get("id"))


def comment_line(n, depth, op, tz):
    author = n.get("author") or "[deleted]"
    head = f"[c:{n.get('id')}] u/{author}" + (" (OP)" if op and author == op else "")
    if n.get("score") is not None:
        head += f" · {n['score']} pts"
    if n.get("created"):
        head += f" · {_date(n['created'], tz)}"
    if n.get("status"):
        head += f" · {STATUS.get(n['status'], n['status'])}"
    body = (n.get("body") or "").strip() or "[empty]"
    pad = "  " * min(depth, 10)
    return "\n".join(pad + line for line in (head + "\n" + body).split("\n"))


def header(data, tz="UTC"):
    tz = ZoneInfo(tz or "UTC")
    p = data["post"]
    lines = [f"Title: {p.get('title')}",
             f"r/{p.get('subreddit')} · posted by u/{p.get('author')} (OP) · {p.get('score')} pts · "
             f"{_date(p.get('created'), tz)} · {data.get('count')} comments saved"]
    if p.get("url"):
        lines.append(f"Link: {p['url']}")
    if p.get("body"):
        lines += ["", "Post:", p["body"].strip()]
    return "\n".join(lines)


def blocks(data, tz="UTC"):
    """One text block per top-level comment with its whole reply tree, in saved order."""
    tzi = ZoneInfo(tz or "UTC")
    op = data["post"].get("author")
    op = op if op and op != "[deleted]" else None
    out = []
    for top in data.get("comments") or []:
        out.append("\n\n".join(comment_line(n, d, op, tzi) for n, d, _ in walk([top])))
    return out


def full(data, tz="UTC"):
    return header(data, tz) + "\n\nComments (replies are indented under the comment they answer):\n\n" \
        + "\n\n".join(blocks(data, tz))


def chunks(data, tz, max_chars):
    """Split the comments into parts of at most ~max_chars, on top-level comment boundaries.
    A single comment tree bigger than that is split on comment boundaries inside it."""
    parts, cur = [], ""
    for b in blocks(data, tz):
        pieces = [b] if len(b) <= max_chars else b.split("\n\n")
        for piece in pieces:
            if cur and len(cur) + len(piece) + 2 > max_chars:
                parts.append(cur)
                cur = ""
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        parts.append(cur)
    return parts
