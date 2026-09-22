"""Turn the scraped thread structure into Markdown."""

from datetime import datetime, timezone

from markdown_it import MarkdownIt

# html=False escapes any raw HTML in comments, so the rendered view cannot inject markup.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).enable(["table", "strikethrough"])


def _date(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _quote(text, depth):
    if depth == 0:
        return text
    prefix = "> " * depth
    return "\n".join((prefix + line).rstrip() for line in text.split("\n"))


def _comment(node, depth, scores, out):
    meta = f"**u/{node['author'] or '[deleted]'}**"
    if scores and node.get("score") is not None:
        meta += f" · {node['score']} pts"
    if node.get("created"):
        meta += f" · {_date(node['created'])}"
    body = (node.get("body") or "").strip() or "*[empty]*"
    out.append(_quote(meta + "\n\n" + body, depth))
    for r in node["replies"]:
        out.append(_quote("", depth).rstrip())
        _comment(r, depth + 1, scores, out)


def render(result, scores=True):
    post = result["post"]
    lines = [f"# {post['title']}", ""]
    meta = [f"r/{post['subreddit']}", f"u/{post['author']}"]
    if scores and post.get("score") is not None:
        meta.append(f"{post['score']} pts")
    if post.get("created"):
        meta.append(_date(post["created"]))
    meta.append(f"{result['count']} comments captured")
    lines.append(" · ".join(meta))
    lines.append("")
    lines.append(f"Source: {post['permalink']}")
    if post.get("url"):
        lines.append(f"Link: {post['url']}")
    lines.append("")
    if post.get("body"):
        lines += [post["body"].strip(), ""]
    lines += ["---", "", "## Comments", ""]

    for top in result["comments"]:
        out = []
        _comment(top, 0, scores, out)
        lines.append("\n".join(out))
        lines += ["", "---", ""]
    return "\n".join(lines).rstrip() + "\n"


def to_html(markdown_text):
    html = _md.render(markdown_text)
    return html.replace("<a href=", '<a target="_blank" rel="noopener noreferrer" href=')
