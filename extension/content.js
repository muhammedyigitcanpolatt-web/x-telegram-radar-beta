/* global chrome */

const HIGHLIGHT_CLASS = 'shortmox-highlight';

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === 'SHORTMOX_ALERT' && message.alert?.source_platform === 'X_TWITTER') {
    highlightTweet(message.alert.tweet_id, message.alert.reason, message.alert.score,
      message.alert.admiralty_code, message.alert.score_kind, message.alert.confidence);
  }
});

function articleTweetId(article) {
  const declaredId = article.getAttribute('data-tweet-id');
  if (/^\d+$/.test(declaredId || '')) return declaredId;
  // Only the article's own timestamp identifies it, never a quoted tweet.
  for (const anchor of article.querySelectorAll('a[href]')) {
    if (!anchor.querySelector('time') || anchor.closest('article') !== article
        || anchor.closest('[data-testid="quoteTweet"]')) continue;
    try {
      const url = new URL(anchor.getAttribute('href'), window.location.href);
      if (!['x.com', 'twitter.com', 'www.x.com', 'www.twitter.com'].includes(url.hostname)) continue;
      const match = url.pathname.match(/^\/(?:[A-Za-z0-9_]+|i\/web)\/status\/(\d+)(?:\/|$)/);
      if (match) return match[1];
    } catch (_) {
      // Ignore malformed page links.
    }
  }
  return null;
}

function highlightTweet(tweetId, reason, score, admiraltyCode, scoreKind, confidence) {
  if (typeof tweetId !== 'string' || !/^\d+$/.test(tweetId)) return false;
  const target = Array.from(document.querySelectorAll('article')).find(
    (article) => articleTweetId(article) === tweetId
  );
  // Unmatched alerts remain in the popup; never substitute another article.
  if (!target || target.classList.contains(HIGHLIGHT_CLASS)) return false;

  target.classList.add(HIGHLIGHT_CLASS);
  const banner = document.createElement('div');
  banner.className = 'shortmox-banner';
  const badge = document.createElement('span');
  badge.className = 'shortmox-badge';
  badge.textContent = 'SHORTMOX RADAR // ' + (admiraltyCode || 'UNASSESSED');
  const explanation = document.createElement('span');
  explanation.className = 'shortmox-reason';
  explanation.textContent = reason || 'Radar signal';
  const scoreElement = document.createElement('span');
  scoreElement.className = 'shortmox-score';
  const scoreLabel = scoreKind === 'anomaly_strength' ? 'ANOMALY STRENGTH' : 'CONFIDENCE';
  const displayScore = scoreKind === 'anomaly_strength' ? score : confidence ?? score;
  scoreElement.textContent = typeof displayScore === 'number' && Number.isFinite(displayScore)
    ? scoreLabel + ': ' + displayScore + (scoreKind === 'anomaly_strength' ? '/100' : '%')
    : scoreLabel + ': not specified';
  banner.append(badge, explanation, scoreElement);
  target.prepend(banner);

  const observer = new MutationObserver(() => {
    // X recycles article nodes while scrolling.
    if (articleTweetId(target) !== tweetId) cleanup();
  });
  let timer;
  function cleanup() {
    observer.disconnect();
    clearTimeout(timer);
    target.classList.remove(HIGHLIGHT_CLASS);
    banner.remove();
  }
  observer.observe(target, { subtree: true, childList: true, attributes: true, attributeFilter: ['href', 'data-tweet-id'] });
  timer = setTimeout(cleanup, 30000);
  return true;
}
