/** Browser capture transport only. All odds, pricing, sizing and capacity are verified by Python. */
(function () {
  const button = document.getElementById('twsport-sync-button');
  const status = document.getElementById('twsport-sync-status');
  if (!button || !status) return;
  let extensionId = null, available = false, busy = false;
  const active = job => job && ['capturing', 'queued', 'running'].includes(job.state);
  const TEXT = {
    not_enabled: '同步功能尚未啟用；完成網站與 Python 處理器設定後即可使用。',
    migration_required: '同步資料表尚未準備完成，請先完成伺服器設定。',
    extension_not_configured: '網站尚未連結同步助手，請先完成設定。',
    worker_unavailable: 'Python 匯入處理器目前未連線，請稍後再試。',
    open_sportsbook: '已開啟台彩。請正常載入 NBA 盤口後，回到這裡再按同步。',
    blocked: '台彩阻擋這次讀取，已停止；不會繞過防護或冒充匯入成功。',
    rate_limited: '台彩要求降低頻率，已停止；請稍後再試。',
    no_games: '台彩頁面沒有所選日期尚未開賽的 NBA 盤口，未匯入資料。',
    parser_changed: '台彩資料格式與目前版本不符，已停止；請保留原有資料並檢查同步助手版本。',
    capture_time_invalid: '擷取時間無法驗證，請確認電腦時間正確後重新同步。',
    capture_too_large: '盤口資料超過這次同步可處理的大小，未送出。',
    extension_permission: '無法讀取台彩頁面，請確認同步助手已獲准存取台彩。',
    sync_busy: '已有同步工作正在進行，請稍後再試。',
    cooldown: '請等待 30 秒再同步。',
    expired: '工作已逾時，結果未確認；請重新整理後檢查，再決定是否重試。',
    capture_cancelled: '上次擷取未完成，沒有排入匯入工作。',
    partial_import_or_refresh: '盤口已部分匯入，或決策尚未完成更新；請檢查系統狀態，不把舊額度當作最新額度。',
    no_snapshots_imported: '沒有成功匯入可用盤口，請確認對戰、時間與系統狀態。',
    processing_failed: 'Python 匯入失敗，請檢查系統狀態；未宣稱資料已更新。',
  };
  function say(text) { status.textContent = text; }
  function render(job) {
    if (!job) return;
    if (job.state === 'capturing') say('正在從一般 Chrome 的台彩頁面擷取盤口，請保持此頁開啟…');
    else if (job.state === 'queued') say('盤口已送達，等待 Python 驗證與匯入…');
    else if (job.state === 'running') say('Python 正在驗證盤口、匯入並更新決策…');
    else if (job.state === 'succeeded') {
      const s = job.summary || {};
      const times = s.capture_times || [];
      say(`已匯入 ${s.markets ?? 0} 個市場（${s.games ?? 0} 場），決策已更新。盤口實際擷取時間：${NBA.tpe(times[0])}${times.length > 1 ? ' ～ ' + NBA.tpe(times.at(-1)) : ''}。可操作與否仍以決策中心當前有效額度為準。`);
    } else say(TEXT[job.error_code] || '同步未完整完成，請檢查系統狀態後再試。');
  }
  const message = data => new Promise((resolve, reject) => {
    if (!globalThis.chrome?.runtime?.sendMessage || !extensionId) return reject(new Error('helper_missing'));
    chrome.runtime.sendMessage(extensionId, data, response => {
      if (chrome.runtime.lastError || !response) reject(new Error('helper_missing'));
      else resolve(response);
    });
  });
  function controls() { button.disabled = busy || !available; button.textContent = busy ? '同步進行中…' : '同步台彩盤口'; }
  async function poll(id) {
    for (let n = 0; n < 105; n++) {
      const d = await NBA.get(`/api/twsport-sync?id=${encodeURIComponent(id)}`);
      render(d.job);
      if (!active(d.job)) {
        if (['succeeded', 'partial'].includes(d.job?.state)) window.dispatchEvent(new Event('nba:twsport-synced'));
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 3000));
    }
    say('工作尚未確認完成，請重新整理以查看原工作結果；不要連續重送。');
  }
  async function init() {
    try {
      const d = await NBA.get('/api/twsport-sync');
      extensionId = d.extension_id;
      available = d.enabled;
      if (!available) { if (d.job) render(d.job); say(TEXT[d.reason] || '同步目前不可用，請先完成設定。'); }
      else {
        try { await message({ type: 'NBA_TWSPORT_PING' }); }
        catch { available = false; say('請使用已安裝同步助手的桌面 Chrome，再重新整理此頁。'); }
        if (available) {
          if (d.job) render(d.job); else say('請先正常開啟台彩 NBA 頁面，再按同步。');
        }
      }
      if (active(d.job)) { busy = true; controls(); await poll(d.job.id); busy = false; }
    } catch (e) { say(e.status === 401 ? '登入後即可查看盤口同步功能。' : '無法確認同步狀態，請稍後重新整理。'); }
    controls();
  }
  button.addEventListener('click', async () => {
    if (busy || !available) return;
    busy = true; controls();
    let job = null;
    try {
      const prepared = await NBA.post('/api/twsport-sync', {
        client_request_id: crypto.randomUUID(), betting_day: document.getElementById('decision-date').value,
      });
      job = prepared.job;
      render(job);
      const result = await message({ type: 'NBA_TWSPORT_COLLECT', date: job.betting_day });
      if (!result.ok) {
        await NBA.post(`/api/twsport-sync/${encodeURIComponent(job.id)}/cancel`, {});
        say(TEXT[result.error] || '台彩擷取未完成，未送出盤口資料。');
        return;
      }
      say('擷取完成，正在傳送盤口…');
      const uploaded = await NBA.post(`/api/twsport-sync/${encodeURIComponent(job.id)}/capture`, result.capture);
      render(uploaded.job);
      await poll(job.id);
    } catch (e) {
      if (job) {
        try {
          const current = await NBA.get(`/api/twsport-sync?id=${encodeURIComponent(job.id)}`);
          if (current.job?.state === 'capturing') await NBA.post(`/api/twsport-sync/${encodeURIComponent(job.id)}/cancel`, {});
          else { render(current.job); await poll(job.id); return; }
        } catch { /* receipt can be recovered after reload, without re-uploading */ }
      }
      say(e.message === 'helper_missing' ? '同步助手未連線，請確認已安裝並允許此網站使用。' : TEXT[e.data?.error] || '同步結果未確認，請重新整理查看原工作；不要連續重送。');
    } finally { busy = false; controls(); }
  });
  init();
})();
