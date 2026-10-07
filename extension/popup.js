/* global chrome */

const STORAGE_KEY = 'shortmox_alerts';
const CONNECTION_KEY = 'shortmox_dashboard_connected';

const alertList = document.getElementById('alert-list');
const clearBtn = document.getElementById('clear-btn');
const statusDot = document.getElementById('connection-status');

function formatTime(isoString) {
  const d = new Date(isoString);
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

async function renderAlerts() {
  const result = await chrome.storage.local.get({ [STORAGE_KEY]: [] });
  const alerts = result[STORAGE_KEY];

  if (!alerts || alerts.length === 0) {
    alertList.innerHTML = '<li class="empty">📡 Waiting for radar alerts. No anomaly has been reported yet.</li>';
    return;
  }

  alertList.innerHTML = alerts.map((alert, index) => {
    const isCritical = alert.admiralty_code?.startsWith('A') || alert.admiralty_code?.startsWith('B');
    const borderClass = isCritical ? 'critical-border' : 'warning-border';
    const platform = alert.source_platform || 'UNKNOWN';
    const adm = alert.admiralty_code || 'UNASSESSED';

    return `
      <li class="alert-item ${borderClass}" data-index="${index}">
        <div class="alert-reason">${escapeHtml(alert.reason || 'Critical intelligence signal')}</div>
        <div class="alert-meta">
          <span class="alert-user">@${escapeHtml(alert.username || 'anonymous')}</span>
          <div class="alert-tags">
            <span class="badge-platform">${escapeHtml(platform)}</span>
            <span class="badge-admiralty">${escapeHtml(adm)}</span>
          </div>
        </div>
        <div class="alert-text">${escapeHtml(alert.text)}</div>
        <div class="alert-time">${formatTime(alert.receivedAt || alert.detected_at)}</div>
      </li>
    `;
  }).join('');

  document.querySelectorAll('.alert-item').forEach(item => {
    item.addEventListener('click', () => {
      const url = alertSourceUrl(alerts[Number(item.dataset.index)]);
      if (url) chrome.tabs.create({ url });
    });
  });
}

function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

clearBtn.addEventListener('click', async () => {
  await chrome.storage.local.set({ [STORAGE_KEY]: [] });
  renderAlerts();
});

function alertSourceUrl(alert) {
  if (!alert) return null;
  if (alert.source_platform === 'X_TWITTER' && /^\d+$/.test(alert.tweet_id || '')) {
    return `https://x.com/i/web/status/${alert.tweet_id}`;
  }
  if (alert.source_platform === 'TELEGRAM') {
    const username = String(alert.username || '').replace(/^tg_/, '').replace(/^@/, '');
    if (/^[A-Za-z][A-Za-z0-9_]{4,31}$/.test(username) && username !== 'UnknownTelegramUser') {
      return `https://t.me/${username}`;
    }
  }
  return null;
}

async function checkConnection() {
  const result = await chrome.storage.local.get({ [CONNECTION_KEY]: false });
  const connected = Boolean(result[CONNECTION_KEY]);
  statusDot.classList.toggle('online', connected);
  statusDot.title = connected
    ? 'Authenticated dashboard connection is open'
    : 'Open a signed-in dashboard tab to receive alerts';
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes[CONNECTION_KEY]) checkConnection();
  if (area === 'local' && changes[STORAGE_KEY]) renderAlerts();
});

checkConnection();
renderAlerts();
