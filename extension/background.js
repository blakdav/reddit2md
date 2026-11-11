// Send to reddit2md: saves Reddit threads through the server's /api/convert endpoint.
//
// - Toolbar button / Alt+Shift+R: sends the current tab's thread.
// - Right-click a link: "Send link to reddit2md" (save a thread from a subreddit
//   listing without opening it). Right-click a page: sends the page.
// - Sending a thread that's already saved updates it: new comments are merged in
//   and marked "new" in reddit2md.
//
// Feedback:
// - The toolbar icon shows the state of the thread in *this* tab, looked up on the
//   server whenever you switch tabs or threads: ✓ saved, ↓ converting, ! the last
//   attempt failed, nothing otherwise. Needs permission for reddit.com (asked on
//   the options page).
// - Desktop notifications when a thread is sent and when it's saved or fails.
//   Clicking the "saved" notification opens the thread in reddit2md. While
//   anything is converting, an alarm checks every 30 seconds (the background
//   worker sleeps between events, so plain timers aren't reliable).

const MENU_LINK = 'send-link';
const MENU_PAGE = 'send-page';
const REDDIT = [
  'https://www.reddit.com/*', 'https://old.reddit.com/*', 'https://new.reddit.com/*',
  'https://np.reddit.com/*', 'https://m.reddit.com/*'
];

const BADGES = {
  saved: { text: '✓', color: '#2fbf4a', title: 'Saved in reddit2md' },
  running: { text: '↓', color: '#3b82f6', title: 'Converting in reddit2md' },
  error: { text: '!', color: '#ff4747', title: 'reddit2md conversion failed' },
};

chrome.runtime.onInstalled.addListener(async ({ reason }) => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: MENU_LINK, title: 'Send link to reddit2md', contexts: ['link'] });
    chrome.contextMenus.create({ id: MENU_PAGE, title: 'Send this thread to reddit2md', contexts: ['page'] });
  });
  const hasReddit = await chrome.permissions.contains({ origins: REDDIT });
  if (reason === 'install' || !hasReddit) chrome.runtime.openOptionsPage();
});

chrome.action.onClicked.addListener(tab => send(tab.url, tab.id));

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === MENU_LINK) send(info.linkUrl, null);
  else send(info.pageUrl, tab && tab.id);
});

// ---- Per-tab badge ----------------------------------------------------------

// A thread page, a redd.it short link, or a share link (/r/sub/s/xxxx).
function isThread(url) {
  return /^https:\/\/([a-z]+\.)?reddit\.com\/(r\/[^/]+\/)?(comments\/[a-z0-9]+|s\/\w+)/i.test(url || '')
    || /^https:\/\/redd\.it\/[a-z0-9]+/i.test(url || '');
}

function setTabBadge(tabId, badge, detail) {
  if (tabId == null) return;
  if (!badge) {
    chrome.action.setBadgeText({ tabId, text: '' });
    chrome.action.setTitle({ tabId, title: 'Send to reddit2md' });
    return;
  }
  chrome.action.setBadgeText({ tabId, text: badge.text });
  chrome.action.setBadgeBackgroundColor({ tabId, color: badge.color });
  chrome.action.setTitle({ tabId, title: badge.title + (detail ? `\n${detail}` : '') });
}

async function server() {
  const { server } = await chrome.storage.local.get('server');
  return server || null;
}

async function lookup(url) {
  const base = await server();
  if (!base) return null;
  const r = await fetch(`${base}/api/lookup?url=${encodeURIComponent(url)}`);
  return r.ok ? r.json() : null;
}

async function refreshTab(tabId, url) {
  if (!isThread(url)) return setTabBadge(tabId, null);
  try {
    const d = await lookup(url);
    if (!d) return setTabBadge(tabId, null);
    const detail = { saved: `${d.title}\n${d.count} comments · click to update`, running: d.progress, error: d.error }[d.state];
    setTabBadge(tabId, BADGES[d.state], detail);
  } catch {
    setTabBadge(tabId, null);  // server unreachable (off the VPN): show nothing
  }
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  // New Reddit changes threads without reloading the page, so watch URL changes
  if (info.url || info.status === 'complete') refreshTab(tabId, tab.url);
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    refreshTab(tabId, tab.url);
  } catch { /* tab already closed */ }
});

async function refreshActiveTabs() {
  const tabs = await chrome.tabs.query({ active: true });
  await Promise.all(tabs.map(tab => refreshTab(tab.id, tab.url)));
}

// ---- Sending ------------------------------------------------------------------

function notify(title, message, id) {
  chrome.notifications.create(id || '', { type: 'basic', iconUrl: 'icons/icon128.png', title, message: message || '' });
}

async function send(url, tabId) {
  if (!isThread(url)) {
    return notify("Can't send this page", 'Open a Reddit thread (or right-click a thread link) and try again.');
  }
  const base = await server();
  if (!base) {
    chrome.runtime.openOptionsPage();
    return notify('Set up reddit2md first', 'Enter your reddit2md address in the extension options.');
  }
  if (!(await chrome.permissions.contains({ origins: [base + '/*'] }))) {
    chrome.runtime.openOptionsPage();
    return notify('reddit2md access not allowed', 'Click Save & test in the extension options and allow access.');
  }

  setTabBadge(tabId, { text: '…', color: '#666666', title: 'Sending...' });
  try {
    // refresh: an already saved thread gets its new comments merged in
    const r = await fetch(base + '/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, refresh: true })
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok || !body.job) throw new Error(body.error || `Server returned ${r.status}`);

    const { pending = {} } = await chrome.storage.session.get('pending');
    pending[body.job] = { url };
    await chrome.storage.session.set({ pending });
    chrome.alarms.create('poll', { periodInMinutes: 0.5 });

    notify('Sent to reddit2md', 'Converting. You\'ll get a notification when it\'s saved.');
    setTabBadge(tabId, BADGES.running, 'Starting');
  } catch (e) {
    notify("Couldn't send to reddit2md", e.message);
    setTabBadge(tabId, BADGES.error, e.message);
  }
}

// ---- Completion notifications -----------------------------------------------

chrome.alarms.onAlarm.addListener(a => { if (a.name === 'poll') poll(); });

async function poll() {
  const { pending = {} } = await chrome.storage.session.get('pending');
  const base = await server();
  if (!Object.keys(pending).length || !base) return chrome.alarms.clear('poll');
  for (const jid of Object.keys(pending)) {
    let job;
    try {
      const r = await fetch(`${base}/api/job/${jid}`);
      if (r.status === 404) { delete pending[jid]; continue; }  // server restarted; job is gone
      job = await r.json();
    } catch {
      continue;  // unreachable for now; try again on the next alarm
    }
    if (job.status === 'done') {
      const u = job.update;
      const what = u ? `Updated: ${u.new} new comment${u.new === 1 ? '' : 's'}` : `Saved: ${job.count} comments`;
      notify(job.title || 'Saved in reddit2md', `${what}. Click to open.`, `open:${job.id}`);
      delete pending[jid];
    } else if (job.status === 'error') {
      notify('reddit2md conversion failed', job.error);
      delete pending[jid];
    }
  }
  await chrome.storage.session.set({ pending });
  if (!Object.keys(pending).length) chrome.alarms.clear('poll');
  await refreshActiveTabs();
}

chrome.notifications.onClicked.addListener(async id => {
  if (!id.startsWith('open:')) return;
  const base = await server();
  if (base) chrome.tabs.create({ url: `${base}/#t=${id.slice(5)}` });
  chrome.notifications.clear(id);
});
