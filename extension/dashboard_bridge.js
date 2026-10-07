/* global chrome */

window.addEventListener('shortmox-radar-alert', (event) => {
  const alert = event.detail;
  if (alert?.schema_version === 1 && alert.event === 'ANOMALY_DETECTED') {
    chrome.runtime.sendMessage({ type: 'SHORTMOX_ALERT', alert }).catch(() => {});
  }
});

window.addEventListener('shortmox-radar-connection', (event) => {
  chrome.runtime.sendMessage({
    type: 'SHORTMOX_CONNECTION_STATUS',
    connected: Boolean(event.detail),
  }).catch(() => {});
});
