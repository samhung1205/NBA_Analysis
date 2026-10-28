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
  const dock = $('decision-dock');
  const picker = $('decision-date');
  const tabs = document.querySelectorAll('[data-dday]');
  // modal 掛在 body 下，開啟時才能把其餘頁面設為 inert（焦點不會跑到背景）
  document.body.appendChild(modalRoot);
  // 日期寫在網址（?date=），手機分頁被系統回收或重新整理後仍回到同一個 betting day
  const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
  const urlDate = new URLSearchParams(location.search).get('date');
  let currentDate = DATE_RE.test(urlDate || '') ? urlDate : NBA.tpeDateStr(0);
  let board = null;
  let loadSeq = 0;
  let lastLoadedAt = 0;
  let resumeAfterLoad = false;
  let lastJumpKey = null;

  /*
   * 記錄草稿（只存在這台裝置）：去台彩下注途中被打斷、分頁被回收時，回來可以接著填。
   * 保留同一個 client_request_id：若上次其實已送出但沒收到回應，重送會被伺服器辨識為同一筆（replay），不會重複建立。
   */
  const DRAFT_KEY = 'nba.decision.recordDraft.v1';
  const DRAFT_TTL_MS = 12 * 3600 * 1000;
  function readDraft() {
    try {
      const d = JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null');
      if (!d || !d.request_id || Date.now() - d.saved_at > DRAFT_TTL_MS) { clearDraft(); return null; }
      return d;
    } catch { return null; }
  }
  function saveDraft(d) { try { localStorage.setItem(DRAFT_KEY, JSON.stringify({ ...d, saved_at: Date.now() })); } catch { /* 無痕或封鎖儲存：不影響記錄 */ } }
  function clearDraft() { try { localStorage.removeItem(DRAFT_KEY); } catch { /* 同上 */ } }

  // 跨物化穩定的機會鍵（opportunity id 依 snapshot 而變）：用於刷新後保留展開狀態、焦點與草稿對應
  const oppKey = (o) => [o.game_id, o.market, o.side, o.bookmaker, o.display_line ?? ''].join('|');

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
  const amt = (v, cur) => (v == null ? '—' : NBA.money(Math.round(Number(v)), cur));
  const pct = (v, d = 2) => NBA.pct(v, d);
  const evText = (v) => (v == null ? '—' : NBA.signed(v * 100, 1) + '%');
  const groupBadge = (g, extra) => {
    const [icon, cls, en, zh] = GROUP[g] || ['fa-circle-question', 'grp-inactive', g || '—', ''];
    return `<span class="grp-badge ${cls}" title="${esc(extra || zh)}"><i class="fas ${icon}"></i>${en}<span class="opacity-70 font-normal">${zh}</span></span>`;
  };
  // 純文字版（草稿標籤用）；sideText 為 HTML 跳脫版
  const sidePlain = (o, g) => {
    const s = o.side;
    const name = s === 'home' ? NBA.teamName(g.home) : s === 'away' ? NBA.teamName(g.away)
      : s === 'over' ? '大分' : s === 'under' ? '小分' : s === 'draw' ? '和局' : s;
    const line = o.display_line != null && o.market_type !== 'moneyline' ? ` ${o.market_type === 'spread' ? NBA.signed(o.display_line) : NBA.fixed(o.display_line)}` : '';
    return `${name}${line}`;
  };
  const sideText = (o, g) => esc(sidePlain(o, g));
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
    if (rs.status === 'recalculation_pending') msgs.push(['amber', 'fa-rotate', '剛記錄的下注或資金異動尚未納入風險計算，額度暫不顯示，約一分鐘內更新。']);
    if (rs.materialization_stale) msgs.push(['red', 'fa-triangle-exclamation', `決策物化已超過 10 分鐘未更新（最後確認 ${ago(d.last_confirmed_at)}）— 系統降級，額度暫不視為有效。`]);
    if (d.bankroll && d.bankroll.configured === false) msgs.push(['slate', 'fa-wallet', '尚未設定策略資金：只顯示理論比例，不產生可操作金額。<a href="/bankroll" class="underline">前往設定</a>']);
    if (d.actual_exposure && d.actual_exposure.incomplete) msgs.push(['red', 'fa-circle-exclamation', '實際下注資料不完整，已下注比例可能低估，暫停新的可操作額度。']);
    if (d.actual_exposure && d.actual_exposure.day_over_limit) msgs.push(['red', 'fa-gauge-high', `當日已下注 ${pct(d.actual_exposure.day_fraction)}，已超過 risk-v1 上限 ${pct(d.actual_exposure.max_day_fraction, 0)}；剩餘 0%（不修改已下注紀錄）。`]);
    const health = d.system_health || {};
    if (health.overall === 'degraded') msgs.push(['amber', 'fa-heart-pulse', '部分資料來源異常（見「資料來源狀態」面板）。']);
    const cls = { red: 'border-red-900/60 bg-red-950/30 text-red-200', amber: 'border-amber-900/60 bg-amber-950/20 text-amber-200', slate: 'border-slate-700 bg-slate-900/60 text-slate-300' };
    banner.innerHTML = draftNotice(d) + msgs.map(([c, i, m]) => `<div class="rounded-lg border ${cls[c]} px-3 py-2 text-xs mb-2"><i class="fas ${i} mr-1.5"></i>${m}</div>`).join('');
  }

  /** 找得到對應機會的草稿才回傳（先比 id，再比穩定鍵）；其他 betting day 的草稿回傳 null */
  function draftTarget(dr, d) {
    if (!dr || !d || dr.betting_day !== d.betting_day) return null;
    return findOpp(dr.opp_id) || findOppByKey(dr.key);
  }

  function draftNotice(d) {
    const dr = readDraft();
    if (!dr) return '';
    const otherDay = dr.betting_day !== d.betting_day;
    const hit = otherDay ? null : draftTarget(dr, d);
    const when = ago(new Date(dr.saved_at).toISOString());
    const action = otherDay
      ? `<button type="button" class="draft-btn" data-draft="goto">前往 ${esc(dr.betting_day)} 繼續</button>`
      : hit ? '<button type="button" class="draft-btn" data-draft="resume">繼續記錄</button>'
        : `<a class="draft-btn" href="/bets?game_id=${encodeURIComponent(dr.game_id)}">改用手動記錄</a>`;
    const gone = !otherDay && !hit ? '<span class="block text-sky-200/80 mt-0.5">原機會已不在目前的決策板（可能已重新物化或盤口變動）。</span>' : '';
    return `<div class="draft-notice" role="status">
      <p class="min-w-0 mr-auto"><i class="fas fa-file-pen mr-1.5" aria-hidden="true"></i>有一筆尚未送出的下注記錄：<strong>${esc(dr.label)}</strong>${otherDay ? `（${esc(dr.betting_day)}）` : ''}<span class="text-sky-200/80"> · ${when}填寫</span>${gone}</p>
      <div class="flex items-center gap-2">${action}<button type="button" class="draft-btn-ghost" data-draft="discard">捨棄</button></div>
    </div>`;
  }

  function tile(label, value, sub, cls = '') {
    return `<div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2 min-w-0">
      <div class="text-xs text-slate-400 truncate">${label}</div>
      <div class="text-sm font-semibold num truncate ${cls}">${value}</div>
      ${sub ? `<div class="text-xs text-slate-400 truncate">${sub}</div>` : ''}</div>`;
  }

  function renderSummary(d) {
    const s = d.summary || {};
    const b = d.bankroll || {};
    const e = d.actual_exposure || {};
    const cur = b.currency || '';
    const rsOk = d.risk_state && d.risk_state.capacity_valid;
    const meter = e.day_fraction != null ? `
      <div class="exposure-meter" role="img" aria-label="當日已下注 ${pct(e.day_fraction)} / 上限 ${pct(e.max_day_fraction, 0)}">
        <span style="width:${NBA.pct(e.day_cap_used_share ?? 0, 1)}"></span>
      </div>` : '';
    summaryEl.innerHTML = `
      <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        ${tile('投注日（台灣）', esc(d.betting_day), d.as_of ? `更新於 ${ago(d.last_confirmed_at)}` : '決策檢視尚未產生')}
        ${tile('當日起始資金', amt(b.day_start_bankroll, cur), b.day_start_basis_as_of ? `凍結於 ${NBA.tpe(b.day_start_basis_as_of)}` : '尚未凍結')}
        ${tile(`目前 / 可用資金${cur ? `（${esc(cur)}）` : ''}`, `${amt(b.current_bankroll)} / ${amt(b.available_bankroll)}`, b.committed_open_stake != null ? `未結算下注 ${amt(b.committed_open_stake)}` : '—')}
        ${tile('當日已下注比例', e.day_fraction != null ? pct(e.day_fraction) : '—', `risk-v1 上限 ${pct(e.max_day_fraction ?? 0.08, 0)}${meter}`)}
        ${tile('剩餘風險額度', e.remaining_day_fraction != null ? pct(e.remaining_day_fraction) : '—', rsOk && e.remaining_day_amount != null ? amt(e.remaining_day_amount, cur) : (d.materialized ? '重新計算中' : '—'), e.remaining_day_fraction === 0 ? 'text-red-400' : '')}
        ${tile('可操作 / 已記錄', `${s.n_actionable ?? 0}${s.n_review ? ` + 需檢視 ${s.n_review}` : ''} / ${s.n_actual_bets ?? 0} 筆`, `無台彩盤口 ${s.n_games_no_taiwan_odds ?? '—'} 場 · 盤口過舊 ${s.n_games_stale_taiwan_odds ?? 0} 場`)}
      </div>
      <div class="flex flex-wrap items-center gap-2 mt-2 text-xs text-slate-400">
        <span class="grp-badge grp-review"><i class="fas fa-flask" aria-hidden="true"></i>前瞻驗證期</span>
        <span>${esc(d.disclaimer || '')}</span>
      </div>`;
  }

  /* ---------------------------- games ---------------------------- */

  // 狀態說明與徽章相同時不重複顯示（例如「無正 EV」）
  function statusNote(o) {
    const zh = o.status_text?.zh || NBA.reasonText(o.decision_status);
    const badge = (GROUP[o.status_group] || [])[3];
    return zh && zh !== badge ? `<span class="text-xs text-slate-400">${esc(zh)}</span>` : '';
  }

  function oppRow(o, g) {
    const cur = (board.bankroll || {}).currency || '';
    const valid = o.capacity_valid;
    const capacity = o.decision_status === 'qualified'
      ? (valid ? `<span class="text-orange-300 font-semibold">${amt(o.suggested_stake_amount, cur)}</span><span class="text-slate-400"> · ${pct(o.user_adjusted_fraction)}</span>`
        : `<span class="text-slate-400">${pct(o.user_adjusted_fraction)}（重新計算中）</span>`)
      : o.decision_status === 'bankroll_unavailable' && o.user_adjusted_fraction != null ? `<span class="text-slate-400">${pct(o.user_adjusted_fraction)}（無金額）</span>`
        : '<span class="text-slate-400">0</span>';
    // 下注是已在台彩發生的事實：每個台彩機會都留記錄入口，合規與否由 /api/bets 依最新物化狀態判斷。
    // 可操作且額度有效的用主按鈕；其餘用次要按鈕，送出時伺服器會在不符合策略時先要求確認。
    const k = esc(oppKey(o));
    const primary = valid && (o.status_group === 'actionable' || o.status_group === 'review');
    const verb = primary ? '記錄下注' : '我已下注';
    const label = `${esc(NBA.teamName(g.away))} @ ${esc(NBA.teamName(g.home))} ${esc(MARKET[o.market] || o.market)} ${sideText(o, g)} 賠率 ${NBA.odds(o.decimal_odds)}`;
    const recordBtn = `<button type="button" class="${primary ? 'record-btn' : 'record-btn-alt'}" data-opp="${o.id}" data-fk="rec|${k}" aria-label="${verb}：${label}"><i class="fas fa-pen-to-square mr-1" aria-hidden="true"></i>${verb}</button>`;
    const warn = (o.warnings || []).filter((w) => w !== 'seed_fixture');
    return `
    <div class="opp-row" data-group="${o.status_group}" data-k="${k}">
      <div class="opp-main">
        <div class="min-w-0">
          <div class="text-xs text-slate-400">${esc(MARKET[o.market] || o.market)}</div>
          <div class="text-sm font-medium break-words">${sideText(o, g)}</div>
        </div>
        <div class="text-right"><div class="text-xs text-slate-400">賠率</div><div class="num text-sm">${NBA.odds(o.decimal_odds)}</div></div>
        <div class="text-right"><div class="text-xs text-slate-400">EV</div><div class="num text-sm ${(o.ev_per_unit ?? 0) > 0 ? 'text-green-400' : 'text-slate-400'}">${evText(o.ev_per_unit)}</div></div>
        <div class="text-right min-w-0"><div class="text-xs text-slate-400">新增額度</div><div class="num text-sm leading-tight">${capacity}</div></div>
        <div class="opp-status">${groupBadge(o.status_group, o.status_text?.zh)}${statusNote(o)}${recordBtn}</div>
      </div>
      <details class="opp-details" data-k="${k}">
        <summary data-fk="sum|${k}">詳細（機率 / Kelly / exposure / 來源）</summary>
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
        ${(o.reasons || []).length ? `<p class="text-xs text-slate-400 mt-1">原因：${o.reasons.map((r) => esc(NBA.reasonText(r))).join('、')}</p>` : ''}
        ${warn.length ? `<p class="text-xs text-amber-300 mt-1"><i class="fas fa-triangle-exclamation mr-1" aria-hidden="true"></i>${warn.map((w) => esc(NBA.reasonText(w))).join('、')}</p>` : ''}
      </details>
    </div>`;
  }

  function intlRow(o, g) {
    return `<tr><td>${esc(o.bookmaker)}</td><td>${esc(MARKET[o.market] || o.market)} ${sideText(o, g)}</td>
      <td class="num">${NBA.odds(o.decimal_odds)}</td><td class="num">${evText(o.ev_per_unit)}</td></tr>`;
  }

  function twMissing(g) {
    const txt = (board.labels?.taiwan_odds || {})[g.taiwan_odds_state] || '台彩盤口尚未取得';
    const icon = TW_ICON[g.taiwan_odds_state] || 'fa-circle-question';
    const hint = g.taiwan_odds_state === 'ingestion_unavailable'
      ? '<div class="text-xs text-slate-400 mt-1">請先按本頁上方「同步台彩盤口」。設定與 HAR 備援見<a href="/static/twsport-sync-help.html" class="underline underline-offset-2">同步助手說明</a>。</div>' : '';
    return `<div class="tw-missing"><i class="fas ${icon} mr-1.5"></i>${esc(txt)}${g.taiwan_odds_reason ? `<span class="text-slate-400">（${esc(g.taiwan_odds_reason)}）</span>` : ''}${hint}
      <div class="text-xs text-slate-400 mt-1">沒有台彩盤口就沒有可操作機會；不會以國際盤替代。</div></div>`;
  }

  function gameCard(g) {
    const p = g.prediction;
    const pd = g.prediction_display;
    const groups = g.status_group || (board.materialized ? 'blocked' : null);
    const predLine = p
      ? `分差 ${NBA.signed(p.pred_margin)} · 總分 ${NBA.fixed(p.pred_total)} · 上半場 ${NBA.signed(p.h1_margin)} / ${NBA.fixed(p.h1_total)}
         <span class="text-slate-400">· ${esc(p.model_version)} ${esc(p.prediction_kind || '')} · ${ago(p.available_at)}</span>`
      : pd ? `<span class="text-slate-400">尚無可定價預測（ml-v2.0）· 最新紀錄：${esc(pd.model_version)} 分差 ${NBA.signed(pd.pred_margin)}（僅顯示）</span>`
        : '<span class="text-slate-400"><i class="fas fa-hourglass-half mr-1"></i>預測尚未產生</span>';
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
          <div class="flex items-center gap-2 text-xs text-slate-400 mb-0.5">
            <span>${esc(g.tipoff_display)}</span>${NBA.gameStatusBadge(g.game_status)}
          </div>
          <h2 class="text-[15px] font-semibold truncate"><a href="/games/${g.game_id}" class="hover:text-orange-300">
            ${NBA.matchup(g.away, g.home)}</a></h2>
        </div>
        <div class="shrink-0 text-right">${groups ? groupBadge(groups) : ''}</div>
      </header>
      <div class="text-xs text-slate-300 mt-1.5">${predLine}</div>
      ${warns.length ? `<div class="flex flex-wrap gap-x-3 gap-y-1 text-xs mt-1">${warns.join('')}</div>` : ''}
      <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400 mt-2">
        <span>同場實際 ${g.actual_game_fraction != null ? pct(g.actual_game_fraction) : '—'}</span>
        <span>同場剩餘 ${g.remaining_game_fraction != null ? pct(g.remaining_game_fraction) : '—'}${board.risk_state?.capacity_valid && g.remaining_game_amount != null ? `（${amt(g.remaining_game_amount)}）` : ''}</span>
        ${recorded ? `<span class="text-sky-300"><i class="fas fa-receipt mr-1"></i>已記錄 ${recorded} 筆實際下注</span>` : ''}
        ${pdec ? `<span>前瞻驗證（開賽前 60 分）：${esc(NBA.reasonText(pdec.decision_status))}${pdec.no_bet_reason ? ' · ' + esc(NBA.reasonText(pdec.no_bet_reason)) : ''}</span>` : ''}
      </div>
      <section class="mt-3">
        <h3 class="text-xs font-semibold text-slate-400 mb-1.5"><i class="fas fa-flag mr-1"></i>台灣運彩</h3>
        ${tw.length ? tw.map((o) => oppRow(o, g)).join('') : twMissing(g)}
        ${tw.length && g.taiwan_odds_state && g.taiwan_odds_state !== 'available' ? twMissing(g) : ''}
      </section>
      ${intl.length ? `
      <details class="mt-2 intl" data-k="intl|${g.game_id}">
        <summary class="text-xs text-slate-400" data-fk="sum|intl|${g.game_id}"><i class="fas fa-globe mr-1" aria-hidden="true"></i>國際盤對照（${intl.length}）— 僅供診斷，不是台彩證據</summary>
        <div class="overflow-x-auto"><table class="stat-table text-xs mt-1"><thead><tr><th scope="col">莊家</th><th scope="col">選項</th><th scope="col">賠率</th><th scope="col">EV（診斷）</th></tr></thead>
        <tbody>${intl.map((o) => intlRow(o, g)).join('')}</tbody></table></div>
      </details>` : ''}
      ${(g.blockers || []).length && !tw.some((o) => o.status_group === 'actionable' || o.status_group === 'review')
        ? `<p class="text-xs text-slate-400 mt-2">為什麼沒有可操作機會：${g.blockers.map((b) => esc(NBA.reasonText(b))).join('、')}</p>` : ''}
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
  }

  /* --------------------------- mobile dock --------------------------- */

  // 手機 / 平板底部：當日剩餘額度（只顯示 Python 物化值）＋下一個可操作 / 繼續未送出的記錄
  function renderDock(d) {
    if (!dock) return;
    if (!d || !d.games.length) { dock.hidden = true; document.body.classList.remove('has-dock'); return; }
    const e = d.actual_exposure || {};
    const cur = (d.bankroll || {}).currency || '';
    const s = d.summary || {};
    const rsOk = d.risk_state && d.risk_state.capacity_valid;
    let value;
    let sub;
    if (!d.materialized) { value = '—'; sub = '決策檢視尚未產生'; }
    else if (!rsOk) { value = '重新計算中'; sub = '更新前不顯示可用額度'; }
    else if (e.remaining_day_amount == null) { value = e.remaining_day_fraction != null ? pct(e.remaining_day_fraction) : '—'; sub = '無可用金額'; }
    else { value = amt(e.remaining_day_amount, cur); sub = `剩餘 ${pct(e.remaining_day_fraction)} · 可操作 ${s.n_actionable ?? 0}${s.n_review ? ` · 需檢視 ${s.n_review}` : ''}`; }
    const targets = gamesEl.querySelectorAll('.record-btn').length;
    const resumable = draftTarget(readDraft(), d);
    const btn = resumable
      ? '<button type="button" class="dock-btn dock-btn-primary" data-dock="resume"><i class="fas fa-file-pen mr-1.5" aria-hidden="true"></i>繼續記錄</button>'
      : targets ? `<button type="button" class="dock-btn" data-dock="next"><i class="fas fa-arrow-down mr-1.5" aria-hidden="true"></i>下一個可操作（${targets}）</button>` : '';
    dock.innerHTML = `
      <div class="min-w-0 mr-auto">
        <div class="dock-label">當日剩餘額度</div>
        <div class="dock-value num${e.remaining_day_fraction === 0 && rsOk ? ' text-red-400' : ''}">${value}</div>
        <div class="dock-sub truncate">${sub}</div>
      </div>${btn}`;
    dock.hidden = false;
    document.body.classList.add('has-dock');
  }

  function jumpToNextActionable() {
    const rows = [...gamesEl.querySelectorAll('.record-btn')].map((b) => b.closest('.opp-row'));
    if (!rows.length) return;
    // 剛跳到的列還在畫面內 → 往下一個；否則從視窗上緣下方第一個開始；都看過了就回到第一個
    const cur = rows.findIndex((r) => r.dataset.k === lastJumpKey);
    const inView = (r) => { const b = r.getBoundingClientRect(); return b.top >= 0 && b.bottom <= window.innerHeight; };
    const next = (cur >= 0 && inView(rows[cur]) ? rows[cur + 1] : rows.find((r) => r.getBoundingClientRect().top > 96)) || rows[0];
    lastJumpKey = next.dataset.k;
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    next.scrollIntoView({ block: 'center', behavior: reduce ? 'auto' : 'smooth' });
    next.querySelector('.record-btn')?.focus({ preventScroll: true });
    next.classList.add('is-target');
    setTimeout(() => next.classList.remove('is-target'), 1600);
  }

  /* -------------------------- side panels -------------------------- */

  function renderEvidence(d) {
    const ev = d.evidence;
    if (!ev) { evidenceEl.innerHTML = ''; return; }
    const pp = ev.prospective_paper || {};
    const bs = pp.bootstrap || {};
    evidenceEl.innerHTML = `
    <section class="panel">
      <h2><i class="fas fa-scale-balanced mr-1.5" aria-hidden="true"></i>證據狀態</h2>
      <dl class="kv">
        <div><dt>歷史模型評估</dt><dd>有</dd></div>
        <div><dt>歷史投注證據（台彩盤口）</dt><dd class="text-amber-300">無</dd></div>
        <div><dt>前瞻驗證</dt><dd>${pp.status === 'unavailable' ? '尚未啟用' : `${pp.n_betting_days ?? 0} 個投注日 · 已結算 ${pp.n_graded_betting_days ?? 0} 日 · ${pp.n_bets ?? 0} 注`}</dd></div>
        <div><dt>信賴區間（需 ≥ ${pp.min_days_for_ci ?? 30} 個已結算日）</dt><dd>${bs.status === 'ok' ? `95% CI ${NBA.signed(bs.yield_ci95[0] * 100, 1)}% ~ ${NBA.signed(bs.yield_ci95[1] * 100, 1)}%（僅描述）` : '樣本不足'}</dd></div>
      </dl>
      <p class="text-xs text-slate-400 mt-2">前瞻驗證結果只作描述，不會改變策略或額度；你的實際下注紀錄另見「資金」頁（不是策略績效）。</p>
    </section>`;
  }

  function renderHealth(d) {
    const h = d.system_health;
    if (!h) { healthEl.innerHTML = ''; return; }
    healthEl.innerHTML = `
    <section class="panel">
      <h2><i class="fas fa-heart-pulse mr-1.5" aria-hidden="true"></i>資料來源狀態 <span class="text-xs font-normal text-slate-400">${esc(({ healthy: '正常', degraded: '部分異常', blocked: '受阻' })[h.overall] || h.overall)}</span></h2>
      <ul class="space-y-1">
        ${h.components.map((c) => {
          const [icon, cls, zh] = HEALTH[c.status] || HEALTH.waiting;
          return `<li class="flex items-start justify-between gap-2 text-xs">
            <span class="text-slate-300 min-w-0">${esc(c.label)}${c.detail ? `<span class="block text-xs text-slate-400 truncate" title="${esc(c.detail)}">${esc(c.detail)}</span>` : ''}</span>
            <span class="shrink-0 text-right ${cls}"><i class="fas ${icon} mr-1"></i>${zh}<span class="block text-xs text-slate-400">${c.last_successful_update ? ago(c.last_successful_update) : ''}</span></span>
          </li>`;
        }).join('')}
      </ul>
      <a href="/status" class="text-xs text-orange-400 hover:underline">完整系統狀態 →</a>
    </section>`;
  }

  /* -------------------------- record modal -------------------------- */

  function findOpp(id) {
    if (!board) return null;
    for (const g of board.games) for (const o of g.taiwan) if (o.id === id) return { o, g };
    return null;
  }
  function findOppByKey(key) {
    if (!board || !key) return null;
    for (const g of board.games) for (const o of g.taiwan) if (oppKey(o) === key) return { o, g };
    return null;
  }

  // datetime-local 需要本地時間字串；上限設為現在（伺服器也拒絕未來時間）
  const localNow = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);

  function setBackgroundInert(on) {
    for (const el of document.body.children) if (el !== modalRoot && el.tagName !== 'SCRIPT') el.toggleAttribute('inert', on);
    document.body.classList.toggle('modal-open', on);
  }

  function openRecord(id, trigger, draft) {
    const hit = findOpp(id);
    if (!hit) return;
    const { o, g } = hit;
    const cur = (board.bankroll || {}).currency || '';
    const valid = o.capacity_valid;
    const primary = valid && (o.status_group === 'actionable' || o.status_group === 'review');
    const groupZh = (GROUP[o.status_group] || [])[3] || o.status_group || '—';
    const requestId = draft?.request_id || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
    const returnFk = trigger?.dataset?.fk || `rec|${oppKey(o)}`;
    // 額度只在 Python 物化狀態有效時顯示；重算中 / 過期不顯示建議金額，也不預填
    const capacityText = valid && o.suggested_stake_amount != null
      ? `${amt(o.suggested_stake_amount, cur)}（${pct(o.user_adjusted_fraction)} of day-start ${amt(o.day_start_bankroll)}）`
      : '—（目前額度無法驗證）';
    const context = primary ? '' : `
        <p class="rec-context"><i class="fas fa-circle-info mr-1.5" aria-hidden="true"></i>目前狀態：${esc(groupZh)}${valid ? '' : '（額度無法驗證）'}。
          送出時由伺服器依最新狀態判斷合規；不符合策略時會先要求你確認。</p>`;
    const fill = valid && o.suggested_stake_amount != null
      ? `<button type="button" class="rec-fill" data-fill="${Number(o.suggested_stake_amount)}">帶入建議上限 ${amt(o.suggested_stake_amount, cur)}</button>` : '';
    modalRoot.innerHTML = `
    <div class="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="rec-title">
      <form id="rec-form" class="modal-card">
        <h2 id="rec-title" class="text-sm font-semibold mb-1"><i class="fas fa-pen-to-square text-orange-400 mr-1.5" aria-hidden="true"></i>記錄下注</h2>
        <p class="text-xs text-slate-400 mb-3">請先在台灣運彩完成下注，再填入<strong>實際成交</strong>的賠率與金額。平台不會替你下注。</p>
        ${context}
        <dl class="kv mb-3">
          <div><dt>比賽</dt><dd>${NBA.matchup(g.away, g.home)} · ${esc(g.tipoff_display)}</dd></div>
          <div><dt>來源</dt><dd>台灣運彩（${esc(o.bookmaker)}）</dd></div>
          <div><dt>玩法 / 方向</dt><dd>${esc(MARKET[o.market] || o.market)} · ${sideText(o, g)}</dd></div>
          <div><dt>平台觀察賠率</dt><dd class="num">${NBA.odds(o.decimal_odds)}（${NBA.tpe(o.odds_fetched_at)}）</dd></div>
          <div><dt>建議最大新增額度</dt><dd class="num">${capacityText}</dd></div>
          <div><dt>EV（平台觀察賠率）</dt><dd class="num">${evText(o.ev_per_unit)} · 尚在前瞻驗證</dd></div>
        </dl>
        <div class="grid grid-cols-2 gap-3">
          <label class="block"><span class="text-xs text-slate-400">實際賠率</span>
            <input name="odds" type="number" inputmode="decimal" step="0.01" min="1.01" required value="${draft?.odds ?? o.decimal_odds ?? ''}" class="inp num" /></label>
          <label class="block"><span class="text-xs text-slate-400">實際金額（${esc(cur || '—')}）</span>
            <input name="stake" type="number" inputmode="numeric" step="1" min="1" required value="${esc(draft?.stake ?? '')}" class="inp num" /></label>
          ${fill ? `<div class="col-span-2 -mt-1">${fill}</div>` : ''}
          <label class="block col-span-2"><span class="text-xs text-slate-400">下注時間（留空＝現在；晚點才記錄請改成實際下注時間）</span>
            <input name="placed_at" type="datetime-local" max="${localNow()}" value="${esc(draft?.placed_at ?? '')}" class="inp" /></label>
          <label class="block col-span-2"><span class="text-xs text-slate-400">備註（選填）</span>
            <input name="note" type="text" maxlength="200" value="${esc(draft?.note ?? '')}" class="inp" /></label>
        </div>
        <p id="rec-odds-note" class="text-xs text-slate-400 mt-2 hidden">實際賠率與平台觀察不同：兩者都會保存（參考盤口快照不會被覆寫）。</p>
        <div id="rec-confirm" class="hidden mt-3 rounded border border-amber-800/60 bg-amber-950/30 p-2 text-xs text-amber-200"></div>
        <div class="modal-actions">
          <span id="rec-msg" class="text-xs mr-auto" role="status"></span>
          <button type="button" id="rec-cancel" class="rec-secondary">取消</button>
          <button type="submit" id="rec-submit" class="rec-primary">確認記錄</button>
        </div>
      </form>
    </div>`;
    const form = $('rec-form');
    const close = () => {
      modalRoot.innerHTML = '';
      setBackgroundInert(false);
      // 焦點回到該機會的按鈕（從底部列 / 橫幅恢復草稿時，按鈕可能在畫面外，讓瀏覽器捲到它）
      document.querySelector(`[data-fk="${CSS.escape(returnFk)}"]`)?.focus();
    };
    const cancel = () => { clearDraft(); close(); renderBanner(board); renderDock(board); };
    setBackgroundInert(true);
    modalRoot.onkeydown = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); cancel(); } };
    $('rec-cancel').onclick = cancel;
    const syncOddsNote = () => $('rec-odds-note').classList.toggle('hidden', Number(form.odds.value) === Number(o.decimal_odds));
    syncOddsNote();
    form.odds.addEventListener('input', syncOddsNote);
    form.querySelector('[data-fill]')?.addEventListener('click', (ev) => {
      form.stake.value = ev.currentTarget.dataset.fill;
      form.stake.dispatchEvent(new Event('input', { bubbles: true }));
      form.stake.focus();
    });
    // 一開始輸入就存草稿（同一個 request id），被打斷後可從橫幅或底部列接著填
    form.addEventListener('input', () => saveDraft({
      opp_id: o.id, key: oppKey(o), game_id: g.game_id, betting_day: board.betting_day, request_id: requestId,
      label: `${NBA.teamName(g.away)} @ ${NBA.teamName(g.home)} ${MARKET[o.market] || o.market} ${sidePlain(o, g)}${form.stake.value ? ` · ${form.stake.value}` : ''}`,
      odds: form.odds.value, stake: form.stake.value, placed_at: form.placed_at.value, note: form.note.value,
    }));
    (form.stake.value ? $('rec-submit') : form.stake).focus();
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
      msg.className = 'text-xs text-slate-400 mr-auto';
      msg.textContent = '送出中…';
      try {
        const res = await NBA.post('/api/bets', payload);
        clearDraft();
        showReceipt(res, payload);
      } catch (e) {
        $('rec-submit').disabled = false;
        if (e.status === 409 && e.data?.error === 'confirmation_required') {
          needConfirm = e.data;
          const box = $('rec-confirm');
          box.classList.remove('hidden');
          box.innerHTML = `${NBA.confirmationHtml(e.data, { stake: payload.stake, currency: cur })}
            <label class="flex items-center gap-2 mt-2"><input id="rec-ack" type="checkbox" class="check" />我確認這是已在外部完成的實際下注</label>
            ${e.data.requires_reason ? '<label class="block mt-2"><span class="text-xs text-amber-200">原因（必填）</span><input id="rec-reason" type="text" maxlength="300" class="inp" /></label>' : ''}`;
          msg.className = 'text-xs text-amber-300 mr-auto';
          msg.textContent = '需要確認後再送出一次';
          $('rec-ack').focus();
        } else if (e.status === 409) {
          msg.className = 'text-xs text-red-400 mr-auto';
          msg.textContent = e.data?.message || e.message;
        } else {
          msg.className = 'text-xs text-red-400 mr-auto';
          msg.textContent = e.status ? e.message : `${e.message}（連線中斷？草稿已保留，可直接再按一次；同一筆不會重複建立）`;
        }
      }
    });

    // 成功後留在視窗裡顯示收據，按「完成」才關閉並重新載入決策板
    function showReceipt(res, payload) {
      const bet = res.bet || {};
      const compliance = bet.strategy_compliance || res.risk_check?.compliance;
      form.outerHTML = `
      <div class="modal-card" role="document">
        <h2 id="rec-title" class="text-sm font-semibold mb-1"><i class="fas fa-circle-check text-green-400 mr-1.5" aria-hidden="true"></i>${res.replay ? `這筆先前已記錄（#${res.id}），未重複建立` : `已記錄 #${res.id}`}</h2>
        <p class="text-xs text-slate-400 mb-3" role="status">風險額度重新計算中，約一分鐘內更新；更新前決策板不顯示可用額度。</p>
        <dl class="kv mb-1">
          <div><dt>比賽</dt><dd>${NBA.matchup(g.away, g.home)}</dd></div>
          <div><dt>玩法 / 方向</dt><dd>${esc(MARKET[o.market] || o.market)} · ${sideText(o, g)}</dd></div>
          <div><dt>實際賠率</dt><dd class="num">${NBA.odds(bet.odds ?? payload.odds)}</dd></div>
          <div><dt>實際金額</dt><dd class="num">${amt(bet.stake ?? payload.stake, cur)}</dd></div>
          <div><dt>策略判定</dt><dd>${esc(NBA.COMPLIANCE_TEXT[compliance] || compliance || '—')}</dd></div>
        </dl>
        <div class="modal-actions">
          <a href="/bets" class="rec-secondary">查看投注紀錄</a>
          <button type="button" id="rec-done" class="rec-primary">完成</button>
        </div>
      </div>`;
      modalRoot.onkeydown = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); done(); } };
      const done = () => { close(); load(currentDate, { silent: true }); };
      $('rec-done').onclick = done;
      $('rec-done').focus();
    }
  }

  /* ------------------------------ load ------------------------------ */

  function syncDate(date) {
    if (picker) picker.value = date;
    // 標題跟著日期走：切到明日時不再寫「今日」
    const title = $('decision-title');
    if (title) {
      const name = date === NBA.tpeDateStr(0) ? '今日決策中心' : date === NBA.tpeDateStr(1) ? '明日決策中心' : `決策中心 · ${date.slice(5).replace('-', '/')}`;
      title.textContent = name;
      document.title = `${name} — NBA 預測平台`;
    }
    tabs.forEach((t) => {
      const on = NBA.tpeDateStr(Number(t.dataset.dday)) === date;
      t.classList.toggle('bg-orange-500/15', on);
      t.classList.toggle('text-orange-300', on);
      t.classList.toggle('font-medium', on);
      t.classList.toggle('text-slate-400', !on);
      t.setAttribute('aria-pressed', String(on));
    });
    const url = new URL(location.href);
    if (url.searchParams.get('date') !== date) {
      url.searchParams.set('date', date);
      history.replaceState(history.state, '', url);
    }
  }

  // 背景刷新時保留：展開中的明細、捲動位置、鍵盤焦點（以跨物化穩定的鍵對應）
  function captureView() {
    return {
      open: [...document.querySelectorAll('#decision-page details[open][data-k]')].map((el) => el.dataset.k),
      focus: document.activeElement?.closest?.('[data-fk]')?.dataset.fk || null,
      y: window.scrollY,
    };
  }
  function restoreView(v) {
    for (const k of v.open) document.querySelector(`#decision-page details[data-k="${CSS.escape(k)}"]`)?.setAttribute('open', '');
    if (Math.abs(window.scrollY - v.y) > 2) window.scrollTo(0, v.y);
    if (v.focus) document.querySelector(`[data-fk="${CSS.escape(v.focus)}"]`)?.focus({ preventScroll: true });
  }

  /**
   * silent：自動刷新 / 回到分頁 / 記錄完成後使用，不清空畫面、不閃骨架，保留閱讀狀態；記錄視窗開著時不刷新。
   * 失敗時一律改顯示錯誤、不保留舊額度（無法確認是否仍有效）。
   */
  async function load(date, { silent = false } = {}) {
    if (silent && modalRoot.innerHTML) return;
    const keep = silent && date === currentDate && board ? captureView() : null;
    const seq = ++loadSeq;
    currentDate = date;
    syncDate(date);
    if (!keep) {
      gamesEl.innerHTML = `<div class="grid gap-4">${NBA.skeletonCards(3)}</div>`;
      summaryEl.innerHTML = '';
      banner.innerHTML = '';
      renderDock(null);
    }
    try {
      const next = await NBA.get(`/api/decision-board?date=${encodeURIComponent(date)}`);
      if (seq !== loadSeq) return; // 期間又切換了日期 / 重新載入：丟棄較舊的回應
      board = next;
      lastLoadedAt = Date.now();
      renderBanner(board);
      renderSummary(board);
      renderGames(board);
      renderEvidence(board);
      renderHealth(board);
      renderDock(board);
      if (keep) restoreView(keep);
      if (resumeAfterLoad) { resumeAfterLoad = false; resumeDraft(); }
    } catch (e) {
      if (seq !== loadSeq) return;
      board = null;
      summaryEl.innerHTML = '';
      banner.innerHTML = '';
      renderDock(null);
      if (e.status === 401) {
        gamesEl.innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-6 text-center">
          <i class="fas fa-lock text-2xl text-slate-400 mb-2"></i>
          <p class="text-sm text-slate-400 mb-3">決策中心包含個人 bankroll 與實際下注，請先登入</p>
          <a href="/login?next=${encodeURIComponent(location.pathname + location.search)}" class="inline-block px-3 py-1.5 rounded bg-orange-500 text-white text-sm">前往登入</a></div>`;
      } else {
        gamesEl.innerHTML = NBA.error(`載入決策中心失敗：${e.message}（API 錯誤；不顯示任何額度）`);
      }
    }
  }

  function resumeDraft() {
    const hit = draftTarget(readDraft(), board);
    if (hit) openRecord(hit.o.id, null, readDraft());
  }

  // 事件委派：背景刷新重建 DOM 後，按鈕仍然有效
  gamesEl.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-opp]');
    if (btn) openRecord(Number(btn.dataset.opp), btn);
  });
  banner.addEventListener('click', (ev) => {
    const act = ev.target.closest('[data-draft]')?.dataset.draft;
    if (act === 'resume') resumeDraft();
    else if (act === 'goto') { const dr = readDraft(); if (dr) { resumeAfterLoad = true; load(dr.betting_day); } }
    else if (act === 'discard') { clearDraft(); if (board) { renderBanner(board); renderDock(board); } }
  });
  dock?.addEventListener('click', (ev) => {
    const act = ev.target.closest('[data-dock]')?.dataset.dock;
    if (act === 'resume') resumeDraft();
    else if (act === 'next') jumpToNextActionable();
  });

  tabs.forEach((t) => t.addEventListener('click', () => load(NBA.tpeDateStr(Number(t.dataset.dday)))));
  if (picker) picker.addEventListener('change', () => picker.value && load(picker.value));
  $('decision-refresh').addEventListener('click', () => load(currentDate));
  window.addEventListener('nba:twsport-synced', () => load(currentDate, { silent: true }));
  setInterval(() => { if (document.visibilityState === 'visible') load(currentDate, { silent: true }); }, 60000);
  // 從台彩 App 切回來（或頁面從 bfcache 還原）時立即更新，不等下一個 60 秒
  const refreshIfIdle = () => { if (document.visibilityState === 'visible' && Date.now() - lastLoadedAt > 20000) load(currentDate, { silent: true }); };
  document.addEventListener('visibilitychange', refreshIfIdle);
  window.addEventListener('pageshow', (ev) => { if (ev.persisted) refreshIfIdle(); });
  load(currentDate);
})();
