# reddit2md

Paste a Reddit thread URL into a web UI and get the whole thread back as Markdown, with every comment expanded. Images and attachments are skipped.

Reddit closed anonymous `.json` access and self-serve API keys in 2026, so this uses headless Chromium (Playwright) running with **your logged-in session**.

## How it works

1. Loads `old.reddit.com` in Chromium using a saved Playwright session (`reddit_state.json`).
2. **JSON mode:** calls Reddit's JSON endpoints with `fetch()` from inside the page, so the real browser sends your cookies. Every "load more comments" stub is expanded through `/api/morechildren`, and every "continue this thread" stub through a subtree request.
3. **DOM mode (fallback):** if the JSON endpoints are blocked, it clicks every "load more comments" link, opens every "continue this thread" page, and converts the rendered HTML.
4. Writes refreshed cookies back to the state file after each run, which keeps the session alive longer.

Nested replies are rendered as nested blockquotes.

## Setup

### 1. Create the session file (one time, on a machine with a display)

```bash
python3 -m venv /tmp/pw && /tmp/pw/bin/pip install playwright==1.63.0 && /tmp/pw/bin/playwright install chromium
/tmp/pw/bin/playwright codegen --save-storage=reddit_state.json https://old.reddit.com/login
```

Log in in the window that opens, then close it. `reddit_state.json` holds your Reddit cookies, so treat it like a password.

### 2. Put it on the server

```bash
ssh <server> 'sudo mkdir -p /opt/docker/reddit2md'
scp reddit_state.json <server>:/tmp/ && ssh <server> 'sudo mv /tmp/reddit_state.json /opt/docker/reddit2md/ && sudo chown -R 1000:1000 /opt/docker/reddit2md'
```

The container runs as UID 1000 and needs write access so it can save refreshed cookies.

### 3. Clean up the desktop side

```bash
rm -rf /tmp/pw ~/.cache/ms-playwright reddit_state.json
```

### 4. Deploy

Copy `docker-compose.yml` to the server, then:

```bash
docker compose pull && docker compose up -d --force-recreate
```

Open `http://<server>:8095` and click **Check session** to confirm you are logged in and whether the JSON endpoints work.

## Settings (environment)

| Variable | Default | Purpose |
|---|---|---|
| `REQUEST_DELAY` | `1.0` | Seconds between Reddit requests |
| `MAX_REQUESTS` | `400` | Safety cap on requests per thread |
| `STATE_FILE` | `/data/reddit_state.json` | Session file path |
| `USER_AGENT` | Chrome on Linux | Browser user agent |

## Notes

- A 1,000 comment thread usually takes 15 to 60 seconds. Multi-thousand comment threads take several minutes.
- When the session expires, **Check session** will say so. Repeat steps 1 to 3.
- There is no authentication on the web UI. Keep it on the internal network.
- Automated access is against Reddit's user agreement. Low volume from a residential IP is low risk, but consider using a secondary account.
