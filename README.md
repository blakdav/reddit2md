# reddit2md

Paste a Reddit thread URL into a web UI and get the whole thread back as Markdown, with every comment expanded. Images and attachments are skipped.

Reddit closed anonymous `.json` access and self-serve API keys in 2026, so this uses headless Chromium (Playwright) running with **your logged-in session**.

## How it works

1. Loads `old.reddit.com` in Chromium with your uploaded Reddit cookies.
2. **JSON mode:** calls Reddit's JSON endpoints with `fetch()` from inside the page, so the real browser sends your cookies. Every "load more comments" stub is expanded through `/api/morechildren`, and every "continue this thread" stub through a subtree request.
3. **DOM mode (fallback):** if the JSON endpoints are blocked, it clicks every "load more comments" link, opens every "continue this thread" page, and converts the rendered HTML.
4. Writes refreshed cookies back to the state file after each run, which keeps the session alive longer.

Nested replies are rendered as nested blockquotes.

## Setup

### 1. Deploy

Copy `docker-compose.yml` to the server, then:

```bash
sudo mkdir -p /opt/docker/reddit2md && sudo chown 1000:1000 /opt/docker/reddit2md
docker compose pull && docker compose up -d --force-recreate
```

Open `http://<server>:8095`. It works logged out, but Reddit blocks anonymous requests more often, so a session is recommended.

### 2. Add your Reddit session (from the web UI)

**Easiest, no extension:**

1. On reddit.com while logged in, press **F12** to open DevTools.
2. Chrome/Edge: **Application** tab > **Cookies** > `https://www.reddit.com`. Firefox: **Storage** tab > **Cookies** > `https://www.reddit.com`.
3. Find the `reddit_session` row, double-click its **Value**, and copy it.
4. Paste it into the reddit2md session field and click **Save**. It checks the login right away.

**Alternative:** export cookies with an extension such as Get cookies.txt LOCALLY, click **Upload cookies**, then delete the exported file.

Accepted formats: Netscape `cookies.txt`, extension JSON exports (Cookie-Editor, EditThisCookie), and Playwright storage state. Only reddit.com cookies are kept. The session is stored at `/opt/docker/reddit2md/reddit_state.json` with mode 600.

**Remove** deletes the saved session. When the session expires, **Check** will say so; export and upload again.

## Settings (environment)

| Variable | Default | Purpose |
|---|---|---|
| `REQUEST_DELAY` | `1.0` | Seconds between Reddit requests |
| `MAX_REQUESTS` | `400` | Safety cap on requests per thread |
| `STATE_FILE` | `/data/reddit_state.json` | Session file path |
| `USER_AGENT` | Chrome on Linux | Browser user agent |

## Notes

- A 1,000 comment thread usually takes 15 to 60 seconds. Multi-thousand comment threads take several minutes.
- There is no authentication on the web UI, and it can replace your Reddit session. Keep it on the internal network.
- Automated access is against Reddit's user agreement. Low volume from a residential IP is low risk, but consider using a secondary account.
