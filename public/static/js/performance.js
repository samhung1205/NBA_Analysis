/**
 * 回測 / 績效頁 (規格書 §3.4-15)
 * 模型歷史準確率、ATS、模擬 ROI、個人實際投注損益曲線
 */

(function () {
  const metricsRoot = document.getElementById('metrics-root');
  const pnlRoot = document.getElementById('pnl-root');
  let pnlChart = null;

  function metricCard(m) {
    const cell = (label, value, cls = '') =>
      `<div><div class="text-[10px] text-slate-500">${label}</div><div class="text-sm font-semibold num ${cls}">${value}</div></div>`;
    // 規格書 Phase B 驗收：Elo accuracy ≥ 63%
    const accCls = m.accuracy >= 0.63 ? 'text-green-400' : m.accuracy >= 0.6 ? 'text-lime-400' : 'text-amber-400';
    const roiCls = (m.sim_roi ?? 0) > 0 ? 'text-green-400' : 'text-red-400';
    return `
    <article class="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <header class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-sm text-orange-300">${NBA.esc(m.model_version)}</h3>
        <span class="text-[11px] text-slate-500">${NBA.esc(m.season || '')} · ${NBA.tpe(m.evaluated_at).slice(0, 9)}</span>
      </header>
      <div class="grid grid-cols-3 sm:grid-cols-4 gap-3">
        ${cell('勝負命中率', NBA.pct(m.accuracy), accCls)}
        ${cell('讓分 ATS', NBA.pct(m.ats_accuracy))}
        ${cell('大小分', NBA.pct(m.ou_accuracy))}
        ${cell('上半場', NBA.pct(m.h1_accuracy))}
        ${cell('模擬 ROI', m.sim_roi != null ? NBA.signed(m.sim_roi * 100, 2) + '%' : '—', roiCls)}
        ${cell('Log Loss', NBA.fixed(m.log_loss, 4))}
        ${cell('Brier', NBA.fixed(m.brier, 4))}
        ${cell('樣本場次', m.n_games ?? '—')}
      </div>
      <div class="grid grid-cols-2 gap-3 mt-3 pt-3 border-t border-slate-800">
        ${cell('分差 MAE', NBA.fixed(m.mae_margin, 2))}
        ${cell('總分 MAE', NBA.fixed(m.mae_total, 2))}
      </div>
      ${m.notes ? `<p class="text-[11px] text-slate-500 mt-3">${NBA.esc(m.notes)}</p>` : ''}
    </article>`;
  }

  async function loadMetrics() {
    metricsRoot.innerHTML = `<div class="grid gap-4 lg:grid-cols-2">${NBA.skeletonCards(2)}</div>`;
    try {
      const d = await NBA.get('/api/metrics');
      const ms = d.metrics || [];
      if (!ms.length) {
        metricsRoot.innerHTML = NBA.empty(
          '尚無模型回測資料。階段二 Phase B/C 完成 walk-forward 回測後會寫入 model_metrics 表。',
          'fa-chart-column'
        );
        return;
      }
      metricsRoot.innerHTML = `<div class="grid gap-4 lg:grid-cols-2">${ms.map(metricCard).join('')}</div>`;
    } catch (e) {
      metricsRoot.innerHTML = NBA.error(`載入模型績效失敗：${e.message}`);
    }
  }

  async function loadPnl() {
    pnlRoot.innerHTML = `<div class="skeleton h-56"></div>`;
    try {
      const d = await NBA.get('/api/bets');
      const s = d.summary;
      const curve = d.pnl_curve || [];
      const roiCls = (s.roi ?? 0) > 0 ? 'text-green-400' : (s.roi ?? 0) < 0 ? 'text-red-400' : 'text-slate-300';

      pnlRoot.innerHTML = `
        <div class="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3 mb-5">
          ${statBox('已結算', `${s.win + s.lose + s.push}`)}
          ${statBox('勝 / 敗', `${s.win} / ${s.lose}`)}
          ${statBox('命中率', NBA.pct(s.hit_rate))}
          ${statBox('待結算', s.pending)}
          ${statBox('總投注額', s.total_staked.toLocaleString())}
          ${statBox('損益', NBA.signed(s.pnl, 0), (s.pnl > 0 ? 'text-green-400' : s.pnl < 0 ? 'text-red-400' : ''))}
          ${statBox('實際 ROI', s.roi != null ? NBA.signed(s.roi * 100, 2) + '%' : '—', roiCls)}
        </div>
        ${
          curve.length
            ? `<div class="relative h-64"><canvas id="pnl-chart"></canvas></div>`
            : NBA.empty('尚無已結算的投注紀錄。到「投注紀錄」頁新增並結算後，這裡會顯示損益曲線。', 'fa-receipt')
        }`;

      if (curve.length) {
        const canvas = document.getElementById('pnl-chart');
        if (pnlChart) pnlChart.destroy();
        pnlChart = new Chart(canvas, {
          type: 'line',
          data: {
            labels: curve.map((p) => NBA.tpe(p.placed_at).slice(0, 9)),
            datasets: [
              {
                label: '累積損益（台彩實際賠率）',
                data: curve.map((p) => p.cumulative_pnl),
                borderColor: '#f97316',
                backgroundColor: 'rgba(249,115,22,0.12)',
                fill: true,
                tension: 0.25,
                pointRadius: 2,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
              x: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1e293b' } },
              y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1e293b' } },
            },
            plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 }, boxWidth: 12 } } },
          },
        });
      }
    } catch (e) {
      if (e.status === 401) {
        pnlRoot.innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-6 text-center">
          <i class="fas fa-lock text-2xl text-slate-600 mb-2"></i>
          <p class="text-sm text-slate-400 mb-3">個人投注損益為私人資料，請先登入</p>
          <a href="/login?next=/performance" class="inline-block px-3 py-1.5 rounded bg-orange-500 text-white text-sm">前往登入</a>
        </div>`;
      } else {
        pnlRoot.innerHTML = NBA.error(`載入個人績效失敗：${e.message}`);
      }
    }
  }

  function statBox(label, value, cls = '') {
    return `<div class="rounded-lg bg-slate-800/50 p-2.5">
      <div class="text-[10px] text-slate-500 mb-0.5">${label}</div>
      <div class="text-sm font-bold num ${cls}">${value}</div>
    </div>`;
  }

  loadMetrics();
  loadPnl();
})();
