/**
 * 今日決策中心（Phase D.5）
 * ------------------------------------------------------------
 * 只顯示 /api/decision-board（Python 物化）的結果：EV / Kelly / exposure / 剩餘額度全部由 Python 計算，
 * 這裡只做分組、排序後的呈現與格式化（不做任何風險數學）。
 * 三個概念分開呈現：模型機會（台彩）/ paper decision（T-60 前瞻驗證）/ 實際下注（使用者記錄）。
 * 平台不會自動下注：使用者在台灣運彩自行下注後，回來按「記錄下注」。
 */

(function () {
  const $ = (id) => document.getElementById(id);
  const banner = $('decision-banner');
  const summaryEl = $('decision-summary');
  const gamesEl = $('decision-games');
  const evidenceEl = $('decision-evidence');
  const healthEl = $('decision-health');
  const modalRoot = $('record-modal-root');
  const picker = $('decision-date');
  const tabs = document.querySelectorAll('[data-dday]');
  let currentDate = NBA.tpeDateStr(0);
  let board = null;

  const MARKET = { ml: '不讓分', spread: '讓分', total: '大小分', h1_ml: '上半場獨贏', h1_spread: '上半場讓分', h1_total: '上半場大小' };
  const GROUP = {
    actionable: ['fa-circle-check', 'grp-actionable', 'ACTIONABLE', '可操作'],
    review: ['fa-magnifying-glass', 'grp-review', 'REVIEW', '需檢視'],
    recorded: ['fa-receipt', 'grp-recorded', 'RECORDED', '已記錄'],
    blocked: ['fa-ban', 'grp-blocked', 'BLOCKED', '受阻'],
    inactive: ['fa-minus', 'grp-inactive', 'NO EDGE', '無正 EV'],
    diagnostic: ['fa-globe', 'grp-diagnostic', 'DIAGNOSTIC', '國際盤診斷'],
  };
  const HEALTH = {
    healthy: ['fa-circle-check', 'text-green-400', '正常'],
    waiting: ['fa-hourglass-half', 'text-slate-300', '等待中'],
    stale: ['fa-clock', 'text-amber-400', '過期'],
    blocked: ['fa-ban', 'text-red-400', '受阻'],
    not_configured: ['fa-plug', 'text-slate-400', '未設定'],
    error: ['fa-circle-xmark', 'text-red-400', '錯誤'],
  };
  const TW_ICON = {
    available: 'fa-circle-check', stale: 'fa-clock', market_closed: 'fa-lock', not_yet_published: 'fa-hourglass-half',
    ingestion_unavailable: 'fa-shield-halved', not_configured: 'fa-plug', outside_window: 'fa-calendar', game_started: 'fa-flag-checkered',
  };

  const esc = (s) => NBA.esc(s);
  const amt = (v, cur) => (v == null ? '—' : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })}${cur ? ' ' + esc(cur) : ''}`);
  const pct = (v, d = 2) => NBA.pct(v, d);
  const evText = (v) => (v == null ? '—' : NBA.signed(v * 100, 1) + '%');
  const groupBadge = (g, extra) => {
    const [icon, cls, en, zh] = GROUP[g] || ['fa-circle-question', 'grp-inactive', g || '—', ''];
    return `<span class="grp-badge ${cls}" title="${esc(extra || zh)}"><i class="fas ${icon}"></i>${en}<span class="opacity-70 font-normal">${zh}</span></span>`;
  };
  const sideText = (o, g) => {
    const s = o.side;
    const name = s === 'home' ? (g.home.name_zh || g.home.abbr) : s === 'away' ? (g.away.name_zh || g.away.abbr)
      : s === 'over' ? '大分' : s === 'under' ? '小分' : s === 'draw' ? '和局' : s;
    const line = o.display_line != null && o.market_type !== 'moneyline' ? ` ${o.market_type === 'spread' ? NBA.signed(o.display_line) : NBA.fixed(o.display_line)}` : '';
    return `${esc(name)}${line}`;
  };
  const ago = (iso) => {
    if (!iso) return '—';
    const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    return NBA.ago(m);
  };

  /* --------------------------- summary --------------------------- */

  function renderBanner(d) {
    const msgs = [];
    const rs = d.risk_state || {};
    if (d.status === 'unavailable') msgs.push(['red', 'fa-database', '決策中心資料表尚未建立（migration 0008 未套用）。下方僅顯示賽程。']);
    else if (!d.materialized && d.games.length) msgs.push(['amber', 'fa-hourglass-half', '決策檢視尚未由 pipeline 產生（排程每分鐘物化）。目前不顯示任何可操作額度。']);
    if (rs.status === 'recalculation_pending') msgs.push(['amber', 'fa-rotate', '剛記錄的下注 / bankroll 異動尚未納入風險計算（risk recalculation pending）。額度暫不顯示，約一分鐘內更新。']);
    if (rs.materialization_stale) msgs.push(['red', 'fa-triangle-exclamation', `決策物化已超過 10 分鐘未更新（最後確認 ${ago(d.last_confirmed_at)}）— 系統降級，額度暫不視為有效。`]);
    if (d.bankroll && d.bankroll.configured === false) msgs.push(['slate', 'fa-wallet', '尚未設定 strategy bankroll：只顯示理論比例，不產生可操作金額。<a href="/bankroll" class="underline">前往設定</a>']);
    if (d.actual_exposure && d.actual_exposure.incomplete) msgs.push(['red', 'fa-circle-exclamation', '實際下注資料不完整：actual exposure may be incomplete，暫停新的可操作額度。']);
    if (d.actual_exposure && d.actual_exposure.day_over_limit) msgs.push(['red', 'fa-gauge-high', `當日實際 exposure ${pct(d.actual_exposure.day_fraction)} 已超過 risk-v1 上限 ${pct(d.actual_exposure.max_day_fraction, 0)}；剩餘 0%（不修改已下注紀錄）。`]);
    const health = d.system_health || {};
    if (health.overall === 'degraded') msgs.push(['amber', 'fa-heart-pulse', '部分資料來源異常（見 System readiness 面板）。']);
    const cls = { red: 'border-red-900/60 bg-red-950/30 text-red-200', amber: 'border-amber-900/60 bg-amber-950/20 text-amber-200', slate: 'border-slate-700 bg-slate-900/60 text-slate-300' };
    banner.innerHTML = msgs.map(([c, i, m]) => `<div class="rounded-lg border ${cls[c]} px-3 py-2 text-xs mb-2"><i class="fas ${i} mr-1.5"></i>${m}</div>`).join('');
  }

  function tile(label, value, sub, cls = '') {
    return `<div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2 min-w-0">
      <div class="text-[10px] text-slate-500 truncate">${label}</div>
      <div class="text-sm font-semibold num truncate ${cls}">${value}</div>
      ${sub ? `<div class="text-[10px] text-slate-500 truncate">${sub}</div>` : ''}</div>`;
  }

  function renderSummary(d) {
    const s = d.summary || {};
    const b = d.bankroll || {};
    const e = d.actual_exposure || {};
    const cur = b.currency || '';
    const rsOk = d.risk_state && d.risk_state.capacity_valid;
    const meter = e.day_fraction != null ? `
      <div class="exposure-meter" role="img" aria-label="當日實際 exposure ${pct(e.day_fraction)} / 上限 ${pct(e.max_day_fraction, 0)}">
        <span style="width:${NBA.pct(e.day_cap_used_share ?? 0, 1)}"></span>
      </div>` : '';
    summaryEl.innerHTML = `
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        ${tile('Betting day（台灣）', esc(d.betting_day), d.as_of ? `物化 ${ago(d.last_confirmed_at)}` : '尚未物化')}
        ${tile('Day-start bankroll', amt(b.day_start_bankroll, cur), b.day_start_basis_as_of ? `凍結於 ${NBA.tpe(b.day_start_basis_as_of)}` : '尚未凍結')}
        ${tile('目前 / 可用 bankroll', `${amt(b.current_bankroll)} / ${amt(b.available_bankroll)}`, b.committed_open_stake != null ? `未結算 stake ${amt(b.committed_open_stake)}` : '—')}
        ${tile('當日實際 exposure', e.day_fraction != null ? pct(e.day_fraction) : '—', `risk-v1 上限 ${pct(e.max_day_fraction ?? 0.08, 0)}${meter}`)}
        ${tile('剩餘風險額度', e.remaining_day_fraction != null ? pct(e.remaining_day_fraction) : '—', rsOk && e.remaining_day_amount != null ? amt(e.remaining_day_amount, cur) : (d.materialized ? '重新計算中' : '—'), e.remaining_day_fraction === 0 ? 'text-red-400' : '')}
        ${tile('機會 / 實際下注', `${s.n_actionable ?? 0}+${s.n_review ?? 0} / ${s.n_actual_bets ?? 0}`, `無台彩盤口 ${s.n_games_no_taiwan_odds ?? '—'} 場 · 過舊 ${s.n_games_stale_taiwan_odds ?? 0}`)}
      </div>
      <div class="flex flex-wrap items-center gap-2 mt-2 text-[11px] text-slate-400">
        <span class="grp-badge grp-review"><i class="fas fa-flask"></i>Prospective validation<span class="opacity-70 font-normal">尚在前瞻驗證</span></span>
        <span>${esc(d.disclaimer || '')}</span>
      </div>`;
  }

  /* ---------------------------- games ---------------------------- */

  function oppRow(o, g) {
    const cur = (board.bankroll || {}).currency || '';
    const valid = o.capacity_valid;
    const capacity = o.decision_status === 'qualified'
      ? (valid ? `<span class="text-orange-300 font-semibold">${amt(o.suggested_stake_amount, cur)}</span><span class="text-slate-500"> · ${pct(o.user_adjusted_fraction)}</span>`
        : `<span class="text-slate-500">${pct(o.user_adjusted_fraction)}（重新計算中）</span>`)
      : o.decision_status === 'bankroll_unavailable' && o.user_adjusted_fraction != null ? `<span class="text-slate-500">${pct(o.user_adjusted_fraction)}（無金額）</span>`
        : '<span class="text-slate-600">0</span>';
    const recordBtn = valid && (o.status_group === 'actionable' || o.status_group === 'review')
      ? `<button class="record-btn" data-opp="${o.id}"><i class="fas fa-pen-to-square mr-1"></i>記錄下注</button>` : '';
    const warn = (o.warnings || []).filter((w) => w !== 'seed_fixture');
    return `
    <div class="opp-row" data-group="${o.status_group}">
      <div class="opp-main">
        <div class="min-w-0">
          <div class="text-xs text-slate-400">${esc(MARKET[o.market] || o.market)}</div>
          <div class="text-sm font-medium break-words">${sideText(o, g)}</div>
        </div>
        <div class="text-right"><div class="text-[10px] text-slate-500">賠率</div><div class="num text-sm">${NBA.odds(o.decimal_odds)}</div></div>
        <div class="text-right"><div class="text-[10px] text-slate-500">EV</div><div class="num text-sm ${(o.ev_per_unit ?? 0) > 0 ? 'text-green-400' : 'text-slate-400'}">${evText(o.ev_per_unit)}</div></div>
        <div class="text-right min-w-0"><div class="text-[10px] text-slate-500">新增額度</div><div class="num text-sm leading-tight">${capacity}</div></div>
        <div class="opp-status">${groupBadge(o.status_group, o.status_text?.zh)}<span class="text-[10px] text-slate-500">${esc(o.status_text?.zh || o.decision_status)}</span>${recordBtn}</div>
      </div>
      <details class="opp-details">
        <summary>詳細（機率 / Kelly / exposure / 來源）</summary>
        <dl class="detail-grid">
          <div><dt>原始隱含</dt><dd>${pct(o.raw_implied_prob, 1)}</dd></div>
          <div><dt>去水公允（proportional-v1）</dt><dd>${pct(o.fair_no_vig_prob, 1)}</dd></div>
          <div><dt>模型 P(勝 / 退 / 輸)</dt><dd>${pct(o.model_prob, 1)} / ${pct(o.push_prob, 1)} / ${pct(o.loss_prob, 1)}</dd></div>
          <div><dt>edge vs fair（診斷）</dt><dd>${o.edge_vs_fair == null ? '—' : NBA.signed(o.edge_vs_fair * 100, 1) + ' pp'}</dd></div>
          <div><dt>Full / ¼ Kelly</dt><dd>${pct(o.full_kelly_fraction)} / ${pct(o.fractional_kelly_fraction)}</dd></div>
          <div><dt>單筆上限後（≤ 2%）</dt><dd>${pct(o.single_bet_capped_fraction)}</dd></div>
          <div><dt>理論（D.3 risk-v1，無實際下注）</dt><dd>${pct(o.theoretical_final_fraction)}</dd></div>
          <div><dt>同場實際 / 剩餘</dt><dd>${pct(o.actual_game_exposure)} / ${pct(o.remaining_game_fraction)}</dd></div>
          <div><dt>當日剩餘</dt><dd>${pct(o.remaining_day_fraction)}</dd></div>
          <div><dt>縮放（同場 × 單日）</dt><dd>${NBA.fixed(o.game_scale_factor, 3)} × ${NBA.fixed(o.day_scale_factor, 3)}</dd></div>
          <div><dt>盤口時間</dt><dd>${NBA.tpe(o.odds_fetched_at)}（最後確認 ${ago(o.odds_last_seen_at)}）</dd></div>
          <div><dt>Artifact / pricing</dt><dd class="break-all">${esc(o.artifact_version || '—')} · ${esc(o.pricing_version || '')}</dd></div>
          ${o.linked_bet_ids?.length ? `<div><dt>已記錄注單</dt><dd>#${o.linked_bet_ids.join(', #')}${o.linked_actual_fraction != null ? `（${pct(o.linked_actual_fraction)}）` : ''}</dd></div>` : ''}
          ${o.paper_decision_id ? `<div><dt>Paper decision（T-60）</dt><dd>#${o.paper_decision_id}（前瞻驗證紀錄，非實際下注）</dd></div>` : ''}
        </dl>
        ${(o.reasons || []).length ? `<p class="text-[10px] text-slate-500 mt-1">原因：${o.reasons.map(esc).join('、')}</p>` : ''}
        ${warn.length ? `<p class="text-[10px] text-amber-400/90 mt-1"><i class="fas fa-triangle-exclamation mr-1"></i>${warn.map(esc).join('、')}</p>` : ''}
      </details>
    </div>`;
  }

  function intlRow(o) {
    return `<tr><td>${esc(o.bookmaker)}</td><td>${esc(MARKET[o.market] || o.market)} ${esc(o.side)}${o.display_line != null ? ' ' + NBA.fixed(o.display_line) : ''}</td>
      <td class="num">${NBA.odds(o.decimal_odds)}</td><td class="num">${evText(o.ev_per_unit)}</td></tr>`;
  }

  function twMissing(g) {
    const txt = (board.labels?.taiwan_odds || {})[g.taiwan_odds_state] || '台彩盤口尚未取得';
    const icon = TW_ICON[g.taiwan_odds_state] || 'fa-circle-question';
    const hint = g.taiwan_odds_state === 'ingestion_unavailable'
      ? '<div class="text-[11px] text-slate-500 mt-1">合規備援：一般瀏覽器匯出 HAR → <code>python run_odds.py --source twsport --from-file capture.har</code></div>' : '';
    return `<div class="tw-missing"><i class="fas ${icon} mr-1.5"></i>${esc(txt)}${g.taiwan_odds_reason ? `<span class="text-slate-500">（${esc(g.taiwan_odds_reason)}）</span>` : ''}${hint}
      <div class="text-[11px] text-slate-500 mt-1">台彩 actionable 狀態維持 unavailable；不會以國際盤替代。</div></div>`;
  }

  function gameCard(g) {
    const p = g.prediction;
    const pd = g.prediction_display;
    const groups = g.status_group || (board.materialized ? 'blocked' : null);
    const predLine = p
      ? `分差 ${NBA.signed(p.pred_margin)} · 總分 ${NBA.fixed(p.pred_total)} · 上半場 ${NBA.signed(p.h1_margin)} / ${NBA.fixed(p.h1_total)}
         <span class="text-slate-600">· ${esc(p.model_version)} ${esc(p.prediction_kind || '')} · ${ago(p.available_at)}</span>`
      : pd ? `<span class="text-slate-500">尚無可定價預測（ml-v2.0）· 最新紀錄：${esc(pd.model_version)} 分差 ${NBA.signed(pd.pred_margin)}（僅顯示）</span>`
        : '<span class="text-slate-500"><i class="fas fa-hourglass-half mr-1"></i>預測尚未產生</span>';
    const dq = (p?.data_quality_flags || []);
    const warns = [];
    if (g.key_absences) warns.push(`<span class="text-red-300"><i class="fas fa-user-injured mr-1"></i>主力缺陣 ${g.key_absences}</span>`);
    if (dq.length) warns.push(`<span class="text-amber-300"><i class="fas fa-flask mr-1"></i>資料品質：${dq.map(esc).join('、')}</span>`);
    const tw = g.taiwan || [];
    const intl = g.international || [];
    const pdec = g.paper_decision;
    const recorded = (g.actual_bet_ids || []).length;
    return `
    <article class="game-card" data-game="${g.game_id}">
      <header class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <div class="flex items-center gap-2 text-[11px] text-slate-500 mb-0.5">
            <span>${esc(g.tipoff_display)}</span>${NBA.gameStatusBadge(g.game_status)}
          </div>
          <a href="/games/${g.game_id}" class="text-[15px] font-semibold hover:text-orange-300 truncate block">
            ${esc(g.away.name_zh || g.away.abbr)} <span class="text-slate-600">@</span> ${esc(g.home.name_zh || g.home.abbr)}</a>
        </div>
        <div class="shrink-0 text-right">${groups ? groupBadge(groups) : ''}</div>
      </header>
      <div class="text-[11px] text-slate-300 mt-1.5">${predLine}</div>
      ${warns.length ? `<div class="flex flex-wrap gap-x-3 gap-y-1 text-[11px] mt-1">${warns.join('')}</div>` : ''}
      <div class="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-slate-400 mt-2">
        <span>同場實際 ${g.actual_game_fraction != null ? pct(g.actual_game_fraction) : '—'}</span>
        <span>同場剩餘 ${g.remaining_game_fraction != null ? pct(g.remaining_game_fraction) : '—'}${board.risk_state?.capacity_valid && g.remaining_game_amount != null ? `（${amt(g.remaining_game_amount)}）` : ''}</span>
        ${recorded ? `<span class="text-sky-300"><i class="fas fa-receipt mr-1"></i>已記錄 ${recorded} 筆實際下注</span>` : ''}
        ${pdec ? `<span>Paper（T-60）：${esc(pdec.decision_status)}${pdec.no_bet_reason ? ' · ' + esc(pdec.no_bet_reason) : ''}</span>` : ''}
      </div>
      <section class="mt-3">
        <h3 class="text-[11px] font-semibold text-slate-400 mb-1.5"><i class="fas fa-flag mr-1"></i>台灣運彩</h3>
        ${tw.length ? tw.map((o) => oppRow(o, g)).join('') : twMissing(g)}
        ${tw.length && g.taiwan_odds_state && g.taiwan_odds_state !== 'available' ? twMissing(g) : ''}
      </section>
      ${intl.length ? `
      <details class="mt-2 intl">
        <summary class="text-[11px] text-slate-400"><i class="fas fa-globe mr-1"></i>International comparison（${intl.length}）— International diagnostic · Not Taiwan Sports Lottery evidence</summary>
        <div class="overflow-x-auto"><table class="stat-table text-xs mt-1"><thead><tr><th>Bookmaker</th><th>Outcome</th><th>賠率</th><th>EV（診斷）</th></tr></thead>
        <tbody>${intl.map(intlRow).join('')}</tbody></table></div>
      </details>` : ''}
      ${(g.blockers || []).length && !tw.some((o) => o.status_group === 'actionable' || o.status_group === 'review')
        ? `<p class="text-[10px] text-slate-500 mt-2">為什麼沒有可操作機會：${g.blockers.map(esc).join('、')}</p>` : ''}
    </article>`;
  }

  function renderGames(d) {
    if (!d.games.length) {
      gamesEl.innerHTML = NBA.empty(`${d.betting_day}（台灣）沒有賽事。若為賽季中，請確認賽程排程（系統狀態 → 賽程）。`, 'fa-calendar-xmark');
      return;
    }
    const order = { actionable: 0, review: 1, recorded: 2, blocked: 3, inactive: 4 };
    const games = [...d.games].sort((a, b) => (order[a.status_group] ?? 5) - (order[b.status_group] ?? 5)
      || String(a.tipoff_utc).localeCompare(String(b.tipoff_utc)));
    gamesEl.innerHTML = `<div class="grid gap-4 2xl:grid-cols-2">${games.map(gameCard).join('')}</div>`;
    gamesEl.querySelectorAll('.record-btn').forEach((btn) => btn.addEventListener('click', () => openRecord(Number(btn.dataset.opp))));
  }

  /* -------------------------- side panels -------------------------- */

  function renderEvidence(d) {
    const ev = d.evidence;
    if (!ev) { evidenceEl.innerHTML = ''; return; }
    const pp = ev.prospective_paper || {};
    const bs = pp.bootstrap || {};
    evidenceEl.innerHTML = `
    <section class="panel">
      <h2><i class="fas fa-scale-balanced mr-1.5"></i>Evidence</h2>
      <dl class="kv">
        <div><dt>Historical model evidence</dt><dd class="text-green-400">Available</dd></div>
        <div><dt>Historical betting evidence</dt><dd class="text-amber-400">Unavailable</dd></div>
        <div><dt>Prospective paper</dt><dd>${pp.status === 'unavailable' ? 'ledger 未啟用' : `${pp.n_betting_days ?? 0} betting days · ${pp.n_graded_betting_days ?? 0} graded · ${pp.n_bets ?? 0} bets`}</dd></div>
        <div><dt>Bootstrap（≥ ${pp.min_days_for_ci ?? 30} graded days）</dt><dd>${bs.status === 'ok' ? `95% CI ${NBA.signed(bs.yield_ci95[0] * 100, 1)}% ~ ${NBA.signed(bs.yield_ci95[1] * 100, 1)}%（描述用）` : 'Insufficient sample'}</dd></div>
      </dl>
      <p class="text-[10px] text-slate-500 mt-2">Prospective 結果只作描述，不會改變策略或額度；使用者實際下注紀錄另見「資金」頁（不是策略績效）。</p>
    </section>`;
  }

  function renderHealth(d) {
    const h = d.system_health;
    if (!h) { healthEl.innerHTML = ''; return; }
    healthEl.innerHTML = `
    <section class="panel">
      <h2><i class="fas fa-heart-pulse mr-1.5"></i>System readiness <span class="text-[10px] font-normal text-slate-500">${esc(h.overall)}</span></h2>
      <ul class="space-y-1">
        ${h.components.map((c) => {
          const [icon, cls, zh] = HEALTH[c.status] || HEALTH.waiting;
          return `<li class="flex items-start justify-between gap-2 text-[11px]">
            <span class="text-slate-300 min-w-0">${esc(c.label)}${c.detail ? `<span class="block text-[10px] text-slate-500 truncate" title="${esc(c.detail)}">${esc(c.detail)}</span>` : ''}</span>
            <span class="shrink-0 text-right ${cls}"><i class="fas ${icon} mr-1"></i>${zh}<span class="block text-[10px] text-slate-500">${c.last_successful_update ? ago(c.last_successful_update) : ''}</span></span>
          </li>`;
        }).join('')}
      </ul>
      <a href="/status" class="text-[10px] text-orange-400 hover:underline">完整系統狀態 →</a>
    </section>`;
  }

  /* -------------------------- record modal -------------------------- */

  function findOpp(id) {
    for (const g of board.games) for (const o of g.taiwan) if (o.id === id) return { o, g };
    return null;
  }

  function openRecord(id) {
    const hit = findOpp(id);
    if (!hit) return;
    const { o, g } = hit;
    const cur = (board.bankroll || {}).currency || '';
    const requestId = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
    modalRoot.innerHTML = `
    <div class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="rec-title">
      <form id="rec-form" class="modal-card">
        <h2 id="rec-title" class="text-sm font-semibold mb-1"><i class="fas fa-pen-to-square text-orange-400 mr-1.5"></i>記錄下注</h2>
        <p class="text-[11px] text-slate-500 mb-3">請先在台灣運彩完成下注，再填入<strong>實際成交</strong>的賠率與金額。平台不會替你下注。</p>
        <dl class="kv mb-3">
          <div><dt>比賽</dt><dd>${esc(g.away.abbr)} @ ${esc(g.home.abbr)} · ${esc(g.tipoff_display)}</dd></div>
          <div><dt>來源</dt><dd>台灣運彩（${esc(o.bookmaker)}）</dd></div>
          <div><dt>玩法 / 方向</dt><dd>${esc(MARKET[o.market] || o.market)} · ${sideText(o, g)}</dd></div>
          <div><dt>平台觀察賠率</dt><dd class="num">${NBA.odds(o.decimal_odds)}（${NBA.tpe(o.odds_fetched_at)}）</dd></div>
          <div><dt>建議最大新增額度</dt><dd class="num">${amt(o.suggested_stake_amount, cur)}（${pct(o.user_adjusted_fraction)} of day-start ${amt(o.day_start_bankroll)}）</dd></div>
          <div><dt>EV（平台觀察賠率）</dt><dd class="num">${evText(o.ev_per_unit)} · 尚在前瞻驗證</dd></div>
        </dl>
        <div class="grid grid-cols-2 gap-3">
          <label class="block"><span class="text-[11px] text-slate-500">實際賠率</span>
            <input name="odds" type="number" step="0.01" min="1.01" required value="${o.decimal_odds ?? ''}" class="inp num" /></label>
          <label class="block"><span class="text-[11px] text-slate-500">實際金額（${esc(cur || '—')}）</span>
            <input name="stake" type="number" step="1" min="1" required value="${o.suggested_stake_amount ?? ''}" class="inp num" /></label>
          <label class="block col-span-2"><span class="text-[11px] text-slate-500">下注時間（選填，預設現在）</span>
            <input name="placed_at" type="datetime-local" class="inp" /></label>
          <label class="block col-span-2"><span class="text-[11px] text-slate-500">備註（選填）</span>
            <input name="note" type="text" maxlength="200" class="inp" /></label>
        </div>
        <p id="rec-odds-note" class="text-[11px] text-slate-500 mt-2 hidden">實際賠率與平台觀察不同：兩者都會保存（參考盤口快照不會被覆寫）。</p>
        <div id="rec-confirm" class="hidden mt-3 rounded border border-amber-800/60 bg-amber-950/30 p-2 text-[11px] text-amber-200"></div>
        <div class="flex items-center justify-end gap-2 mt-4">
          <span id="rec-msg" class="text-xs mr-auto"></span>
          <button type="button" id="rec-cancel" class="px-3 py-1.5 rounded border border-slate-700 text-xs">取消</button>
          <button type="submit" id="rec-submit" class="px-3 py-1.5 rounded bg-orange-500 hover:bg-orange-400 text-white text-xs font-medium">確認記錄</button>
        </div>
      </form>
    </div>`;
    const form = $('rec-form');
    const close = () => { modalRoot.innerHTML = ''; };
    $('rec-cancel').onclick = close;
    form.odds.addEventListener('input', () => {
      $('rec-odds-note').classList.toggle('hidden', Number(form.odds.value) === Number(o.decimal_odds));
    });
    let needConfirm = null;
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const msg = $('rec-msg');
      const payload = {
        decision_opportunity_id: o.id, odds: Number(form.odds.value), stake: Number(form.stake.value),
        note: form.note.value || null, client_request_id: requestId,
        placed_at: form.placed_at.value ? new Date(form.placed_at.value).toISOString() : undefined,
      };
      if (needConfirm) {
        const box = form.querySelector('#rec-ack');
        if (!box || !box.checked) { msg.className = 'text-xs text-red-400 mr-auto'; msg.textContent = '請勾選確認'; return; }
        payload.confirm_override = true;
        payload.override_reason = (form.querySelector('#rec-reason')?.value || '').trim();
        if (needConfirm.requires_reason && !payload.override_reason) { msg.className = 'text-xs text-red-400 mr-auto'; msg.textContent = '請填寫原因'; return; }
      }
      $('rec-submit').disabled = true;
      try {
        const res = await NBA.post('/api/bets', payload);
        msg.className = 'text-xs text-green-400 mr-auto';
        msg.textContent = `已記錄 #${res.id}（${res.bet?.strategy_compliance || ''}）— 風險額度重新計算中`;
        setTimeout(() => { close(); load(currentDate); }, 900);
      } catch (e) {
        $('rec-submit').disabled = false;
        if (e.status === 409 && e.data?.error === 'confirmation_required') {
          needConfirm = e.data;
          const box = $('rec-confirm');
          box.classList.remove('hidden');
          box.innerHTML = `<p class="font-semibold mb-1"><i class="fas fa-triangle-exclamation mr-1"></i>${esc(e.data.message)}</p>
            <ul class="list-disc ml-4 mb-1">${(e.data.checks || []).map((c) => `<li>${esc(c)}</li>`).join('')}</ul>
            <p class="mb-1">確認後記錄為 <code>${esc(e.data.compliance_if_confirmed)}</code>，不會自動縮小你的金額，也不視為策略合規下注。</p>
            <label class="flex items-center gap-1.5"><input id="rec-ack" type="checkbox" />我確認這是已在外部完成的實際下注</label>
            ${e.data.requires_reason ? '<input id="rec-reason" type="text" maxlength="300" placeholder="原因（必填）" class="inp mt-1" />' : ''}`;
          msg.className = 'text-xs text-amber-300 mr-auto';
          msg.textContent = '需要明確確認';
        } else if (e.status === 409) {
          msg.className = 'text-xs text-red-400 mr-auto';
          msg.textContent = e.data?.message || e.message;
        } else {
          msg.className = 'text-xs text-red-400 mr-auto';
          msg.textContent = e.message;
        }
      }
    });
  }

  /* ------------------------------ load ------------------------------ */

  async function load(date) {
    currentDate = date;
    if (picker) picker.value = date;
    gamesEl.innerHTML = `<div class="grid gap-4">${NBA.skeletonCards(3)}</div>`;
    summaryEl.innerHTML = '';
    banner.innerHTML = '';
    try {
      board = await NBA.get(`/api/decision-board?date=${encodeURIComponent(date)}`);
      renderBanner(board);
      renderSummary(board);
      renderGames(board);
      renderEvidence(board);
      renderHealth(board);
    } catch (e) {
      board = null;
      if (e.status === 401) {
        gamesEl.innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-6 text-center">
          <i class="fas fa-lock text-2xl text-slate-600 mb-2"></i>
          <p class="text-sm text-slate-400 mb-3">決策中心包含個人 bankroll 與實際下注，請先登入</p>
          <a href="/login?next=/decision" class="inline-block px-3 py-1.5 rounded bg-orange-500 text-white text-sm">前往登入</a></div>`;
      } else {
        gamesEl.innerHTML = NBA.error(`載入決策中心失敗：${e.message}（API 錯誤；不顯示任何額度）`);
      }
    }
  }

  tabs.forEach((t) => t.addEventListener('click', () => {
    tabs.forEach((x) => { x.classList.remove('bg-orange-500/15', 'text-orange-300', 'font-medium'); x.classList.add('text-slate-400'); });
    t.classList.add('bg-orange-500/15', 'text-orange-300', 'font-medium');
    t.classList.remove('text-slate-400');
    load(NBA.tpeDateStr(Number(t.dataset.dday)));
  }));
  if (picker) picker.addEventListener('change', () => picker.value && load(picker.value));
  $('decision-refresh').addEventListener('click', () => load(currentDate));
  setInterval(() => { if (!modalRoot.innerHTML && document.visibilityState === 'visible') load(currentDate); }, 60000);
  load(currentDate);
})();
