/* global chrome */

const STORAGE_KEY = 'shortmox_alerts';
const CONNECTION_KEY = 'shortmox_dashboard_connected';
const CONNECTION_TABS_KEY = 'shortmox_dashboard_connected_tabs';
const MAX_ALERTS = 100;
let alertQueue = Promise.resolve();
let statusQueue = Promise.resolve();

chrome.runtime.onMessage.addListener((message, sender) => {
  if (message.type === 'SHORTMOX_ALERT' && isRadarEvent(message.alert)) {
    alertQueue = alertQueue.then(() => handleAnomaly(message.alert)).catch(() => {});
  }
  if (message.type === 'SHORTMOX_CONNECTION_STATUS' && Number.isInteger(sender.tab?.id)) {
    statusQueue = statusQueue.then(() => updateConnection(sender.tab.id, Boolean(message.connected))).catch(() => {});
  }
});

chrome.tabs.onRemoved?.addListener((tabId) => {
  statusQueue = statusQueue.then(() => updateConnection(tabId, false)).catch(() => {});
});

chrome.tabs.onUpdated?.addListener((tabId, changeInfo) => {
  if (changeInfo.status === 'loading') {
    statusQueue = statusQueue.then(() => updateConnection(tabId, false)).catch(() => {});
  }
});

function clearConnectionStatus() {
  statusQueue = statusQueue.then(() => chrome.storage.local.set({
    [CONNECTION_TABS_KEY]: {},
    [CONNECTION_KEY]: false,
  })).catch(() => {});
}

chrome.runtime.onStartup?.addListener(clearConnectionStatus);
chrome.runtime.onInstalled?.addListener(clearConnectionStatus);

async function updateConnection(tabId, connected) {
  const stored = await chrome.storage.local.get({ [CONNECTION_TABS_KEY]: {} });
  const tabs = { ...stored[CONNECTION_TABS_KEY] };
  if (connected) tabs[tabId] = true;
  else delete tabs[tabId];
  await chrome.storage.local.set({
    [CONNECTION_TABS_KEY]: tabs,
    [CONNECTION_KEY]: Object.keys(tabs).length > 0,
  });
}

function isRadarEvent(alert) {
  return alert?.schema_version === 1
    && alert.event === 'ANOMALY_DETECTED'
    && typeof alert.event_id === 'string'
    && alert.event_id.length > 0
    && typeof alert.tweet_id === 'string'
    && typeof alert.source_platform === 'string';
}

async function handleAnomaly(alert) {
  const result = await chrome.storage.local.get({ [STORAGE_KEY]: [] });
  const previous = result[STORAGE_KEY];
  if (previous.some((item) => item.event_id === alert.event_id)) return;
  await chrome.storage.local.set({ [STORAGE_KEY]: [alert, ...previous].slice(0, MAX_ALERTS) });

  const isCritical = alert.admiralty_code?.startsWith('A') || alert.admiralty_code?.startsWith('B');
  const platform = alert.source_platform;

  chrome.notifications.create(alert.event_id, {
    type: 'basic',
    iconUrl: 'icons/icon128.png',
    title: isCritical ? `CRITICAL SIGNAL [${alert.admiralty_code}]` : alert.admiralty_code ? `SIGNAL [${alert.admiralty_code}]` : 'RADAR SIGNAL',
    message: `[${platform}] @${alert.username || 'anonymous'}: ${alert.reason || (alert.text ? alert.text.substring(0, 80) : '')}`,
    priority: isCritical ? 2 : 1
  });

  if (platform === 'X_TWITTER') {
    chrome.tabs.query({ url: ['*://x.com/*', '*://twitter.com/*'] }, (tabs) => {
      for (const tab of tabs) {
        if (tab.id !== undefined) {
          chrome.tabs.sendMessage(tab.id, { type: 'SHORTMOX_ALERT', alert }).catch(() => {});
        }
      }
    });
  }
}
