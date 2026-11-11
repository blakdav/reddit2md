const input = document.getElementById('server');
const result = document.getElementById('result');
const REDDIT = [
  'https://www.reddit.com/*', 'https://old.reddit.com/*', 'https://new.reddit.com/*',
  'https://np.reddit.com/*', 'https://m.reddit.com/*'
];

chrome.storage.local.get('server').then(({ server }) => {
  if (server) input.value = server;
});

function show(cls, msg) {
  result.className = cls;
  result.textContent = msg;
}

function normalize(raw) {
  let s = raw.trim();
  if (!s) return '';
  if (!/^https?:\/\//i.test(s)) s = 'https://' + s;
  return new URL(s).origin + new URL(s).pathname.replace(/\/+$/, '');
}

async function save() {
  let server;
  try {
    server = normalize(input.value);
  } catch {
    return show('err', 'That doesn\'t look like a valid address.');
  }
  if (!server) return show('err', 'Enter an address.');
  input.value = server;

  // Must be called directly from the click handler (user gesture).
  // The server, plus Reddit so the icon can show whether the thread in a tab is
  // already saved (it reads Reddit tab addresses, nothing else).
  const granted = await chrome.permissions.request({ origins: [new URL(server).origin + '/*', ...REDDIT] });
  if (!granted) return show('err', 'Access was not allowed, so the extension can\'t reach reddit2md.');

  await chrome.storage.local.set({ server });
  show('', 'Saved. Testing connection...');
  try {
    const r = await fetch(server + '/api/threads');
    if (!r.ok) throw new Error(`Server returned ${r.status}`);
    const d = await r.json();
    if (!Array.isArray(d.threads)) throw new Error('Unexpected response. Is this the reddit2md address?');
    show('ok', `✓ Connected. ${d.threads.length} saved thread(s).`);
  } catch (e) {
    show('err', `Saved, but the test failed: ${e.message}`);
  }
}

document.getElementById('save').addEventListener('click', save);
input.addEventListener('keydown', e => { if (e.key === 'Enter') save(); });
