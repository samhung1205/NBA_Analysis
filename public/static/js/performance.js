/**
 * 回測 / 績效頁 (規格書 §3.4-15)
 * 模型歷史準確率、ATS、模擬 ROI、個人實際投注損益曲線
 */

(function () {
  const metricsRoot = document.getElementById('metrics-root');
  const pnlRoot = document.getElementById('pnl-root');
  const observabilityRoot = document.getElementById('model-observability-root');
  let pnlChart = null;

  function evaluationTable(evaluations) {
    const rows = evaluations.map(([label, e]) => `<tr class="border-t border-slate-800">
      <th scope="row" class="py-3 pr-4 text-left font-medium">${NBA.esc(label)}</th>
      <td class="px-3 py-3 num">${e?.n_games ?? 0}</td>
      <td class="px-3 py-3 num">${NBA.pct(e?.win_logistic?.accuracy)}</td>
      <td class="px-3 py-3 num">${NBA.fixed(e?.win_logistic?.log_loss, 4)}</td>
      <td class="px-3 py-3 num">${NBA.fixed(e?.win_logistic?.brier, 4)}</td>
      <td class="px-3 py-3 num">${NBA.fixed(e?.margin?.mae, 2)}</td>
      <td class="px-3 py-3 num">${NBA.fixed(e?.total?.mae, 2)}</td>
      <td class="pl-3 py-3 num">${NBA.fixed(e?.h1_total?.mae, 2)}</td></tr>`).join('');
    return `<div class="overflow-x-auto" role="region" aria-label="模型績效比較表，可左右捲動" tabindex="0">
      <table class="w-full text-sm text-right whitespace-nowrap"><caption class="sr-only">每場只計一筆，MAE 單位為分；Log loss、Brier 與 MAE 越低越好</caption>
        <thead class="text-xs text-slate-400"><tr><th scope="col" class="pb-2 text-left">評估</th>
          ${['場次', '勝負命中', 'Log loss', 'Brier', '分差 MAE', '總分 MAE', '半場總分 MAE'].map(x => `<th scope="col" class="px-3 pb-2 font-medium">${x}</th>`).join('')}
        </tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function reportTime(row) {
    return `<p class="text-xs mt-2 ${row.stale ? 'text-amber-300' : 'text-slate-400'}">${row.stale ? '資料已過期，僅供歷史檢視。' : ''}更新：${NBA.esc(NBA.tpe(row.generated_at))}</p>`;
  }

  function prospectivePanel(row) {
    const heading = '<h2 class="text-sm font-semibold text-slate-200 mb-3">本季實際預測</h2>';
    if (!row?.report) return `${heading}${NBA.empty('尚待 Python 產生前瞻績效報告；歷史回測不代表本季表現。', 'fa-clock')}`;
    const r = row.report;
    const final = r.evaluations?.final_t60;
    const range = r.game_range_utc?.map(t => NBA.esc(NBA.tpe(t))).join(' 至 ') || '尚無已完賽樣本';
    return `${heading}<p class="text-sm text-slate-300 mb-2">${NBA.esc(r.model_version)} · 最近 14 天已完賽 ${r.n_final_games ?? 0} 場</p>
      <p class="text-xs text-slate-400 mb-3">${range}。只評分當時已保存的賽前預測，不用今天的模型重算。</p>
      ${evaluationTable([['final · T-60', final], ['early · 最早可用', r.evaluations?.early]])}
      <p class="text-xs text-slate-400 mt-3">T-60 有預測 ${final?.n_games ?? 0}/${r.n_final_games ?? 0} 場；機率評分為網站 logistic 勝率。台彩定價另採分差分佈。MAE 單位為分。</p>
      <p class="text-xs text-slate-400 mt-2">這是預測表現，不能當作台彩策略 ROI；缺少當時可用的盤口時，投注效果仍無證據。</p>${reportTime(row)}`;
  }

  function shadowPanel(row) {
    const heading = '<h2 class="text-sm font-semibold text-slate-200 mt-8 mb-3">獨立影子實驗</h2>';
    const s = row?.report;
    if (!s || s.status === 'NOT_ACTIVATED') return `${heading}${NBA.empty('尚未啟用。啟用後僅收集未來比賽，不影響正式預測或下注額度。', 'fa-flask')}${row ? reportTime(row) : ''}`;
    const state = s.state;
    const shifts = state?.shifts || {};
    const labels = { margin: '分差', total: '總分', h1_margin: '半場分差', h1_total: '半場總分' };
    const shiftText = Object.entries(labels).map(([t, label]) => `${label} ${NBA.signed(shifts[t] ?? 0, 2)}`).join('、');
    const deltas = Object.entries(labels).map(([t, label]) => {
      const d = s.paired_mae_delta?.[t];
      return `<tr class="border-t border-slate-800"><th scope="row" class="text-left py-2 font-medium">${label}</th>
        <td class="text-right py-2 num">${d?.n ?? 0}</td><td class="text-right py-2 num">${d?.candidate_minus_baseline == null ? '—' : NBA.signed(d.candidate_minus_baseline, 3)}</td></tr>`;
    }).join('');
    return `${heading}<p class="text-sm text-slate-300">${NBA.esc(s.version)} · ${s.collector_enabled === false ? '自動收集未啟用' : s.status === 'READY_FOR_REVIEW' ? '樣本可供人工檢視，尚未驗收通過' : '累積樣本中'}</p>
      <p class="text-xs text-slate-400 mt-2">開始：${NBA.esc(NBA.tpe(s.activated_at))}。已保存 ${s.n_captured ?? 0} 場配對，已取得結果 ${s.n_pairs ?? 0} 場；其中實際套用校正 ${s.n_adjusted_pairs ?? 0} 場。</p>
      <p class="text-xs text-slate-400 mt-2">啟用後已完賽 ${s.n_final_since_activation ?? 0} 場，可與配對數核對缺漏。錯過賽前窗口不補算。</p>
      <p class="text-xs text-slate-400 mt-2">目前校正：${NBA.esc(shiftText)} 分。先累積資料，達到暖機條件後才啟用收縮校正。</p>
      <table class="w-full text-sm mt-3"><caption class="text-left text-xs text-slate-400 mb-2">已校正樣本的成對 MAE 差（影子 − 正式），負值表示影子誤差較小；不是獲利證明。</caption>
        <thead class="text-xs text-slate-400"><tr><th scope="col" class="text-left pb-2">目標</th><th scope="col" class="text-right pb-2">場次</th><th scope="col" class="text-right pb-2">MAE 差</th></tr></thead><tbody>${deltas}</tbody></table>
      <p class="text-xs text-slate-400 mt-3">影子版本不會自動切換上線；logistic 勝率保持原值，分差衍生機率另作配對評估。零校正樣本不算改善證據。</p>${reportTime(row)}`;
  }

  async function loadObservability() {
    observabilityRoot.innerHTML = '<p class="text-sm text-slate-400">載入本季預測與影子實驗…</p>';
    try {
      const data = await NBA.get('/api/metrics/prospective');
      const reports = data.reports || [];
      observabilityRoot.innerHTML = prospectivePanel(reports.find(r => r.key === 'prospective-model-v1')) +
        shadowPanel(reports.find(r => r.key === 'adaptive-shadow-v1'));
    } catch (e) {
      observabilityRoot.innerHTML = NBA.error(`本季績效載入失敗：${e.message}`) + '<button id="retry-model-reports" type="button" class="mt-3 text-sm underline underline-offset-4 focus-visible:outline focus-visible:outline-2">重試載入</button>';
      document.getElementById('retry-model-reports').addEventListener('click', loadObservability);
    }
  }

  function metricCard(m) {
    const cell = (label, value, cls = '') =>
      `<div><div class="text-xs text-slate-400">${label}</div><div class="text-sm font-semibold num ${cls}">${value}</div></div>`;
    // 審慎低飽和：不以顏色暗示「好」；模擬 ROI 沒有歷史台彩盤口證據時顯示「無證據」，不是 0 也不是獲利證明
    return `
    <article class="rounded-xl border border-slate-800 bg-slate-900/60 p-4">
      <header class="flex items-center justify-between mb-3">
        <h3 class="font-semibold text-sm text-slate-100">${NBA.esc(m.model_version)}</h3>
        <span class="text-xs text-slate-400">${NBA.esc(m.season || '')} · ${NBA.tpe(m.evaluated_at).slice(0, 9)}</span>
      </header>
      <div class="grid grid-cols-3 sm:grid-cols-4 gap-3">
        ${cell('勝負命中率', NBA.pct(m.accuracy))}
        ${cell('讓分 ATS', NBA.pct(m.ats_accuracy))}
        ${cell('大小分', NBA.pct(m.ou_accuracy))}
        ${cell('上半場', NBA.pct(m.h1_accuracy))}
        ${cell('模擬 ROI', m.sim_roi != null ? NBA.signed(m.sim_roi * 100, 2) + '%' : '無證據')}
        ${cell('Log Loss', NBA.fixed(m.log_loss, 4))}
        ${cell('Brier', NBA.fixed(m.brier, 4))}
        ${cell('樣本場次', m.n_games ?? '—')}
      </div>
      <div class="grid grid-cols-2 gap-3 mt-3 pt-3 border-t border-slate-800">
        ${cell('分差 MAE', NBA.fixed(m.mae_margin, 2))}
        ${cell('總分 MAE', NBA.fixed(m.mae_total, 2))}
      </div>
      ${m.notes ? `<p class="text-xs text-slate-400 mt-3">${NBA.esc(m.notes)}</p>` : ''}
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
          ${statBox('已結算投注額', NBA.money(s.total_staked))}
          ${statBox('損益', NBA.signed(s.pnl, 0), (s.pnl > 0 ? 'text-green-400' : s.pnl < 0 ? 'text-red-400' : ''))}
          ${statBox('實際 ROI', s.roi != null ? NBA.signed(s.roi * 100, 2) + '%' : '—', roiCls)}
        </div>
        ${
          curve.length
            ? `<div class="relative h-64"><canvas id="pnl-chart" role="img" aria-label="累積損益曲線：${curve.length} 筆已結算，目前累積 ${NBA.signed(curve[curve.length - 1].cumulative_pnl, 0)}">累積損益 ${NBA.signed(curve[curve.length - 1].cumulative_pnl, 0)}</canvas></div>`
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
          <i class="fas fa-lock text-2xl text-slate-400 mb-2"></i>
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
      <div class="text-xs text-slate-400 mb-0.5">${label}</div>
      <div class="text-sm font-bold num ${cls}">${value}</div>
    </div>`;
  }

  loadMetrics();
  loadObservability();
  loadPnl();
})();
