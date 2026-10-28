/**
 * 賽事總覽頁 (規格書 §3.4-12)
 * 每場卡片：對戰、台灣時間、模型勝率、預測分差/總分、上半場預測、
 *           台彩盤口 vs 國際盤 vs 模型、edge 標示
 */

(function () {
  const listEl = document.getElementById('games-list');
  const dateLabel = document.getElementById('date-label');
  const summaryEl = document.getElementById('games-summary');
  const tabs = document.querySelectorAll('[data-day-tab]');
  const datePicker = document.getElementById('date-picker');

  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const urlDate = new URLSearchParams(location.search).get('date');
  let currentDate = DATE_RE.test(urlDate || '') ? urlDate : NBA.tpeDateStr(1); // 預設隔日（規格書主要用途）

  function marketRow(label, twOdds, intlOdds, modelText, edge) {
    return `
      <tr>
        <th scope="row" class="text-slate-400 text-xs font-normal whitespace-nowrap">${NBA.esc(label)}</th>
        <td class="text-xs">${twOdds}</td>
        <td class="text-xs text-slate-400">${intlOdds}</td>
        <td class="text-xs text-slate-100">${modelText}</td>
        <td class="text-right">${edge}</td>
      </tr>`;
  }

  function renderCard(g) {
    const p = g.prediction;
    const tw = g.odds?.twsport || {};
    const intl = g.odds?.international || {};
    const edges = g.odds?.edges || [];
    const edgeBy = (m) => edges.find((e) => e.market === m);

    const hwp = p?.home_win_prob;
    const homeName = NBA.teamName(g.home);
    const awayName = NBA.teamName(g.away);

    const sideLabel = (sel) =>
      sel === 'home' ? homeName : sel === 'away' ? awayName : sel === 'over' ? '大分' : '小分';
    // D.2：edge = 模型 − 去水公允機率（Python 定價結果）；讓分 / 大小分不再顯示線差
    const edgeText = (e) => `${sideLabel(e.selection)} ${NBA.signed((e.edge ?? 0) * 100, 1)}%`;
    // 獨贏模型機率：分差分佈導出（與定價一致）；尚未定價時退回邏輯迴歸勝率
    const twMl = (g.odds?.pricing?.markets || []).find((m) => m.source === 'twsport' && m.market === 'ml' && m.status === 'priced');
    const mlHome = twMl ? twMl.outcomes.find((o) => o.side === 'home')?.model_prob : null;

    // 盤口比較列
    const rows = [];
    const mlEdge = edgeBy('ml');
    rows.push(
      marketRow(
        '不讓分',
        tw.ml ? `${NBA.esc(awayName)} ${NBA.odds(tw.ml.away_odds)} / ${NBA.esc(homeName)} ${NBA.odds(tw.ml.home_odds)}` : '<span class="text-slate-400">無盤口</span>',
        intl.ml ? `${NBA.odds(intl.ml.away_odds)} / ${NBA.odds(intl.ml.home_odds)}` : '—',
        mlHome != null ? `主 ${NBA.pct(mlHome)}` : hwp != null ? `主 ${NBA.pct(hwp)}` : '—',
        mlEdge ? NBA.edgeBadge(mlEdge.tier, edgeText(mlEdge)) : ''
      )
    );
    const spEdge = edgeBy('spread');
    rows.push(
      marketRow(
        '讓分',
        tw.spread ? `${NBA.signed(tw.spread.line)} @ ${NBA.odds(tw.spread.home_odds)}` : '<span class="text-slate-400">無盤口</span>',
        intl.spread ? `${NBA.signed(intl.spread.line)}` : '—',
        p?.pred_margin != null ? `主隊 ${NBA.signed(p.pred_margin)}` : '—',
        spEdge ? NBA.edgeBadge(spEdge.tier, edgeText(spEdge)) : ''
      )
    );
    const toEdge = edgeBy('total');
    rows.push(
      marketRow(
        '大小分',
        tw.total ? `${NBA.fixed(tw.total.line)} (大 ${NBA.odds(tw.total.over_odds)}/小 ${NBA.odds(tw.total.under_odds)})` : '<span class="text-slate-400">無盤口</span>',
        intl.total ? NBA.fixed(intl.total.line) : '—',
        p?.pred_total != null ? NBA.fixed(p.pred_total) : '—',
        toEdge ? NBA.edgeBadge(toEdge.tier, edgeText(toEdge)) : ''
      )
    );
    const h1sEdge = edgeBy('h1_spread');
    if (tw.h1_spread || p?.half1?.margin != null) {
      rows.push(
        marketRow(
          '上半場讓分',
          tw.h1_spread ? `${NBA.signed(tw.h1_spread.line)} @ ${NBA.odds(tw.h1_spread.home_odds)}` : '<span class="text-slate-400">無盤口</span>',
          '—',
          p?.half1?.margin != null ? `主隊 ${NBA.signed(p.half1.margin)}` : '—',
          h1sEdge ? NBA.edgeBadge(h1sEdge.tier, edgeText(h1sEdge)) : ''
        )
      );
    }
    const h1tEdge = edgeBy('h1_total');
    if (tw.h1_total || p?.half1?.total != null) {
      rows.push(
        marketRow(
          '上半場大小',
          tw.h1_total ? `${NBA.fixed(tw.h1_total.line)} (大 ${NBA.odds(tw.h1_total.over_odds)}/小 ${NBA.odds(tw.h1_total.under_odds)})` : '<span class="text-slate-400">無盤口</span>',
          '—',
          p?.half1?.total != null ? NBA.fixed(p.half1.total) : '—',
          h1tEdge ? NBA.edgeBadge(h1tEdge.tier, edgeText(h1tEdge)) : ''
        )
      );
    }

    const scoreBlock =
      g.status !== 'scheduled' && g.score.home != null
        ? `<div class="text-right num"><span class="text-lg font-bold">${g.score.away} - ${g.score.home}</span></div>`
        : '';

    return `
    <article class="rounded-xl border border-slate-800 bg-slate-900/60 hover:border-slate-700 transition overflow-hidden">
      <header class="flex items-start justify-between gap-3 px-4 pt-3.5 pb-2">
        <div class="min-w-0">
          <div class="flex items-center gap-2 text-xs text-slate-400 mb-1">
            <span>${NBA.tpe(g.date_utc)}</span>
            ${NBA.gameStatusBadge(g.status)}
            ${g.arena ? `<span class="hidden sm:inline truncate">· ${NBA.esc(g.arena)}</span>` : ''}
          </div>
          <a href="/games/${g.id}" class="block group">
            <div class="text-[15px] font-semibold truncate group-hover:text-orange-300 transition">
              <span class="text-slate-300">${NBA.esc(awayName)}</span>
              <span class="text-slate-400 mx-1">@</span>
              <span class="text-white">${NBA.esc(homeName)}</span>
            </div>
          </a>
        </div>
        <div class="shrink-0 text-right">
          ${scoreBlock}
        </div>
      </header>

      <div class="px-4 pb-3">
        ${
          p
            ? `
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-2.5 mb-3">
          <div class="rounded-lg bg-slate-800/50 p-2">
            <div class="text-xs text-slate-400 mb-0.5">模型主勝率</div>
            <div class="text-base font-bold num">${NBA.pct(hwp)}</div>
            <div class="prob-bar mt-1" aria-hidden="true"><span style="transform:scaleX(${Math.min(1, Math.max(0, hwp ?? 0)).toFixed(3)})"></span></div>
          </div>
          <div class="rounded-lg bg-slate-800/50 p-2">
            <div class="text-xs text-slate-400 mb-0.5">預測分差(主)</div>
            <div class="text-base font-bold num">${NBA.signed(p.pred_margin)}</div>
          </div>
          <div class="rounded-lg bg-slate-800/50 p-2">
            <div class="text-xs text-slate-400 mb-0.5">預測總分</div>
            <div class="text-base font-bold num">${NBA.fixed(p.pred_total)}</div>
          </div>
          <div class="rounded-lg bg-slate-800/50 p-2">
            <div class="text-xs text-slate-400 mb-0.5">信心度</div>
            <div class="text-base font-bold num">${NBA.pct(p.confidence)}</div>
          </div>
        </div>
        <div class="text-xs text-slate-400 mb-2.5 flex flex-wrap gap-x-4 gap-y-1">
          <span>上半場預測：<span class="num text-slate-200">${NBA.fixed(p.half1.away)} - ${NBA.fixed(p.half1.home)}</span>
            (分差 ${NBA.signed(p.half1.margin)} / 總分 ${NBA.fixed(p.half1.total)})</span>
          <span>下半場：<span class="num text-slate-200">${NBA.fixed(p.half2.away)} - ${NBA.fixed(p.half2.home)}</span></span>
          <span class="text-slate-400">模型 ${NBA.esc(p.model_version)}</span>
        </div>`
            : `<div class="rounded-lg border border-dashed border-slate-700 p-3 mb-3 text-xs text-slate-400">
                 <i class="fas fa-hourglass-half mr-1"></i>尚無模型預測（階段二預測引擎上線後自動顯示）
               </div>`
        }

        <div class="overflow-x-auto -mx-1 px-1">
        <table class="stat-table text-sm">
          <caption class="sr-only">${NBA.esc(awayName)} @ ${NBA.esc(homeName)} 盤口比較</caption>
          <thead>
            <tr><th scope="col">玩法</th><th scope="col">台彩盤口</th><th scope="col">國際盤</th><th scope="col">模型</th><th scope="col" class="text-right">模型差距（診斷）</th></tr>
          </thead>
          <tbody>${rows.join('')}</tbody>
        </table>
        </div>
      </div>

      <footer class="px-4 py-2 border-t border-slate-800 bg-slate-900/40 flex justify-between items-center">
        <a href="/games/${g.id}" class="text-xs text-orange-300 hover:text-orange-200 inline-flex items-center min-h-[2.75rem] sm:min-h-0">
          查看詳情 <i class="fas fa-arrow-right ml-0.5 text-xs" aria-hidden="true"></i>
        </a>
        <a href="/decision?date=${encodeURIComponent(currentDate)}" class="text-xs text-slate-300 hover:text-slate-100 inline-flex items-center min-h-[2.75rem] sm:min-h-0">
          <i class="fas fa-compass mr-1" aria-hidden="true"></i>能不能下、下多少 → 決策中心
        </a>
      </footer>
    </article>`;
  }

  function renderSummary(games) {
    if (!summaryEl) return;
    const withPred = games.filter((g) => g.prediction).length;
    const allEdges = games.flatMap((g) => (g.odds?.edges || []).filter((e) => e.tier && e.tier !== 'none'));
    const strong = allEdges.filter((e) => e.tier === 'high' || e.tier === 'mid').length;
    summaryEl.innerHTML = `
      <div class="flex flex-wrap gap-4 text-xs text-slate-400">
        <span><i class="fas fa-basketball mr-1 text-slate-400"></i>賽事 <span class="text-slate-100 font-semibold num">${games.length}</span></span>
        <span><i class="fas fa-brain mr-1 text-slate-400"></i>已有預測 <span class="text-slate-100 font-semibold num">${withPred}</span></span>
        <span><i class="fas fa-scale-balanced mr-1 text-slate-400" aria-hidden="true"></i>模型差距 ≥ 3% 的選項（市場分歧，非推薦） <span class="text-slate-100 font-semibold num">${strong}</span></span>
      </div>
      <p class="text-xs text-slate-400 mt-2">模型差距 = 模型機率 − 台彩去水公允機率，只用來看市場分歧；能不能下、能下多少只看<a href="/decision?date=${encodeURIComponent(currentDate)}" class="text-orange-300 underline underline-offset-2">決策中心</a>（已扣除你實際下注的額度）。</p>`;
  }

  function syncDate(date) {
    tabs.forEach((t) => {
      const on = NBA.tpeDateStr(Number(t.dataset.dayTab)) === date;
      t.classList.toggle('bg-orange-500/15', on);
      t.classList.toggle('text-orange-300', on);
      t.classList.toggle('font-medium', on);
      t.classList.toggle('text-slate-400', !on);
      t.setAttribute('aria-pressed', String(on));
    });
    const url = new URL(location.href);
    if (url.searchParams.get('date') !== date) { url.searchParams.set('date', date); history.replaceState(history.state, '', url); }
  }

  async function load(date) {
    currentDate = date;
    syncDate(date);
    if (dateLabel) dateLabel.textContent = date;
    if (datePicker) datePicker.value = date;
    listEl.innerHTML = `<div class="grid gap-4 lg:grid-cols-2">${NBA.skeletonCards(4)}</div>`;
    if (summaryEl) summaryEl.innerHTML = '';
    try {
      const data = await NBA.get(`/api/games?date=${encodeURIComponent(date)}`);
      const games = data.games || [];
      if (!games.length) {
        listEl.innerHTML = NBA.empty(
          `${date}（台灣時間）沒有賽事資料。若為賽季中，請確認階段二的賽程抓取排程是否正常。`,
          'fa-calendar-xmark'
        );
        renderSummary([]);
        return;
      }
      renderSummary(games);
      listEl.innerHTML = `<div class="grid gap-4 lg:grid-cols-2">${games.map(renderCard).join('')}</div>`;
    } catch (e) {
      listEl.innerHTML = NBA.error(`載入賽事失敗：${e.message}`);
    }
  }

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => load(NBA.tpeDateStr(Number(tab.dataset.dayTab))));
  });

  if (datePicker) {
    datePicker.addEventListener('change', () => {
      if (datePicker.value) load(datePicker.value);
    });
  }

  load(currentDate);
})();
