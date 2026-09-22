"""Convert uploaded cookie exports into a Playwright storage-state file.

Accepted formats:
  1. Playwright storage state: {"cookies": [...], "origins": [...]}
  2. Browser extension JSON export (Cookie-Editor, EditThisCookie): [{name, value, domain, ...}, ...]
  3. Netscape cookies.txt (Get cookies.txt LOCALLY and similar)
Only reddit.com cookies are kept.
"""

import json

SAMESITE = {"no_restriction": "None", "none": "None", "lax": "Lax", "strict": "Strict"}
LOGIN_COOKIES = {"reddit_session", "token_v2"}


class CookieError(Exception):
    pass


def _is_reddit(domain):
    d = (domain or "").lstrip(".").lower()
    return d == "reddit.com" or d.endswith(".reddit.com")


def _from_extension(items):
    out = []
    for c in items:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        exp = c.get("expirationDate", c.get("expires"))
        secure = bool(c.get("secure", False))
        same = SAMESITE.get(str(c.get("sameSite") or "").lower(), "Lax")
        if same == "None" and not secure:
            same = "Lax"  # Chromium rejects SameSite=None without Secure
        out.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "expires": float(exp) if exp not in (None, "") and not c.get("session") else -1,
            "httpOnly": bool(c.get("httpOnly", False)),
            "secure": secure,
            "sameSite": same,
        })
    return out


def _from_netscape(text):
    out = []
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, expires, name, value = parts[:7]
        out.append({
            "name": name, "value": value.rstrip("\r\n"), "domain": domain, "path": path,
            "expires": float(expires) if expires.strip() not in ("", "0") else -1,
            "httpOnly": http_only, "secure": secure.upper() == "TRUE", "sameSite": "Lax",
        })
    return out


def to_storage_state(raw: bytes) -> tuple[dict, int]:
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        raise CookieError("File is empty")
    try:
        data = json.loads(text)
    except ValueError:
        data = None

    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        cookies = _from_extension(data["cookies"])
    elif isinstance(data, list):
        cookies = _from_extension(data)
    elif data is None:
        cookies = _from_netscape(text)
    else:
        raise CookieError("Unrecognized format. Upload a cookie JSON export or cookies.txt file.")

    cookies = [c for c in cookies if _is_reddit(c["domain"])]
    if not cookies:
        raise CookieError("No reddit.com cookies found in the file")
    if not LOGIN_COOKIES & {c["name"] for c in cookies}:
        raise CookieError("No Reddit login cookie (reddit_session or token_v2) found. Export while logged in.")
    return {"cookies": cookies, "origins": []}, len(cookies)


def from_values(values: dict) -> tuple[dict, int]:
    """Build a storage state from cookie values pasted from browser DevTools."""
    cookies = []
    for name in ("reddit_session", "token_v2"):
        v = (values.get(name) or "").strip().strip('"')
        if v:
            cookies.append({"name": name, "value": v, "domain": ".reddit.com", "path": "/",
                            "expires": -1, "httpOnly": True, "secure": True, "sameSite": "Lax"})
    if not cookies:
        raise CookieError("Paste at least the reddit_session value")
    return {"cookies": cookies, "origins": []}, len(cookies)
