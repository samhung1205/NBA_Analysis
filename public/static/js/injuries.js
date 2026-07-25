/**
 * 傷病中心 (規格書 §3.4-14)
 * 當日各隊傷病報告 + 主力缺陣警示
 */

(function () {
  const root = document.getElementById('injuries-root');
  const summary = document.getElementById('injuries-summary');
  const picker = document.getElementById('inj-date');

  function teamCard(t) {
    const alert = t.key_players_out > 0;
    const rows = t.players
      .map(
        (p) => `<tr class="${p.key_absence_alert ? 'bg-red-950/20' : ''}">
        <td class="text-xs">
          ${NBA.esc(p.name || '—')}
          ${p.is_starter ? '<span class="text-[10px] text-amber-400 ml-1">主力</span>' : ''}
        </td>
        <td class="text-xs text-slate-500">${NBA.esc(p.position || '—')}</td>
        <td>${NBA.statusBadge(p.status)}</td>
        <td class="text-xs text-slate-400">${NBA.esc(p.reason || '—')}</td>
        <td class="text-xs text-slate-500 whitespace-nowrap">${NBA.tpe(p.report_time_utc)}</td>
      </tr>`
      )
      .join('');

    return `
    <article class="rounded-xl border ${alert ? 'border-red-900/60' : 'border-slate-800'} bg-slate-900/60 overflow-hidden">
      <header class="flex items-center justify-between px-4 py-2.5 border-b border-slate-800">
        <h3 class="font-semibold text-sm">
          <span class="text-slate-500 text-xs mr-1.5">${NBA.esc(t.team.abbr || '')}</span>
          ${NBA.esc(NBA.teamName(t.team))}
        </h3>
        ${
          alert
            ? `<span class="edge-badge status-out"><i class="fas fa-triangle-exclamation"></i>主力缺陣 ${t.key_players_out} 人</span>`
            : `<span class="text-[11px] text-slate-500">${t.players.length} 筆申報</span>`
        }
      </header>
      <div class="overflow-x-auto">
        <table class="stat-table text-sm">
          <thead><tr><th>球員</th><th>位置</th><th>狀態</th><th>原因</th><th>申報時間</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </article>`;
  }

  async function load(date) {
    picker.value = date;
    root.innerHTML = `<div class="grid gap-4 lg:grid-cols-2">${NBA.skeletonCards(4)}</div>`;
    summary.innerHTML = '';
    try {
      const d = await NBA.get(`/api/injuries/today?date=${encodeURIComponent(date)}`);
      const teams = d.teams || [];
      const keyOut = teams.reduce((s, t) => s + t.key_players_out, 0);
      summary.innerHTML = `
        <div class="flex flex-wrap gap-4 text-xs text-slate-400">
          <span><i class="fas fa-clipboard-list mr-1 text-slate-500"></i>申報筆數 <span class="text-slate-100 font-semibold num">${d.total_reports}</span></span>
          <span><i class="fas fa-people-group mr-1 text-slate-500"></i>涉及球隊 <span class="text-slate-100 font-semibold num">${teams.length}</span></span>
          <span><i class="fas fa-triangle-exclamation mr-1 text-red-500"></i>主力缺陣 <span class="text-red-400 font-semibold num">${keyOut}</span></span>
        </div>`;
      if (!teams.length) {
        root.innerHTML = NBA.empty(
          `${date}（台灣時間）沒有傷病申報資料。NBA 官方於賽前一日當地 17:00 前申報，階段二排程會每 30 分鐘更新。`,
          'fa-notes-medical'
        );
        return;
      }
      root.innerHTML = `<div class="grid gap-4 lg:grid-cols-2">${teams.map(teamCard).join('')}</div>`;
    } catch (e) {
      root.innerHTML = NBA.error(`載入傷病資料失敗：${e.message}`);
    }
  }

  const tabs = document.querySelectorAll('[data-inj-tab]');
  tabs.forEach((tab) => {
    tab.addEventListener('click', () => {
      tabs.forEach((t) => {
        t.classList.remove('bg-orange-500/15', 'text-orange-300', 'font-medium');
        t.classList.add('text-slate-400');
      });
      tab.classList.add('bg-orange-500/15', 'text-orange-300', 'font-medium');
      tab.classList.remove('text-slate-400');
      load(NBA.tpeDateStr(Number(tab.dataset.injTab)));
    });
  });

  picker.addEventListener('change', () => picker.value && load(picker.value));
  load(NBA.tpeDateStr(0));
})();
