const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const load = (name) => fs.readFileSync(path.join(root, name), 'utf8');

function article(id, links = []) {
  const classes = new Set();
  const item = {
    id, banners: [], links,
    getAttribute(name) { return name === 'data-tweet-id' ? this.id : null; },
    classList: { contains: x => classes.has(x), add: x => classes.add(x), remove: x => classes.delete(x) },
    querySelectorAll() { return this.links; },
    prepend(node) { this.banners.push(node); node.parent = this; },
  };
  return item;
}
function anchor(owner, href, { time = true, quoted = false } = {}) {
  return {
    querySelector: () => time ? {} : null,
    closest: selector => selector === 'article' ? owner : quoted ? {} : null,
    getAttribute: () => href,
  };
}
function content(articles) {
  const observers = [];
  const handlers = [];
  const context = {
    URL, window: { location: { href: 'https://x.com/home' } },
    chrome: { runtime: { onMessage: { addListener: f => handlers.push(f) } } },
    setTimeout: () => 1, clearTimeout() {},
    MutationObserver: class {
      constructor(callback) { this.callback = callback; observers.push(this); }
      observe() {} disconnect() { this.disconnected = true; }
    },
    document: {
      querySelectorAll: selector => { assert.equal(selector, 'article'); return articles; },
      createElement: () => ({ children: [], append(...children) { this.children.push(...children); }, remove() { this.removed = true; } }),
    },
  };
  vm.createContext(context); vm.runInContext(load('content.js'), context);
  return { context, observers, handlers };
}

test('absent tweet and partial ID never mark another article', () => {
  const other = article('123456'); const { context } = content([other]);
  assert.equal(context.highlightTweet('123', 'fixture'), false);
  assert.equal(context.highlightTweet('999', 'fixture'), false);
  assert.equal(other.banners.length, 0);
});

test('exact permalink matches own timestamp, excluding quoted tweet links', () => {
  const own = article(null);
  own.links = [anchor(own, '/quoted/status/999', { quoted: true }), anchor(own, '/writer/status/123456/photo/1')];
  const { context } = content([own]);
  assert.equal(context.highlightTweet('999', 'quoted'), false);
  assert.equal(context.highlightTweet('123456', 'own'), true);
  assert.equal(own.banners.length, 1);
});

test('foreign domains and status-ID substrings do not identify an article', () => {
  const item = article(null);
  item.links = [anchor(item, 'https://evil.example/writer/status/123'), anchor(item, '/writer/status/12345')];
  const { context } = content([item]);
  assert.equal(context.highlightTweet('123', 'fixture'), false);
});

test('repeated alert never falls through to a different article', () => {
  const target = article('123'), unrelated = article('999'); const { context } = content([target, unrelated]);
  assert.equal(context.highlightTweet('123', 'first'), true);
  assert.equal(context.highlightTweet('123', 'second'), false);
  assert.equal(target.banners.length, 1); assert.equal(unrelated.banners.length, 0);
});

test('recycled article loses the previous tweet label', () => {
  const target = article('123'); const { context, observers } = content([target]);
  context.highlightTweet('123', 'fixture'); target.id = '456'; observers[0].callback();
  assert.equal(target.classList.contains('shortmox-highlight'), false);
  assert.equal(target.banners[0].removed, true); assert.equal(observers[0].disconnected, true);
});

test('alert data uses text nodes and missing confidence is not fabricated', () => {
  const target = article('123'); const { context } = content([target]);
  context.highlightTweet('123', '<img src=x onerror=alert(1)>', undefined, '<b>A1</b>');
  const spans = target.banners[0].children;
  assert.equal(spans[0].textContent, 'SHORTMOX RADAR // <b>A1</b>');
  assert.equal(spans[1].textContent, '<img src=x onerror=alert(1)>');
  assert.equal(spans[2].textContent, 'CONFIDENCE: not specified');
  assert.ok(spans.every(span => !('innerHTML' in span)));
});

test('content script ignores Telegram IDs and malformed selector input', () => {
  const target = article('123'); const { context, handlers } = content([target]);
  handlers[0]({ type: 'SHORTMOX_ALERT', alert: { source_platform: 'TELEGRAM', tweet_id: '123' } });
  assert.equal(context.highlightTweet('123"]', 'fixture'), false);
  assert.equal(target.banners.length, 0);
});

function popup(alerts) {
  const list = { innerHTML: '' }; const opens = []; const clicks = [];
  const elements = { 'alert-list': list, 'clear-btn': { addEventListener() {} }, 'connection-status': { classList: { toggle() {} } } };
  const context = {
    document: {
      getElementById: id => elements[id],
      createElement: () => ({ textContent: '', get innerHTML() { return this.textContent; } }),
      querySelectorAll: () => alerts.map((_, index) => ({ dataset: { index: String(index) }, addEventListener: (_, fn) => { clicks[index] = fn; } })),
    },
    chrome: {
      storage: { local: { async get() { return { shortmox_alerts: alerts, shortmox_dashboard_connected: true }; } }, onChanged: { addListener() {} } },
      tabs: { create: item => opens.push(item.url) },
    },
  };
  vm.createContext(context); vm.runInContext(load('popup.js'), context);
  return { context, list, opens, clicks };
}

test('popup uses source_platform and opens Telegram channel, not a fake X status', async () => {
  const alert = { source_platform: 'TELEGRAM', tweet_id: 'telegram:-100123:55', username: 'tg_fixture', text: 'fixture' };
  const { list, opens, clicks } = popup([alert]);
  await new Promise(setImmediate); clicks[0]();
  assert.match(list.innerHTML, /TELEGRAM/);
  assert.match(list.innerHTML, /UNASSESSED/);
  assert.deepEqual(opens, ['https://t.me/fixture']);
});

test('popup X routing uses numeric ID, unknown/darkweb sources do not fall back to X', () => {
  const { context } = popup([]);
  assert.equal(context.alertSourceUrl({ source_platform: 'X_TWITTER', tweet_id: '123' }), 'https://x.com/i/web/status/123');
  assert.equal(context.alertSourceUrl({ source_platform: 'DARKWEB', tweet_id: '123' }), null);
  assert.equal(context.alertSourceUrl({ source_platform: 'TELEGRAM', username: 'tg_UnknownTelegramUser' }), null);
  assert.equal(context.alertSourceUrl({ source_platform: 'X_TWITTER', tweet_id: '123/../other' }), null);
});

test('all manifest scripts/popups/icons exist; legacy has no background or privileges', () => {
  for (const dir of [root, path.resolve(root, '../chrome_extension')]) {
    const manifest = JSON.parse(fs.readFileSync(path.join(dir, 'manifest.json')));
    const references = [manifest.background?.service_worker, manifest.action?.default_popup,
      ...Object.values(manifest.icons || {}), ...Object.values(manifest.action?.default_icon || {}),
      ...(manifest.content_scripts || []).flatMap(rule => [...(rule.js || []), ...(rule.css || [])])].filter(Boolean);
    for (const filename of references) assert.ok(fs.existsSync(path.join(dir, filename)), filename);
    for (const filename of Object.values(manifest.icons || {})) {
      const icon = fs.readFileSync(path.join(dir, filename));
      assert.equal(icon.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
      assert.equal(icon.readUInt32BE(16), Number(path.basename(filename).match(/\d+/)[0]));
    }
  }
  const legacy = JSON.parse(fs.readFileSync(path.resolve(root, '../chrome_extension/manifest.json')));
  assert.equal(legacy.background, undefined); assert.equal(legacy.permissions, undefined); assert.equal(legacy.host_permissions, undefined);
});

test('dashboard bridge forwards the existing event contract without handling credentials', () => {
  const listeners = {}, messages = [];
  const context = { window: { addEventListener: (name, fn) => { listeners[name] = fn; } }, chrome: { runtime: { sendMessage: async message => { messages.push(message); } } } };
  vm.createContext(context); vm.runInContext(load('dashboard_bridge.js'), context);
  const alert = { schema_version: 1, event: 'ANOMALY_DETECTED', event_id: 'evt-1', tweet_id: '123', source_platform: 'X_TWITTER' };
  listeners['shortmox-radar-alert']({ detail: alert });
  listeners['shortmox-radar-connection']({ detail: true });
  assert.equal(messages[0].alert, alert); assert.equal(messages[1].connected, true);
  assert.doesNotMatch(load('background.js') + load('dashboard_bridge.js'), /new WebSocket|RADAR_INGEST_API_KEY|cookies\./);
});

test('background retains unmatched alerts and sends only X events to X tabs', async () => {
  const data = { shortmox_alerts: [] }, notifications = [], sent = [];
  let listener;
  const chrome = {
    runtime: { onMessage: { addListener: fn => { listener = fn; } } },
    notifications: { create: (...args) => notifications.push(args) },
    storage: { local: { async get() { return { ...data }; }, async set(value) { Object.assign(data, value); } } },
    tabs: { query: (_, callback) => callback([{ id: 7 }]), sendMessage: async (id, message) => { sent.push({ id, message }); } },
  };
  const context = { chrome }; vm.createContext(context); vm.runInContext(load('background.js'), context);
  const xAlert = { schema_version: 1, event: 'ANOMALY_DETECTED', event_id: 'x-1', source_platform: 'X_TWITTER', tweet_id: '999', username: 'fixture' };
  listener({ type: 'SHORTMOX_ALERT', alert: xAlert }); await new Promise(setImmediate);
  const tgAlert = { ...xAlert, event_id: 'tg-1', source_platform: 'TELEGRAM', tweet_id: 'telegram:-100:1' };
  listener({ type: 'SHORTMOX_ALERT', alert: tgAlert }); await new Promise(setImmediate);
  listener({ type: 'SHORTMOX_ALERT', alert: xAlert }); await new Promise(setImmediate);
  assert.equal(data.shortmox_alerts.length, 2);
  assert.equal(notifications.length, 2);
  assert.equal(notifications[0][1].title, 'RADAR SIGNAL');
  assert.equal(sent.length, 1); assert.equal(sent[0].message.alert, xAlert);
});

test('connection status tracks all dashboard tabs and clears after closing or restart', async () => {
  const data = {};
  let listener, removed, updated, startup, installed;
  const chrome = {
    runtime: {
      onMessage: { addListener: fn => { listener = fn; } },
      onStartup: { addListener: fn => { startup = fn; } },
      onInstalled: { addListener: fn => { installed = fn; } },
    },
    tabs: {
      onRemoved: { addListener: fn => { removed = fn; } },
      onUpdated: { addListener: fn => { updated = fn; } },
    },
    storage: { local: {
      async get(defaults) { return { ...defaults, ...data }; },
      async set(value) { Object.assign(data, value); },
    } },
  };
  const context = { chrome }; vm.createContext(context); vm.runInContext(load('background.js'), context);
  const connection = (id, connected) => listener({ type: 'SHORTMOX_CONNECTION_STATUS', connected }, { tab: { id } });
  connection(1, true); connection(2, true); await new Promise(setImmediate);
  assert.equal(data.shortmox_dashboard_connected, true);
  connection(1, false); await new Promise(setImmediate);
  assert.equal(data.shortmox_dashboard_connected, true);
  updated(2, { status: 'loading' }); await new Promise(setImmediate);
  assert.equal(data.shortmox_dashboard_connected, false);
  connection(3, true); removed(3); await new Promise(setImmediate);
  assert.equal(data.shortmox_dashboard_connected, false);
  connection(4, true); startup(); await new Promise(setImmediate);
  assert.equal(data.shortmox_dashboard_connected, false);
  connection(5, true); installed(); await new Promise(setImmediate);
  assert.equal(data.shortmox_dashboard_connected, false);
});
