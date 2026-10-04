/**
 * 單場詳情頁 (規格書 §3.4-13)
 * 特徵拆解、逐節預測、盤口變動歷史折線圖
 */

(function () {
  const gameId = window.__GAME_ID__;
  const root = document.getElementById('game-detail');

  function header(g, p) {
    const home = NBA.teamName(g.home), away = NBA.teamName(g.away);
    return `
    <section id="game-header" class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
      <div class="flex items-center gap-2 text-xs text-slate-500 mb-2">
        <a href="/" class="hover:text-slate-300"><i class="fas fa-arrow-left mr-1"></i>返回總覽</a>
        <span>·</span><span>${NBA.tpe(g.date_utc)}</span>
        ${NBA.gameStatusBadge(g.status)}
        ${g.arena ? `<span>· ${NBA.esc(g.arena)}</span>` : ''}
        <span>· ${NBA.esc(g.season)}</span>
      </div>
      <div class="flex items-end justify-between gap-4 flex-wrap">
        <h1 class="text-xl sm:text-2xl font-bold">
          <span class="text-slate-300">${NBA.esc(away)}</span>
          <span class="text-slate-600 mx-2 text-base">@</span>
          <span class="text-white">${NBA.esc(home)}</span>
        </h1>
        ${g.score.home != null ? `<div class="num text-2xl font-bold">${g.score.away} <span class="text-slate-600">-</span> ${g.score.home}</div>` : ''}
      </div>
      ${
        p
          ? `<div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4">
        ${statBox('模型主勝率', NBA.pct(p.home_win_prob), 'text-orange-300', p.home_win_prob)}
        ${statBox('預測分差(主)', NBA.signed(p.pred_margin))}
        ${statBox('預測總分', NBA.fixed(p.pred_total))}
        ${statBox('信心度', NBA.pct(p.confidence))}
      </div>
      <p class="text-[11px] text-slate-500 mt-2">模型 ${NBA.esc(p.model_version)} · 產出時間 ${NBA.tpe(p.created_at)}</p>`
          : `<p class="mt-4 text-sm text-slate-500"><i class="fas fa-hourglass-half mr-1"></i>尚無模型預測</p>`
      }
    </section>`;
  }

  function statBox(label, value, cls = '', bar = null) {
    return `<div class="rounded-lg bg-slate-800/50 p-3">
      <div class="text-[10px] text-slate-500 mb-1">${label}</div>
      <div class="text-lg font-bold num ${cls}">${value}</div>
      ${bar != null ? `<div class="prob-bar mt-1.5"><span style="width:${((bar ?? 0) * 100).toFixed(1)}%"></span></div>` : ''}
    </div>`;
  }

  /** 逐節 / 半場預測 */
  function periodSection(g, p) {
    const s = g.score;
    const rowActual = (side) => {
      const q = s.quarters[side];
      return `<tr>
        <td class="font-medium">${NBA.esc(NBA.teamName(g[side]))} 實際</td>
        ${q.map((v) => `<td>${v ?? '—'}</td>`).join('')}
        <td>${s.quarters[`${side}_ot`] ?? '—'}</td>
        <td class="font-semibold">${s[`${side}_h1`] ?? '—'}</td>
        <td class="font-semibold">${s[`${side}_h2`] ?? '—'}</td>
        <td class="font-bold text-white">${s[side] ?? '—'}</td>
      </tr>`;
    };
    const rowPred = (side) => {
      if (!p) return '';
      const h1 = side === 'home' ? p.half1.home : p.half1.away;
      const h2 = side === 'home' ? p.half2.home : p.half2.away;
      const tot = h1 != null && h2 != null ? h1 + h2 : null;
      return `<tr class="text-orange-300/90">
        <td class="font-medium">${NBA.esc(NBA.teamName(g[side]))} 預測</td>
        <td colspan="5" class="text-slate-600 text-xs">階段二半場模型輸出（逐節預測為 Phase E 選做）</td>
        <td class="font-semibold">${NBA.fixed(h1)}</td>
        <td class="font-semibold">${NBA.fixed(h2)}</td>
        <td class="font-bold">${NBA.fixed(tot)}</td>
      </tr>`;
    };
    return `
    <section id="period-section" class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
      <h2 class="text-sm font-semibold text-slate-300 mb-3"><i class="fas fa-table-cells mr-1.5 text-slate-500"></i>逐節 / 半場</h2>
      <div class="overflow-x-auto">
        <table class="stat-table text-sm">
          <thead><tr><th>隊伍</th><th>Q1</th><th>Q2</th><th>Q3</th><th>Q4</th><th>OT</th><th>上半</th><th>下半</th><th>總分</th></tr></thead>
          <tbody>
            ${rowActual('away')}${rowActual('home')}${rowPred('away')}${rowPred('home')}
          </tbody>
        </table>
      </div>
    </section>`;
  }

  /** 特徵拆解（為什麼這樣預測） */
  function featureSection(p) {
    const f = p?.features;
    if (!f) {
      return `<section class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
        <h2 class="text-sm font-semibold text-slate-300 mb-3"><i class="fas fa-lightbulb mr-1.5 text-slate-500"></i>特徵拆解：為什麼這樣預測</h2>
        <p class="text-sm text-slate-500">尚無特徵資料。階段二會將特徵寫入 <code class="text-slate-400">predictions.features_json</code>，此區塊自動呈現。</p>
      </section>`;
    }

    const entries = Object.entries(f).filter(([k]) => k !== 'contributions');
    const contribs = Array.isArray(f.contributions) ? f.contributions : null;

    const contribBars = contribs
      ? contribs
          .slice()
          .sort((a, b) => Math.abs(b.value ?? 0) - Math.abs(a.value ?? 0))
          .map((c) => {
            const v = Number(c.value) || 0;
            const maxAbs = Math.max(...contribs.map((x) => Math.abs(Number(x.value) || 0)), 1);
            const w = (Math.abs(v) / maxAbs) * 50;
            const pos = v >= 0;
            return `
            <div class="flex items-center gap-2 text-xs py-1">
              <div class="w-32 sm:w-44 shrink-0 text-slate-400 truncate">${NBA.esc(c.label || c.name)}</div>
              <div class="flex-1 flex items-center h-4">
                <div class="w-1/2 flex justify-end"><div class="h-3 rounded-l bg-red-500/70" style="width:${pos ? 0 : w * 2}%"></div></div>
                <div class="w-px h-4 bg-slate-600"></div>
                <div class="w-1/2"><div class="h-3 rounded-r bg-green-500/70" style="width:${pos ? w * 2 : 0}%"></div></div>
              </div>
              <div class="w-14 text-right num ${pos ? 'text-green-400' : 'text-red-400'}">${NBA.signed(v, 2)}</div>
            </div>`;
          })
          .join('')
      : '';

    const kvRows = entries
      .map(([k, v]) => {
        const val = typeof v === 'object' ? JSON.stringify(v) : String(v);
        return `<tr><td class="text-slate-400 text-xs">${NBA.esc(k)}</td><td class="text-xs num">${NBA.esc(val)}</td></tr>`;
      })
      .join('');

    return `
    <section id="feature-section" class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
      <h2 class="text-sm font-semibold text-slate-300 mb-3"><i class="fas fa-lightbulb mr-1.5 text-slate-500"></i>特徵拆解：為什麼這樣預測</h2>
      ${
        contribBars
          ? `<div class="mb-4">
               <div class="flex justify-between text-[10px] text-slate-500 mb-1"><span>← 不利主隊</span><span>有利主隊 →</span></div>
               ${contribBars}
             </div>`
          : ''
      }
      <details class="text-sm">
        <summary class="cursor-pointer text-slate-400 hover:text-slate-200 text-xs">原始特徵值 (features_json)</summary>
        <div class="overflow-x-auto mt-2"><table class="stat-table">${kvRows}</table></div>
      </details>
    </section>`;
  }

  /**
   * 盤口定價（D.2）：每個來源 / bookmaker / 市場 / outcome 一列。
   * 數字全部來自 Python pricing engine（odds.pricing），這裡不計算任何機率。
   * 市場機率（原始隱含 / 去水公允）與模型機率分欄顯示，不混用；理論注碼另見 sizingSection（D.3），不是下注建議。
   */
  function oddsSection(d) {
    const pr = d.odds?.pricing;
    const markets = pr?.markets || [];
    const head = `<h2 class="text-sm font-semibold text-slate-300 mb-1"><i class="fas fa-scale-balanced mr-1.5 text-slate-500"></i>盤口定價（去水 / 模型 / Edge / EV）</h2>`;
    if (!markets.length) {
      const msg = pr?.status === 'unavailable'
        ? '定價資料表尚未建立（migration 0005）。'
        : pr?.status === 'not_priced' ? '已有盤口，尚待定價工作計算（每 5 分鐘）。' : '尚無盤口資料。';
      return `<section class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">${head}
        <p class="text-sm text-slate-500">${msg}</p></section>`;
    }
    const g = d.game;
    const sideLabel = (s) =>
      ({ home: NBA.teamName(g.home), away: NBA.teamName(g.away), draw: '和局', over: '大分', under: '小分' })[s] || s;
    const bookLabel = (m) => (m.source === 'twsport' ? '台彩' : m.bookmaker);
    const statusText = {
      market_only: '尚無有效預測（只有市場機率）',
      unsupported_settlement: '平手結算規則不明 → 不計算 EV',
      unsupported_market: '模型不支援此市場',
      rejected: '市場不完整 / 資料異常',
    };
    const pct = (v, dp = 1) => (v != null ? NBA.pct(v, dp) : '—');
    const rows = markets
      .map((m) => {
        const note = m.status !== 'priced'
          ? `<div class="text-[10px] text-amber-400/80">${NBA.esc(statusText[m.status] || m.status)}</div>`
          : (m.warnings || []).length ? `<div class="text-[10px] text-amber-400/80">${NBA.esc(m.warnings.join(', '))}</div>` : '';
        return m.outcomes
          .map((o, i) => `<tr class="${i === 0 ? 'border-t border-slate-800' : ''}">
          <td class="text-xs text-slate-400">${i === 0 ? NBA.esc(bookLabel(m)) : ''}</td>
          <td class="text-xs text-slate-300">${i === 0 ? NBA.esc(m.market_label || m.market) + note : ''}</td>
          <td class="text-xs">${NBA.esc(sideLabel(o.side))}</td>
          <td class="text-xs num">${o.display_line != null ? (m.market_type === 'total' ? NBA.fixed(o.display_line) : NBA.signed(o.display_line)) : '—'}</td>
          <td class="text-xs num">${NBA.odds(o.decimal_odds)}</td>
          <td class="text-xs num text-slate-500">${pct(o.raw_implied_prob)}</td>
          <td class="text-xs num text-slate-400">${pct(o.fair_no_vig_prob)}</td>
          <td class="text-xs num text-orange-300">${pct(o.model_prob)}</td>
          <td class="text-xs num text-slate-500">${o.push_prob ? pct(o.push_prob) : o.model_prob != null ? '0' : '—'}</td>
          <td class="text-right">${o.edge_vs_fair != null ? NBA.edgeBadge(NBA.edgeTierOf(o.edge_vs_fair), NBA.signed(o.edge_vs_fair * 100, 1) + '%') : '—'}</td>
          <td class="text-xs num text-right ${o.ev_per_unit > 0 ? 'text-green-400' : 'text-slate-400'}">${o.ev_per_unit != null ? NBA.signed(o.ev_per_unit * 100, 1) + '%' : '—'}</td>
          <td class="text-xs num text-right text-slate-500">${i === 0 ? pct(m.market_overround) : ''}</td>
        </tr>`)
          .join('');
      })
      .join('');

    return `
    <section id="odds-section" class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
      ${head}
      <p class="text-[11px] text-slate-500 mb-3">
        原始隱含 = 1/賠率（含水）；去水公允 = 同一盤口全部選項一起比例去水（${NBA.esc(pr.no_vig_method)}）；
        模型 = C.5E 分佈的 P(勝)；Push = 整數線退款機率；Edge = 模型 − 去水公允；EV = 以實際賠率每投注 1 單位的期望值。
        各 bookmaker 分開計算，不平均、不挑最佳盤；本表不是投注建議。
      </p>
      <div class="overflow-x-auto">
        <table class="stat-table text-sm">
          <thead><tr><th>來源</th><th>玩法</th><th>選項</th><th>盤線</th><th>賠率</th><th>原始隱含</th><th>去水公允</th><th>模型</th><th>Push</th><th class="text-right">Edge</th><th class="text-right">EV</th><th class="text-right">抽水</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </section>`;
  }

  /**
   * 理論注碼（D.3 sizing）：每個已計算 sizing 的 outcome 一列。
   * Kelly / ¼ Kelly / 單筆上限 / 同場與單日縮放全部由 Python（bet_sizing_snapshots）計算，這裡只顯示、不計算、不排序。
   * 用語中性：理論注碼 / risk-adjusted stake；不是投注建議、不是推薦清單。
   */
  const SIZING_STATUS = {
    eligible: ['合格', 'low'],
    exposure_scaled: ['合格（exposure 縮放）', 'low'],
    no_positive_ev: ['EV ≤ 0', 'none'],
    stale_quote: ['報價過舊', 'none'],
    unsupported_settlement: ['結算不支援', 'none'],
    market_not_open: ['未開盤 / 已開賽', 'none'],
    no_prediction: ['無模型機率', 'none'],
    invalid_probability: ['機率異常', 'none'],
    mutually_exclusive_positive_kelly: ['互斥 outcome 同時為正（拒絕）', 'none'],
    unavailable: ['不可用', 'none'],
  };

  function sizingSection(d) {
    const sz = d.odds?.sizing;
    const markets = d.odds?.pricing?.markets || [];
    const head = `<h2 class="text-sm font-semibold text-slate-300 mb-1"><i class="fas fa-shield-halved mr-1.5 text-slate-500"></i>理論注碼（risk-adjusted stake，bankroll 比例）</h2>`;
    const rowsData = markets.flatMap((m) => m.outcomes.filter((o) => o.sizing).map((o) => ({ m, o, s: o.sizing })));
    if (!sz || !rowsData.length) {
      const msg = sz?.status === 'unavailable'
        ? 'sizing 資料表尚未建立（migration 0006）。'
        : '尚無 sizing 結果（定價後由排程計算）。';
      return `<section class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">${head}
        <p class="text-sm text-slate-500">${msg}</p></section>`;
    }
    const g = d.game;
    const sideLabel = (x) =>
      ({ home: NBA.teamName(g.home), away: NBA.teamName(g.away), draw: '和局', over: '大分', under: '小分' })[x] || x;
    const bookLabel = (m) => (m.source === 'twsport' ? '台彩' : m.bookmaker);
    const pct = (v, dp = 2) => (v != null ? NBA.pct(v, dp) : '—');
    const pol = sz.policy || {};
    const ge = sz.game_exposure || {};
    const de = sz.daily_exposure || {};
    const rows = rowsData
      .map(({ m, o, s }) => {
        const [label, cls] = SIZING_STATUS[s.qualification_status] || [s.qualification_status, 'none'];
        const notes = [...(s.reasons || []), ...(s.warnings || [])].join(', ');
        return `<tr>
          <td class="text-xs text-slate-400">${NBA.esc(bookLabel(m))}</td>
          <td class="text-xs text-slate-300">${NBA.esc(m.market_label || m.market)}</td>
          <td class="text-xs">${NBA.esc(sideLabel(o.side))}</td>
          <td class="text-xs num">${NBA.odds(s.decimal_odds)}</td>
          <td class="text-xs num text-slate-400">${pct(s.p_win, 1)} / ${pct(s.p_push, 1)} / ${pct(s.p_loss, 1)}</td>
          <td class="text-xs num text-right ${s.ev_per_unit > 0 ? 'text-green-400' : 'text-slate-400'}">${s.ev_per_unit != null ? NBA.signed(s.ev_per_unit * 100, 1) + '%' : '—'}</td>
          <td class="text-xs num text-right text-slate-400">${pct(s.full_kelly_fraction)}</td>
          <td class="text-xs num text-right text-slate-400">${pct(s.fractional_kelly_fraction)}</td>
          <td class="text-xs num text-right text-slate-400">${pct(s.single_bet_capped_fraction)}</td>
          <td class="text-xs num text-right ${s.actionable ? 'text-orange-300 font-semibold' : 'text-slate-600'}">${pct(s.final_stake_fraction)}</td>
          <td class="text-right">${NBA.edgeBadge(cls, label)}</td>
          <td class="text-[10px] text-slate-500 max-w-[16rem]">${NBA.esc(notes)}</td>
        </tr>`;
      })
      .join('');
    return `
    <section id="sizing-section" class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
      ${head}
      <p class="text-[11px] text-slate-500 mb-2">
        ${NBA.esc(pol.risk_policy_version || sz.risk_policy_version)}：push-aware full Kelly（只用 P(勝)、P(退款)、P(輸) 與實際賠率，不用 edge）
        → Kelly × ${NBA.fixed(pol.kelly_multiplier, 2)} → 單筆上限 ${pct(pol.max_bet_fraction, 0)}
        → 同場合計上限 ${pct(pol.max_game_fraction, 0)} → 單日（台灣日期）合計上限 ${pct(pol.max_day_fraction, 0)}，超過時等比例縮放。
        資料品質旗標只顯示警示、不調整注碼。這是理論計算，不是投注建議。
      </p>
      <div class="flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-slate-400 mb-3">
        <span>betting day <span class="num text-slate-200">${NBA.esc(sz.betting_day || '—')}</span></span>
        <span>同場 exposure <span class="num text-slate-200">${pct(ge.before)} → ${pct(ge.after)}</span>（縮放 × ${NBA.fixed(ge.scale_factor, 3)}）</span>
        <span>單日 exposure <span class="num text-slate-200">${pct(de.before)} → ${pct(de.after)}</span>（縮放 × ${NBA.fixed(de.scale_factor, 3)}）</span>
        <span class="text-slate-600">計算時點 ${NBA.esc(NBA.tpe(sz.analysis_as_of))}</span>
      </div>
      <div class="overflow-x-auto">
        <table class="stat-table text-sm">
          <thead><tr><th>來源</th><th>玩法</th><th>選項</th><th>賠率</th><th>P(勝 / 退 / 輸)</th><th class="text-right">EV</th><th class="text-right">Full Kelly</th><th class="text-right">分數 Kelly</th><th class="text-right">單筆上限後</th><th class="text-right">最終（風險調整）</th><th class="text-right">資格</th><th>原因 / 警示</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </section>`;
  }

  /** 盤口變動歷史折線圖 */
  function oddsChartSection() {
    return `
    <section id="odds-chart-section" class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
      <div class="flex items-center justify-between mb-3 gap-2 flex-wrap">
        <h2 class="text-sm font-semibold text-slate-300"><i class="fas fa-chart-line mr-1.5 text-slate-500"></i>盤口變動歷史</h2>
        <select id="chart-market" class="bg-slate-800 border border-slate-700 rounded px-2 py-1 text-xs">
          <option value="spread">讓分盤線</option>
          <option value="total">大小分盤線</option>
          <option value="ml">獨贏賠率</option>
          <option value="h1_spread">上半場讓分</option>
          <option value="h1_total">上半場大小</option>
        </select>
      </div>
      <div class="relative h-64"><canvas id="odds-chart"></canvas></div>
      <p id="chart-empty" class="text-sm text-slate-500 hidden">此玩法尚無盤口快照歷史。</p>
    </section>`;
  }

  let chart = null;
  function drawChart(seriesAll, market) {
    const canvas = document.getElementById('odds-chart');
    const emptyEl = document.getElementById('chart-empty');
    const series = seriesAll.filter((s) => s.market === market);
    if (chart) { chart.destroy(); chart = null; }
    if (!series.length) {
      canvas.classList.add('hidden');
      emptyEl.classList.remove('hidden');
      return;
    }
    canvas.classList.remove('hidden');
    emptyEl.classList.add('hidden');

    const colors = { twsport: '#f97316', oddsapi: '#38bdf8', pinnacle: '#a78bfa' };
    const datasets = [];
    for (const s of series) {
      const useLine = market !== 'ml';
      if (useLine) {
        datasets.push({
          label: `${s.source === 'twsport' ? '台彩' : '國際盤'} 盤線`,
          data: s.points.map((p) => ({ x: p.t, y: p.line })),
          borderColor: colors[s.source] || '#94a3b8',
          backgroundColor: 'transparent',
          tension: 0.25,
          pointRadius: 2,
        });
      } else {
        datasets.push({
          label: `${s.source === 'twsport' ? '台彩' : '國際盤'} 主隊賠率`,
          data: s.points.map((p) => ({ x: p.t, y: p.home_odds })),
          borderColor: colors[s.source] || '#94a3b8',
          backgroundColor: 'transparent',
          tension: 0.25,
          pointRadius: 2,
        });
        datasets.push({
          label: `${s.source === 'twsport' ? '台彩' : '國際盤'} 客隊賠率`,
          data: s.points.map((p) => ({ x: p.t, y: p.away_odds })),
          borderColor: colors[s.source] || '#94a3b8',
          borderDash: [4, 3],
          backgroundColor: 'transparent',
          tension: 0.25,
          pointRadius: 2,
        });
      }
    }

    chart = new Chart(canvas, {
      type: 'line',
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: {
            type: 'category',
            labels: [...new Set(series.flatMap((s) => s.points.map((p) => p.t)))].sort(),
            ticks: { color: '#64748b', font: { size: 10 }, callback: function (v) { return NBA.tpe(this.getLabelForValue(v)); } },
            grid: { color: '#1e293b' },
          },
          y: { ticks: { color: '#64748b', font: { size: 10 } }, grid: { color: '#1e293b' } },
        },
        plugins: { legend: { labels: { color: '#94a3b8', font: { size: 11 }, boxWidth: 12 } } },
      },
    });

    // Chart.js category 軸需要 x 為 label 值
    chart.data.datasets.forEach((ds) => {
      ds.data = ds.data.map((pt) => ({ x: pt.x, y: pt.y }));
    });
    chart.update();
  }

  /** 近況 & H2H */
  function formSection(d) {
    const g = d.game;
    const row = (game, teamId) => {
      const isHome = game.home.id === teamId;
      const my = isHome ? game.score.home : game.score.away;
      const opp = isHome ? game.score.away : game.score.home;
      const oppTeam = isHome ? game.away : game.home;
      const win = my != null && opp != null && my > opp;
      return `<tr>
        <td class="text-xs text-slate-500">${NBA.tpe(game.date_utc).slice(0, 9)}</td>
        <td class="text-xs">${isHome ? '主' : '客'} vs ${NBA.esc(oppTeam.abbr)}</td>
        <td class="text-xs num">${my ?? '—'}-${opp ?? '—'}</td>
        <td class="text-xs font-semibold ${win ? 'text-green-400' : 'text-red-400'}">${my == null ? '—' : win ? 'W' : 'L'}</td>
      </tr>`;
    };
    const recordOf = (games, teamId) => {
      let w = 0, l = 0;
      for (const gm of games) {
        const isHome = gm.home.id === teamId;
        const my = isHome ? gm.score.home : gm.score.away;
        const opp = isHome ? gm.score.away : gm.score.home;
        if (my == null || opp == null) continue;
        my > opp ? w++ : l++;
      }
      return `${w}勝${l}敗`;
    };

    const h2hRows = d.head_to_head
      .map(
        (gm) => `<tr>
        <td class="text-xs text-slate-500">${NBA.tpe(gm.date_utc).slice(0, 9)}</td>
        <td class="text-xs">${NBA.esc(gm.away.abbr)} @ ${NBA.esc(gm.home.abbr)}</td>
        <td class="text-xs num">${gm.score.away ?? '—'} - ${gm.score.home ?? '—'}</td>
        <td class="text-xs num text-slate-400">${gm.score.home != null && gm.score.away != null ? gm.score.home + gm.score.away : '—'}</td>
      </tr>`
      )
      .join('');

    return `
    <div class="grid gap-5 lg:grid-cols-3 mb-5">
      <section class="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
        <h2 class="text-sm font-semibold text-slate-300 mb-1">${NBA.esc(NBA.teamName(g.away))} 近況</h2>
        <p class="text-xs text-slate-500 mb-2">近 ${d.recent_form.away.length} 場 ${recordOf(d.recent_form.away, g.away.id)}</p>
        <table class="stat-table">${d.recent_form.away.map((gm) => row(gm, g.away.id)).join('') || '<tr><td class="text-xs text-slate-600">無資料</td></tr>'}</table>
      </section>
      <section class="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
        <h2 class="text-sm font-semibold text-slate-300 mb-1">${NBA.esc(NBA.teamName(g.home))} 近況</h2>
        <p class="text-xs text-slate-500 mb-2">近 ${d.recent_form.home.length} 場 ${recordOf(d.recent_form.home, g.home.id)}</p>
        <table class="stat-table">${d.recent_form.home.map((gm) => row(gm, g.home.id)).join('') || '<tr><td class="text-xs text-slate-600">無資料</td></tr>'}</table>
      </section>
      <section class="rounded-xl border border-slate-800 bg-slate-900/60 p-5">
        <h2 class="text-sm font-semibold text-slate-300 mb-1">歷史對戰 H2H</h2>
        <p class="text-xs text-slate-500 mb-2">最近 ${d.head_to_head.length} 次交手</p>
        <table class="stat-table">
          ${h2hRows || '<tr><td class="text-xs text-slate-600">無資料</td></tr>'}
        </table>
      </section>
    </div>`;
  }

  /** 傷病 */
  function injurySection(d) {
    if (!d.injuries?.length) return '';
    const rows = d.injuries
      .map(
        (i) => `<tr>
        <td class="text-xs">${NBA.esc(i.team_abbr || '—')}</td>
        <td class="text-xs">${NBA.esc(i.player_name || '—')}${i.is_starter ? ' <span class="text-[10px] text-amber-400">(主力)</span>' : ''}</td>
        <td>${NBA.statusBadge(i.status)}</td>
        <td class="text-xs text-slate-400">${NBA.esc(i.reason || '—')}</td>
        <td class="text-xs text-slate-500">${NBA.tpe(i.report_time_utc)}</td>
      </tr>`
      )
      .join('');
    return `
    <section id="injury-section" class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
      <h2 class="text-sm font-semibold text-slate-300 mb-3"><i class="fas fa-kit-medical mr-1.5 text-slate-500"></i>本場傷病報告</h2>
      <div class="overflow-x-auto"><table class="stat-table text-sm">
        <thead><tr><th>球隊</th><th>球員</th><th>狀態</th><th>原因</th><th>申報時間</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
    </section>`;
  }

  /** 球隊數據 */
  function teamStatsSection(d) {
    if (!d.team_game_stats?.length) return '';
    const keys = [
      ['pts', '得分'], ['fg_pct', 'FG%'], ['fg3_pct', '3P%'], ['ft_pct', 'FT%'],
      ['reb', '籃板'], ['ast', '助攻'], ['tov', '失誤'],
      ['pace', 'Pace'], ['off_rtg', 'ORtg'], ['def_rtg', 'DRtg'],
    ];
    const head = d.team_game_stats.map((s) => `<th class="text-right">${NBA.esc(s.name_zh || s.abbr)}</th>`).join('');
    const rows = keys
      .map(
        ([k, label]) => `<tr><td class="text-xs text-slate-400">${label}</td>${d.team_game_stats
          .map((s) => `<td class="text-xs num text-right">${s[k] != null ? (String(k).includes('pct') ? (s[k] * 100).toFixed(1) + '%' : Number(s[k]).toFixed(k === 'pace' || k.includes('rtg') ? 1 : 0)) : '—'}</td>`)
          .join('')}</tr>`
      )
      .join('');
    return `
    <section id="team-stats-section" class="rounded-xl border border-slate-800 bg-slate-900/60 p-5 mb-5">
      <h2 class="text-sm font-semibold text-slate-300 mb-3"><i class="fas fa-chart-simple mr-1.5 text-slate-500"></i>本場球隊數據</h2>
      <div class="overflow-x-auto"><table class="stat-table text-sm">
        <thead><tr><th>項目</th>${head}</tr></thead><tbody>${rows}</tbody>
      </table></div>
    </section>`;
  }

  async function load() {
    root.innerHTML = `<div class="grid gap-4">${NBA.skeletonCards(3)}</div>`;
    try {
      const d = await NBA.get(`/api/games/${gameId}`);
      const p = d.prediction;
      root.innerHTML =
        header(d.game, p) +
        oddsSection(d) +
        sizingSection(d) +
        periodSection(d.game, p) +
        featureSection(p) +
        oddsChartSection() +
        injurySection(d) +
        teamStatsSection(d) +
        formSection(d);

      const sel = document.getElementById('chart-market');
      const series = d.odds_history?.length
        ? (await NBA.get(`/api/odds/${gameId}`)).series
        : [];
      drawChart(series, sel.value);
      sel.addEventListener('change', () => drawChart(series, sel.value));
    } catch (e) {
      root.innerHTML = NBA.error(`載入賽事詳情失敗：${e.message}`);
    }
  }

  load();
})();
