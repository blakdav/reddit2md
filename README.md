# reddit2md

Paste a Reddit thread URL into a web UI and get the whole thread back as Markdown, with every comment expanded. Images and attachments are skipped.

Reddit closed anonymous `.json` access and self-serve API keys in 2026, so this uses headless Chromium (Playwright) running with **your logged-in session**.

## How it works

1. Loads `old.reddit.com` in Chromium with your uploaded Reddit cookies.
2. **JSON mode:** calls Reddit's JSON endpoints with `fetch()` from inside the page, so the real browser sends your cookies. Every "load more comments" stub is expanded through `/api/morechildren`, and every "continue this thread" stub through a subtree request.
3. **DOM mode (fallback):** if the JSON endpoints are blocked, it clicks every "load more comments" link, opens every "continue this thread" page, and converts the rendered HTML.
4. Writes refreshed cookies back to the state file after each run, which keeps the session alive longer.

Nested replies are rendered as nested blockquotes.

## Queue

Conversions go into a queue and run one at a time, oldest first, with a pause between threads (`QUEUE_PAUSE`, 15 s by default; `RATE_LIMIT_PAUSE`, 120 s, after a thread where Reddit was rate limiting). That way a burst of sends from the browser extension doesn't hammer Reddit. You can paste the next URL while one is running. Sending a thread that's already waiting or running returns the existing job instead of adding it twice.

The queue panel at the top of the sidebar shows each job: what it's doing, how many are ahead, or why it failed. Click a job to see its log (the last 40 progress lines, with times). Failed or cancelled jobs can be retried (↻), a running job can be cancelled (✕), and **Clear finished** tidies the list. If a conversion runs out of time (`MAX_SECONDS`) or requests (`MAX_REQUESTS`), or has to skip "load more" links that keep failing, it saves what it collected instead of failing, and the thread is flagged **Partial** (in the sidebar, the reader, and a note at the top of the Markdown saying how many comments were captured and why it stopped). Run **Update** later: comments missing from a partial update are kept as they were (not labelled "no longer on Reddit"), and the flag clears once an update completes. The queue is saved in `/opt/docker/reddit2md/queue.json`, so waiting threads survive a container restart, and a job that was running at the time goes back to the front of the line. The last 50 finished jobs are kept.

## Saved threads

Every converted thread is saved on the server in `/opt/docker/reddit2md/threads/`, one JSON file per thread. Converting the same thread again loads the saved copy instantly without contacting Reddit.

**Update** fetches the thread again and merges it into the saved copy:

- Comments added since the previous update get a **new** badge, and **New only** shows just those (with their parent comments for context).
- Comments deleted by their author or removed by moderators keep the saved text, labelled **deleted on Reddit** or **removed by mods**. Comments that vanished entirely are kept and labelled **no longer on Reddit**. The same applies to the post body.
- Changed comments are labelled **edited**, and scores are updated.

Badges appear only in the web view. Copy and Download give clean Markdown (the kept text is included, without labels). An update fetches the whole thread, so it costs the same number of Reddit requests as the first conversion. The sidebar lists saved threads with title, subreddit, save date, comment count and a link back to Reddit. On phones the sidebar opens from the menu button. Each thread also has its own address (`http://<server>:8095/#t=<id>`) you can bookmark.

**View order** (in the reader toolbar) re-sorts the saved copy by Top, New or Old at every reply level, without re-fetching. *As saved* keeps the order the thread was fetched in. Copy and Download use the order you're viewing. Comments by the thread's author get an **OP** badge (and `(OP)` in the Markdown).

Saved threads are deleted automatically once they are older than the auto-delete setting (default 30 days, based on when they were last fetched), checked hourly.

## AI summaries, questions and search

Optional, using the same setup as Vidscribe: a local model through your Ollama server, Claude through an API key, or both. Set `OLLAMA_URL`/`OLLAMA_MODEL` and/or `ANTHROPIC_API_KEY` in `docker-compose.yml`; options whose settings are left as placeholders are hidden.

- **Summary** (**AI** button in the reader → *Summary*): TL;DR, the main viewpoints and how well supported they are, what OP added, useful details, and notable comments. Saved with the thread. After an **Update** it shows how many comments were added since, with a **Redo** button. Claude reads the whole thread at once; the local model reads big threads in parts and merges its notes.
- **Ask** (*Ask* tab): questions about the open thread. Follow-ups keep the conversation. With the local model, a thread too long for its context is answered from the comments that best match the question (the answer says so).
- **Search** (🔍 in the header, or press Enter in the sidebar filter): every saved comment containing all the words you type, with highlighted snippets. `"Quoted text"` must appear as is. Click a result to jump to that comment.
- **Ask AI across all threads** (🔍 → *Ask AI*): finds the saved comments that best match your question (keyword search), and the AI answers from them, naming the threads. Name the thing you're asking about.

Answers cite comments as links like `u/name`; clicking one jumps to that comment (in another thread, it opens that thread first). Claude shows the cost of each answer: a summary of a typical thread is a few cents; a 1,000-comment thread is around $0.50 on Opus; a question across threads is roughly $0.25 or less. Summaries and chats are stored in `/opt/docker/reddit2md/ai/` and deleted along with their thread.

**Auto-summarize** (Settings): Off, Local AI or Claude. Summarizes each newly converted thread, and again after an **Update** that finds new comments.

**Sharing the GPU with Vidscribe:** mount the same host folder into both containers (e.g. `/opt/docker/gpu` at `/gpu`, writable by UID 1000) and set `GPU_LOCK_FILE=/gpu/lock` in both. Local AI here then waits while Vidscribe transcribes or runs its own local AI, and the other way round; the panel shows "Waiting for the GPU (Vidscribe: transcribing)". Claude never waits. Without it, both apps can load models onto the GPU at once. Summaries unload the local model right away; questions keep it loaded for 10 minutes so follow-ups are quick (Vidscribe unloads it before transcribing, as long as both use the same `OLLAMA_MODEL`).

## Browser extension (Brave / Chrome)

The `extension/` folder has a small extension that saves threads without copy-pasting:

- **Toolbar button** (or **Alt+Shift+R**): sends the thread in the current tab.
- **Right-click a link → Send link to reddit2md**: save a thread straight from a subreddit listing without opening it.
- Sending a thread that's already saved **updates** it (new comments are merged in and marked **new**).
- The icon shows the state of the thread **in the current tab**: **✓** saved, **↓** converting,
  **!** the last attempt failed, nothing if it isn't saved. Hover the icon for details.
- Desktop notifications when you send a thread and when it's saved or fails. Click the "saved" notification to open the thread in reddit2md.

Install:

1. Open `brave://extensions` (or `chrome://extensions`) and turn on **Developer mode**.
2. Click **Load unpacked** and pick the `extension/` folder.
3. The options page opens: enter the address you use for reddit2md (e.g. `https://reddit2md.example.lan`), click **Save & test**, and allow access when asked. Brave asks for that site and Reddit; Reddit access lets the icon check the thread in the current tab, and the extension can't see any other sites.

To update it after a `git pull`, click the reload icon on the extension's card.

## Settings (web UI)

Open **Settings** (gear icon, top right) to change:

- **Auto-delete saved threads:** days to keep saved threads. `0` keeps them forever. Lowering it deletes older threads immediately, after a confirmation.
- **Time zone:** the zone used for comment times, post times and saved dates. Times are stored in UTC, so changing it re-renders every saved thread without re-fetching.

Settings are stored in `/opt/docker/reddit2md/settings.json`. `RETENTION_DAYS` and `TZ` in `docker-compose.yml` only set the defaults before anything is saved in the UI.

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

The pill in the header shows the login state (green with your username, red when logged out or expired) and links to Settings. It is updated after every conversion and never contacts Reddit just to load the page. **Remove session** deletes the saved session.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `REQUEST_DELAY` | `1.0` | Seconds between Reddit requests |
| `MAX_REQUESTS` | `400` | Safety cap on requests per thread |
| `FETCH_TIMEOUT` | `30` | Seconds before a stalled Reddit request is aborted and retried |
| `MAX_SECONDS` | `900` | Hard time limit per conversion, so a stuck job can't hold the browser forever |
| `QUEUE_PAUSE` | `15` | Seconds to wait between queued threads |
| `RATE_LIMIT_PAUSE` | `120` | Seconds to wait after a thread where Reddit was rate limiting |
| `OLLAMA_URL` | (none) | Ollama server for local AI, e.g. `http://host.docker.internal:11434` |
| `OLLAMA_MODEL` | `qwen3:14b` | Local model (must be pulled in Ollama) |
| `OLLAMA_NUM_CTX` | `32768` | Local context window in tokens |
| `ANTHROPIC_API_KEY` | (none) | Claude API key for Claude summaries and questions |
| `CLAUDE_MODEL` | `claude-opus-5-5` | Claude model |
| `AUTO_SUMMARY` | (off) | First-run default for Auto-summarize: `local` or `cloud` |
| `GPU_LOCK_FILE` | (none) | Lock file shared with Vidscribe, e.g. `/gpu/lock` |
| `RETENTION_DAYS` | `30` | Initial auto-delete days, until changed in Settings |
| `TZ` | `UTC` | Initial display time zone, until changed in Settings |
| `THREADS_DIR` | `/data/threads` | Where saved threads are stored |
| `STATE_FILE` | `/data/reddit_state.json` | Session file path |
| `USER_AGENT` | bundled Chromium version, no headless marker | Override the browser user agent |

## Notes

- A 1,000 comment thread usually takes 15 to 60 seconds. Multi-thousand comment threads take several minutes.
- There is no authentication on the web UI, and it can replace your Reddit session. Keep it on the internal network.
- Automated access is against Reddit's user agreement. Low volume from a residential IP is low risk, but consider using a secondary account.
