import { collectTwsport } from './collector.js';
import { SITE_ORIGIN, SCHEMA } from './config.js';
let busy = false;
chrome.runtime.onMessageExternal.addListener((message, sender, respond) => {
  let url;
  try { url = new URL(sender.url); } catch { return false; }
  if (url.origin !== SITE_ORIGIN || url.pathname !== '/decision' || sender.frameId !== 0) return false;
  if (message?.type === 'NBA_TWSPORT_PING') { respond({ ok: true, version: 'twsport-browser-v1' }); return false; }
  if (message?.type !== 'NBA_TWSPORT_COLLECT' || !/^\d{4}-\d{2}-\d{2}$/.test(message.date || '')) return false;
  if (busy) { respond({ ok: false, error: 'sync_busy' }); return false; }
  busy = true;
  // Bounded, user-initiated work only. Keep the MV3 worker alive while waiting for page requests.
  const keepAlive = setInterval(() => chrome.runtime.getPlatformInfo().catch(() => {}), 20000);
  (async () => {
    try {
      const tabs = await chrome.tabs.query({ url: ['https://www.sportslottery.com.tw/*', 'https://www-talo-ssb-pr.sportslottery.com.tw/*'] });
      for (const tab of tabs) {
        const frames = await chrome.webNavigation.getAllFrames({ tabId: tab.id });
        const frame = frames?.find(f => { try { return new URL(f.url).origin === 'https://www-talo-ssb-pr.sportslottery.com.tw'; } catch { return false; } });
        if (!frame) continue;
        const results = await chrome.scripting.executeScript({
          target: { tabId: tab.id, frameIds: [frame.frameId] }, world: 'MAIN',
          func: collectTwsport, args: [{ date: message.date, schema: SCHEMA }],
        });
        respond(results[0]?.result || { ok: false, error: 'source_unavailable' });
        return;
      }
      // Open the normal website for the user; do not click, authenticate, solve a challenge or retry.
      await chrome.tabs.create({ url: 'https://www.sportslottery.com.tw/sportsbook', active: true });
      respond({ ok: false, error: 'open_sportsbook' });
    } catch { respond({ ok: false, error: 'extension_permission' }); }
    finally { clearInterval(keepAlive); busy = false; }
  })();
  return true;
});
