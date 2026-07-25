/**
 * 系統狀態頁 (規格書 §3.4-16)
 * 各資料來源最後更新時間、失敗告警
 */

(function () {
  const root = document.getElementById('status-root');
  const overall = document.getElementById('status-overall');

  const HEALTH = {
    ok: ['正常', 'text-green-400', 'fa-circle-check', 'border-green-900/50'],
    warn: ['資料過期', 'text-amber-400', 'fa-triangle-exclamation', 'border-amber-900/50'],
    error: ['失敗', 'text-red-400', 'fa-circle-xmark', 'border-red-900/60'],
    unknown: ['未知', 'text-slate-400', 'fa-circle-question', 'border-slate-800'],
  };

  const CATEGORY = { stats: '數據', injury: '傷病', odds: '盤口', model: '模型' };

  function card(s) {
    const [label, cls, icon, border] = HEALTH[s.health] || HEALTH.unknown;
    return `
    <article class="rounded-xl border ${border} bg-slate-900/60 p-4">
      <header class="flex items-start justify-between gap-2 mb-3">
        <div class="min-w-0">
          <h3 class="font-semibold text-sm truncate">${NBA.esc(s.display_name)}</h3>
          <p class="text-[11px] text-slate-500">
            <code>${NBA.esc(s.source_key)}</code>
            ${s.category ? ` · ${NBA.esc(CATEGORY[s.category] || s.category)}` : ''}
          </p>
        </div>
        <span class="edge-badge ${s.health === 'ok' ? 'edge-high' : s.health === 'warn' ? 'edge-low' : s.health === 'error' ? 'status-out' : 'edge-none'}">
          <i class="fas ${icon}"></i>${label}
        </span>
      </header>
      <dl class="grid grid-cols-2 gap-2 text-xs">
        <div><dt class="text-[10px] text-slate-500">最後成功</dt><dd class="num ${cls}">${NBA.ago(s.age_minutes)}</dd></div>
        <div><dt class="text-[10px] text-slate-500">預期間隔</dt><dd class="num text-slate-300">${s.expected_interval_min != null ? s.expected_interval_min + ' 分' : '—'}</dd></div>
        <div><dt class="text-[10px] text-slate-500">更新時間</dt><dd class="text-slate-400">${NBA.tpe(s.last_success_at)}</dd></div>
        <div><dt class="text-[10px] text-slate-500">寫入筆數</dt><dd class="num text-slate-300">${s.records_updated ?? '—'}</dd></div>
      </dl>
      ${
        s.last_error
          ? `<p class="mt-3 pt-2 border-t border-slate-800 text-[11px] text-red-400"><i class="fas fa-bug mr-1"></i>${NBA.esc(s.last_error)}</p>`
          : ''
      }
    </article>`;
  }

  async function load() {
    root.innerHTML = `<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">${NBA.skeletonCards(6)}</div>`;
    try {
      const d = await NBA.get('/api/system/status');
      const sources = d.sources || [];
      const [label, cls, icon] = HEALTH[d.overall] || HEALTH.unknown;
      overall.innerHTML = `
        <div class="flex flex-wrap items-center gap-4 text-xs">
          <span class="${cls} font-semibold"><i class="fas ${icon} mr-1"></i>整體狀態：${label}</span>
          <span class="text-slate-500">檢查時間 ${NBA.tpe(d.checked_at)}</span>
          <span class="text-slate-500">資料庫驅動 <code class="text-slate-400">${NBA.esc(d.db_driver)}</code></span>
          <span class="text-slate-500">階段 <code class="text-slate-400">${NBA.esc(d.stage)}</code></span>
        </div>
        ${d.note ? `<p class="text-[11px] text-slate-500 mt-1.5"><i class="fas fa-circle-info mr-1"></i>${NBA.esc(d.note)}</p>` : ''}`;

      if (!sources.length) {
        root.innerHTML = NBA.empty('尚無資料來源紀錄。階段二排程器啟動後會寫入 data_sources 表。', 'fa-plug');
        return;
      }
      root.innerHTML = `<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">${sources.map(card).join('')}</div>`;
    } catch (e) {
      root.innerHTML = NBA.error(`載入系統狀態失敗：${e.message}`);
    }
  }

  load();
  setInterval(load, 60000); // 每分鐘自動刷新
})();
