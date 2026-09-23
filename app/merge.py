"""Merge a fresh fetch of a thread into its saved copy.

- Comments keep the time they were first seen, so the viewer can flag what is new since the last update.
- Comments that were deleted or removed on Reddit keep their saved text and get a status label.
- Comments that vanished from Reddit entirely are re-inserted under their parent with status "gone".
- Changed comment text is marked as edited.
"""

import copy

DELETED_BODIES = {"[deleted]": "deleted", "[removed]": "removed"}


def _walk(nodes, parent_id, out):
    for n in nodes:
        out.append((n, parent_id))
        _walk(n["replies"], n["id"], out)
    return out


def _count(nodes):
    return sum(1 + _count(n["replies"]) for n in nodes)


def stamp_new(comments, ts):
    """First save: every comment was first seen now."""
    for n, _ in _walk(comments, None, []):
        n.setdefault("first_seen", ts)
    return comments


def merge(old, new_comments, new_post, now, same_mode=True):
    """Return (comments, post, stats). `old` is the saved thread dict."""
    fallback_seen = old.get("first_saved") or old.get("saved_at") or now
    old_nodes = {n["id"]: (n, pid) for n, pid in _walk(old.get("comments", []), None, [])}
    merged = copy.deepcopy(new_comments)
    index = {}
    stats = {"new": 0, "kept_deleted": 0, "edited": 0}

    for n, _ in _walk(merged, None, []):
        index[n["id"]] = n
        prev = old_nodes.get(n["id"], (None, None))[0]
        if prev is None:
            n["first_seen"] = now
            stats["new"] += 1
            continue
        n["first_seen"] = prev.get("first_seen", fallback_seen)
        status = DELETED_BODIES.get((n.get("body") or "").strip())
        if prev.get("status"):
            # Already known to be deleted or removed: keep carrying our saved copy.
            n["status"] = prev["status"]
            if status:
                n["body"], n["author"] = prev["body"], prev.get("author") or n.get("author")
        elif status and prev.get("body", "").strip() not in DELETED_BODIES:
            # Deleted or removed since we saved it: keep our copy of the text and author.
            n["body"], n["author"] = prev["body"], prev.get("author") or n.get("author")
            n["status"] = status
            stats["kept_deleted"] += 1
        # Text comparison is only meaningful when both fetches used the same method (JSON vs rendered page).
        if prev.get("edited") or (same_mode and not status and not prev.get("status")
                                  and prev.get("body", "").strip() != (n.get("body") or "").strip()):
            n["edited"] = True
            if not prev.get("edited"):
                stats["edited"] += 1

    # Comments that disappeared entirely: put them back under their parent (parents come first in walk order).
    root = {"replies": merged}
    for n, pid in _walk(old.get("comments", []), None, []):
        if n["id"] in index:
            continue
        keep = {k: v for k, v in n.items() if k != "replies"}
        keep["replies"] = []
        keep.setdefault("first_seen", fallback_seen)
        keep["status"] = keep.get("status") or "gone"
        parent = index.get(pid) if pid else root
        (parent or root)["replies"].append(keep)
        index[n["id"]] = keep
        if not n.get("status"):  # only count comments that disappeared during this update
            stats["kept_deleted"] += 1

    post = dict(new_post)
    old_post = old.get("post", {})
    new_body = (post.get("body") or "").strip()
    if new_body in DELETED_BODIES and (old_post.get("body") or "").strip() not in DELETED_BODIES:
        post["body"] = old_post.get("body", "")
        post["status"] = DELETED_BODIES[new_body]
    elif old_post.get("status"):
        post["status"] = old_post["status"]
        if new_body in DELETED_BODIES or not new_body:
            post["body"] = old_post.get("body", "")

    return merged, post, stats, _count(merged)
