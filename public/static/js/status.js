/**
 * 系統狀態頁 (規格書 §3.4-16)
 * 各資料來源最後更新時間、失敗告警
 */

(function () {
  // 預期間隔：分鐘 → 人讀得懂的單位（10080 分 → 7 天）
  const interval = (m) => m == null ? '—' : m >= 1440 && m % 1440 === 0 ? `${m / 1440} 天` : m >= 60 && m % 60 === 0 ? `${m / 60} 小時` : `${m} 分`;
  const root = document.getElementById('status-root');
  const overall = document.getElementById('status-overall');

  const HEALTH = {
    ok: ['正常', 'text-green-400', 'fa-circle-check', 'border-green-900/50'],
    warn: ['需留意', 'text-amber-400', 'fa-triangle-exclamation', 'border-amber-900/50'],
    error: ['失敗', 'text-red-400', 'fa-circle-xmark', 'border-red-900/60'],
    unknown: ['未知', 'text-slate-400', 'fa-circle-question', 'border-slate-800'],
  };

  const STATES = {
    ...HEALTH,
    stale: ['資料過期', ...HEALTH.warn.slice(1)],
    warning: ['執行警告', ...HEALTH.warn.slice(1)],
    blocked: ['需匯入盤口', ...HEALTH.warn.slice(1)],
    waiting: ['等待排程', 'text-slate-300', 'fa-clock', 'border-slate-800'],
    standby: ['備援待命', ...HEALTH.unknown.slice(1)],
    inactive: ['未使用', ...HEALTH.unknown.slice(1)],
    historical: ['歷史回填', ...HEALTH.unknown.slice(1)],
  };
  const CATEGORY = { games: '賽程／比分', stats: '數據', injury: '傷病', odds: '盤口', model: '模型', notify: '推播' };

  function card(s) {
    const [label, cls, icon, border] = STATES[s.state] || HEALTH[s.health] || HEALTH.unknown;
    return `
    <article class="rounded-xl border ${border} bg-slate-900/60 p-4">
      <header class="flex items-start justify-between gap-2 mb-3">
        <div class="min-w-0">
          <h3 class="font-semibold text-sm">${NBA.esc(s.display_name)}</h3>
          <p class="text-xs text-slate-400">
            <code>${NBA.esc(s.source_key)}</code>
            ${s.category ? ` · ${NBA.esc(CATEGORY[s.category] || s.category)}` : ''}
          </p>
        </div>
        <span class="edge-badge shrink-0 ${s.health === 'ok' && s.state !== 'waiting' ? 'status-available' : s.health === 'warn' ? 'status-questionable' : s.health === 'error' ? 'status-out' : 'edge-chip-none'}">
          <i class="fas ${icon}" aria-hidden="true"></i>${label}
        </span>
      </header>
      <dl class="grid grid-cols-2 gap-2 text-xs">
        <div><dt class="text-xs text-slate-400">最後成功</dt><dd class="num ${cls}">${NBA.ago(s.age_minutes)}</dd></div>
        <div><dt class="text-xs text-slate-400">預期間隔</dt><dd class="num text-slate-300">${interval(s.expected_interval_min)}</dd></div>
        <div><dt class="text-xs text-slate-400">更新時間</dt><dd class="text-slate-400">${NBA.tpe(s.last_success_at)}</dd></div>
        <div><dt class="text-xs text-slate-400">寫入筆數</dt><dd class="num text-slate-300">${s.records_updated ?? '—'}</dd></div>
        <div><dt class="text-xs text-slate-400">最後執行</dt><dd class="text-slate-300">${NBA.tpe(s.last_attempt_at)}</dd></div>
        ${s.next_poll_at ? `<div><dt class="text-xs text-slate-400">下次排程</dt><dd class="text-slate-300">${NBA.tpe(s.next_poll_at)}</dd></div>` : ''}
      </dl>
      ${s.detail ? `<p class="mt-3 text-xs text-slate-300">${NBA.esc(s.detail)}</p>` : ''}
      ${s.action ? `<p class="mt-2 text-xs text-amber-300">${NBA.esc(s.action)}</p>` : ''}
      ${
        s.last_error
          ? `<p class="mt-3 pt-2 border-t border-slate-800 text-xs ${s.operational && s.health === 'error' ? 'text-red-400' : 'text-amber-300'}">上次執行紀錄：${NBA.esc(s.last_error)}</p>`
          : ''
      }
    </article>`;
  }

  // 目前需處理的來源優先，歷史與未使用來源列在後面。
  const SEVERITY = { error: 0, warn: 1, unknown: 2, ok: 3 };
  // 台彩自動擷取被阻擋時的合規備援（不繞過防護；HAR 含登入資訊，不要分享或提交）
  const HAR_HELP = `<section class="panel mt-5" id="har-help">
      <h2><i class="fas fa-file-import mr-1.5" aria-hidden="true"></i>台彩盤口手動匯入（自動擷取被阻擋時）</h2>
      <ol class="list-decimal ml-5 space-y-1 text-xs text-slate-300">
        <li>一般 Chrome → 開發者工具 → Network，載入台彩 NBA 聯盟與相關比賽的全場 / 上半場盤口 → 匯出 HAR（sanitized，含回應內容），存到 <code class="text-slate-200">pipeline/captures/</code>。</li>
        <li>離線檢查涵蓋範圍：<code class="text-slate-200">.venv/bin/python run_twsport_har.py captures/&lt;檔名&gt;.har --inspect</code></li>
        <li>比對資料庫（不寫入）：<code class="text-slate-200">.venv/bin/python run_twsport_har.py captures/&lt;檔名&gt;.har</code></li>
        <li>確認目標資料庫後匯入並刪檔：<code class="text-slate-200">… --import --delete-after</code></li>
      </ol>
      <p class="text-xs text-slate-400 mt-2">建議在開賽前 120～60 分鐘內擷取並盡快匯入；完整說明見 <code>docs/production-runbook.md</code> §7。</p>
      <p class="text-xs text-amber-300/90 mt-2"><i class="fas fa-lock mr-1" aria-hidden="true"></i>HAR 檔可能含登入資訊，請勿分享或提交到版本庫。</p>
    </section>`;
  let loaded = false;

  async function load() {
    // 第一次才顯示骨架；每分鐘自動刷新時直接替換，不閃爍
    if (!loaded) root.innerHTML = `<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">${NBA.skeletonCards(6)}</div>`;
    try {
      const d = await NBA.get('/api/system/status');
      const sources = d.sources || [];
      const [label, cls, icon] = HEALTH[d.overall] || HEALTH.unknown;
      overall.innerHTML = `
        <div class="flex flex-wrap items-center gap-4 text-xs">
          <span class="${cls} font-semibold"><i class="fas ${icon} mr-1"></i>整體狀態：${label}</span>
          <span class="text-slate-400">檢查時間 ${NBA.tpe(d.checked_at)}</span>
          <span class="text-slate-400">資料庫驅動 <code class="text-slate-400">${NBA.esc(d.db_driver)}</code></span>
          <span class="text-slate-400">階段 <code class="text-slate-400">${NBA.esc(d.stage)}</code></span>
        </div>
        ${d.note ? `<p class="text-xs text-slate-400 mt-1.5"><i class="fas fa-circle-info mr-1"></i>${NBA.esc(d.note)}</p>` : ''}
        <p class="text-xs text-slate-300 mt-2">即時來源過期 ${sources.filter(s => s.operational && s.state === 'stale').length} 項
          · 即時來源警告／受阻 ${sources.filter(s => s.operational && ['warning', 'blocked'].includes(s.state)).length} 項
          · 歷史回填警告 ${sources.filter(s => !s.operational && ['warn', 'error'].includes(s.health)).length} 項</p>`;

      if (!sources.length) {
        root.innerHTML = NBA.empty('尚無資料來源紀錄。階段二排程器啟動後會寫入 data_sources 表。', 'fa-plug');
        return;
      }
      const sorted = [...sources].sort((a, b) => Number(b.operational !== false) - Number(a.operational !== false)
        || (SEVERITY[a.health] ?? 2) - (SEVERITY[b.health] ?? 2));
      root.innerHTML = `<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">${sorted.map(card).join('')}</div>${HAR_HELP}`;
      loaded = true;
    } catch (e) {
      root.innerHTML = NBA.error(`載入系統狀態失敗：${e.message}`);
    }
  }

  load();
  setInterval(load, 60000); // 每分鐘自動刷新
})();
