/**
 * 個人投注紀錄 (規格書 §3.3-10)
 * 需登入；賠率一律填台彩實際賠率。
 * 注單是 append-only 稽核資料：結算 / 作廢 / 更正都先確認，錯了只能沖銷或更正，所以表單盡量在送出前擋下錯誤。
 */

(function () {
  const root = document.getElementById('bets-root');
  const formWrap = document.getElementById('bet-form-wrap');
  const esc = (s) => NBA.esc(s);

  const MARKETS = [
    ['ml', '不讓分(獨贏)'],
    ['spread', '讓分'],
    ['total', '大小分'],
    ['h1_ml', '上半場獨贏'],
    ['h1_spread', '上半場讓分'],
    ['h1_total', '上半場大小分'],
  ];
  const SELECTIONS = [
    ['home', '主隊'],
    ['away', '客隊'],
    ['over', '大分'],
    ['under', '小分'],
    ['draw', '和局（上半場三向）'],
  ];
  // 玩法 → 可選方向（與伺服器 VALID_SELECTIONS 規則一致：draw 只用於上半場獨贏）
  const SIDES_FOR = {
    ml: ['home', 'away'], spread: ['home', 'away'], total: ['over', 'under'],
    h1_ml: ['home', 'away', 'draw'], h1_spread: ['home', 'away'], h1_total: ['over', 'under'],
  };
  const NEEDS_LINE = { spread: true, total: true, h1_spread: true, h1_total: true };
  const COMPLIANCE_CLS = {
    compliant: 'grp-actionable', manual_unlinked: 'grp-inactive', user_override: 'grp-blocked',
    outside_model: 'grp-review', missing_context: 'grp-review',
  };
  const RESULTS = [
    ['pending', '待結算'],
    ['win', '贏'],
    ['lose', '輸'],
    ['push', '和局退款'],
    ['void', '取消'],
  ];
  const RECORD_STATUS = { voided: '已作廢', superseded: '已更正', deleted: '已刪除' };
  let showInactive = false;
  let pendingConfirm = null;
  let games = [];

  const label = (arr, v) => (arr.find((x) => x[0] === v) || [v, v])[1];
  const gameById = (id) => games.find((g) => String(g.id) === String(id));
  const sideName = (sel, home, away) => sel === 'home' ? `主隊・${NBA.teamName(home)}` : sel === 'away' ? `客隊・${NBA.teamName(away)}` : label(SELECTIONS, sel);
  const localNow = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const newRequestId = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  const GAME_STATE = { live: '進行中', final: '已結束', postponed: '延賽' };

  function renderForm() {
    const params = new URLSearchParams(location.search);
    const preselect = params.get('game_id');
    // 預設選第一場「未開賽」的比賽；已開賽 / 已結束仍可選（補記早先下的注），但標示清楚
    const firstOpen = games.find((g) => g.status === 'scheduled');
    const selectedId = preselect && gameById(preselect) ? preselect : firstOpen ? String(firstOpen.id) : '';
    const options = games
      .map((g) => `<option value="${g.id}" ${String(g.id) === selectedId ? 'selected' : ''}>${NBA.tpe(g.date_utc)} ${esc(NBA.teamName(g.away))} @ ${esc(NBA.teamName(g.home))}${GAME_STATE[g.status] ? `（${GAME_STATE[g.status]}）` : ''}</option>`)
      .join('');
    const field = 'w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm inp-touch';

    formWrap.innerHTML = `
    <form id="bet-form" class="rounded-xl border border-slate-800 bg-slate-900/60 p-4 mb-5" novalidate>
      <h2 class="text-sm font-semibold text-slate-300 mb-1"><i class="fas fa-plus mr-1.5 text-slate-400" aria-hidden="true"></i>手動記錄實際下注</h2>
      <p class="text-xs text-slate-400 mb-3">平台機會請從<a href="/decision" class="text-orange-300 underline underline-offset-2">決策中心</a>按「記錄下注」，會保留盤口 / 定價 / 額度的對照；這裡記的是手動紀錄（仍計入當日與同場實際下注）。</p>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label class="block sm:col-span-2">
          <span class="text-xs text-slate-400">比賽</span>
          <select name="game_id" required class="${field}">
            ${options || '<option value="">（今明兩日沒有賽程）</option>'}
          </select>
        </label>
        <label class="block">
          <span class="text-xs text-slate-400">玩法</span>
          <select name="market" required class="${field}">
            ${MARKETS.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}
          </select>
        </label>
        <label class="block">
          <span class="text-xs text-slate-400">選擇方向</span>
          <select name="selection" required class="${field}"></select>
        </label>
        <label class="block" id="line-wrap">
          <span class="text-xs text-slate-400">盤線（台彩單上的數字，例如 -5.5、228.5）</span>
          <input name="line" type="number" inputmode="decimal" step="0.5" class="${field} num" />
        </label>
        <label class="block">
          <span class="text-xs text-slate-400">台彩實際賠率（十進位，大於 1）</span>
          <input name="odds" type="number" inputmode="decimal" step="0.01" min="1.01" required class="${field} num" />
        </label>
        <label class="block">
          <span class="text-xs text-slate-400">投注金額</span>
          <input name="stake" type="number" inputmode="numeric" step="1" min="1" required class="${field} num" />
        </label>
        <label class="block">
          <span class="text-xs text-slate-400">下注時間（留空＝現在）</span>
          <input name="placed_at" type="datetime-local" max="${localNow()}" class="${field}" />
        </label>
        <label class="block">
          <span class="text-xs text-slate-400">下注管道</span>
          <select name="source" class="${field}">
            <option value="twsport">台灣運彩</option><option value="other">其他</option>
          </select>
        </label>
        <label class="block sm:col-span-2 lg:col-span-3">
          <span class="text-xs text-slate-400">備註（選填）</span>
          <input name="note" type="text" maxlength="200" class="${field}" />
        </label>
      </div>
      <div id="bet-confirm"></div>
      <div class="flex flex-wrap items-center gap-3 mt-3">
        <button type="submit" class="rec-primary">新增紀錄</button>
        <span id="form-msg" class="text-xs" role="status"></span>
      </div>
    </form>`;

    const form = document.getElementById('bet-form');
    form.dataset.requestId = newRequestId();
    const syncSides = () => {
      const g = gameById(form.game_id.value);
      const allowed = SIDES_FOR[form.market.value] || ['home', 'away'];
      const prev = form.selection.value;
      form.selection.innerHTML = allowed.map((v) => `<option value="${v}">${esc(g ? sideName(v, g.home, g.away) : label(SELECTIONS, v))}</option>`).join('');
      if (allowed.includes(prev)) form.selection.value = prev;
      const needLine = !!NEEDS_LINE[form.market.value];
      document.getElementById('line-wrap').classList.toggle('hidden', !needLine);
      form.line.required = needLine;
      if (!needLine) form.line.value = '';
      clearConfirm();
    };
    const clearConfirm = () => { pendingConfirm = null; document.getElementById('bet-confirm').innerHTML = ''; };
    form.market.addEventListener('change', syncSides);
    form.game_id.addEventListener('change', syncSides);
    // 改了金額 / 賠率就要重新驗證，先前的確認不能沿用
    ['odds', 'stake', 'line'].forEach((n) => form[n].addEventListener('input', clearConfirm));
    syncSides();

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const msg = document.getElementById('form-msg');
      const fail = (text, el) => { msg.className = 'text-xs text-red-400'; msg.textContent = text; el && el.focus(); };
      const odds = Number(form.odds.value);
      const stake = Number(form.stake.value);
      if (!form.game_id.value) return fail('請選擇比賽', form.game_id);
      if (NEEDS_LINE[form.market.value] && (form.line.value === '' || !Number.isFinite(Number(form.line.value)))) return fail('這個玩法需要填盤線（台彩單上的數字）', form.line);
      if (!(odds > 1)) return fail('賠率請填大於 1 的十進位數字（例如台彩單上的 1.85）', form.odds);
      if (!(stake > 0)) return fail('金額請填大於 0 的數字', form.stake);
      if (form.placed_at.value && new Date(form.placed_at.value).getTime() > Date.now() + 5 * 60000) return fail('下注時間不能在未來', form.placed_at);
      const payload = {
        game_id: Number(form.game_id.value),
        market: form.market.value,
        selection: form.selection.value,
        line: NEEDS_LINE[form.market.value] ? Number(form.line.value) : null,
        odds, stake,
        note: form.note.value || null,
        source: form.source.value || 'twsport',
        placed_at: form.placed_at.value ? new Date(form.placed_at.value).toISOString() : undefined,
        client_request_id: form.dataset.requestId,
      };
      if (pendingConfirm) {
        const ack = document.getElementById('bet-ack');
        if (!ack || !ack.checked) return fail('請先勾選確認', ack);
        payload.confirm_override = true;
        payload.override_reason = (document.getElementById('bet-reason')?.value || '').trim();
        if (pendingConfirm.requires_reason && !payload.override_reason) return fail('請填寫原因', document.getElementById('bet-reason'));
      }
      const btn = form.querySelector('[type=submit]');
      btn.disabled = true;
      msg.className = 'text-xs text-slate-400';
      msg.textContent = '送出中…';
      try {
        const res = await NBA.post('/api/bets', payload);
        const comp = res.bet?.strategy_compliance;
        msg.className = 'text-xs text-green-400';
        msg.textContent = `${res.replay ? '這筆先前已記錄' : '已記錄'} #${res.id}${comp ? `（${NBA.COMPLIANCE_TEXT[comp] || comp}）` : ''}`;
        clearConfirm();
        form.reset();
        form.dataset.requestId = newRequestId();
        syncSides();
        loadBets();
      } catch (e) {
        if (e.status === 409 && e.data?.error === 'confirmation_required') {
          pendingConfirm = e.data;
          document.getElementById('bet-confirm').innerHTML = `
            <div class="rounded border border-amber-800/60 bg-amber-950/30 p-3 text-xs text-amber-100 mt-3">
              ${NBA.confirmationHtml(e.data, { stake })}
              <label class="flex items-center gap-2 mt-2"><input id="bet-ack" type="checkbox" class="check" />我確認這是已在外部完成的實際下注</label>
              ${e.data.requires_reason ? '<label class="block mt-2"><span class="text-xs text-amber-200">原因（必填）</span><input id="bet-reason" type="text" maxlength="300" class="inp" /></label>' : ''}
            </div>`;
          msg.className = 'text-xs text-amber-300';
          msg.textContent = '需要確認後再送出一次';
          document.getElementById('bet-ack').focus();
        } else {
          fail(e.data?.message || e.message || '送出失敗，請稍後再試（同一筆重送不會重複建立）');
        }
      } finally {
        btn.disabled = false;
      }
    });
  }

  function betRow(b) {
    const active = (b.record_status || 'active') === 'active';
    const resultCls = { win: 'text-green-400', lose: 'text-red-400', pending: 'text-slate-300' }[b.result] || 'text-slate-300';
    const pnl = b.result === 'pending' || !active ? null : (b.payout ?? 0) - b.stake;
    const comp = b.legacy ? 'legacy' : b.strategy_compliance;
    const compText = NBA.COMPLIANCE_TEXT[comp] || comp || '—';
    const refOdds = b.reference_decimal_odds != null && b.reference_decimal_odds !== b.odds
      ? `<div class="text-xs text-slate-400">平台觀察 ${NBA.odds(b.reference_decimal_odds)}</div>` : '';
    const matchup = `${esc(NBA.teamName(b.away_abbr))} @ ${esc(NBA.teamName(b.home_abbr))}`;
    const side = esc(sideName(b.selection, b.home_abbr, b.away_abbr));
    const what = `#${b.id} ${matchup} ${esc(label(MARKETS, b.market))} ${side}`;
    return `<tr class="${active ? '' : 'opacity-60'}">
      <td class="text-xs text-slate-400 whitespace-nowrap">${NBA.tpe(b.placed_at).slice(0, 9)}<div class="text-xs">#${b.id}</div></td>
      <td class="text-xs bet-matchup">${matchup}
        ${b.game_status === 'final' ? `<span class="text-slate-400 num ml-1">(${b.away_pts}-${b.home_pts})</span>` : ''}
      </td>
      <td class="text-xs whitespace-nowrap">${esc(label(MARKETS, b.market))}</td>
      <td class="text-xs bet-side">${side}</td>
      <td class="text-xs num">${b.line != null ? NBA.signed(b.line) : '—'}</td>
      <td class="text-xs num">${NBA.odds(b.odds)}${refOdds}</td>
      <td class="text-xs num">${NBA.money(b.stake)}</td>
      <td class="text-xs num ${pnl == null ? '' : pnl > 0 ? 'text-green-400' : pnl < 0 ? 'text-red-400' : ''}">${pnl == null ? '—' : NBA.signed(pnl, 0)}</td>
      <td>
        ${active ? `<select data-bet-id="${b.id}" data-prev="${esc(b.result)}" aria-label="結算結果：${what}" class="bet-result bg-slate-800 border border-slate-700 rounded px-1.5 py-1 text-xs ${resultCls}">
          ${RESULTS.map(([v, t]) => `<option value="${v}" ${b.result === v ? 'selected' : ''}>${t}</option>`).join('')}
        </select>${b.settlement_source ? `<div class="text-xs text-slate-400">${b.settlement_source === 'manual' ? '手動結算' : esc(b.settlement_source)}</div>` : ''}`
          : `<span class="text-xs text-slate-300">${esc(RECORD_STATUS[b.record_status] || b.record_status)}</span>`}
      </td>
      <td class="text-xs"><span class="grp-badge ${COMPLIANCE_CLS[comp] || 'grp-inactive'}">${esc(compText)}</span>
        <div class="text-xs text-slate-400">${esc(NBA.ORIGIN_TEXT[b.origin || 'legacy_unlinked'] || b.origin)}</div></td>
      <td class="text-xs text-slate-400 max-w-[10rem] truncate">${esc(b.note || b.void_reason || '')}</td>
      <td class="text-right whitespace-nowrap">
        ${active ? `<button type="button" data-fix="${b.id}" class="icon-btn hover:text-sky-300" aria-label="更正 ${what}" title="更正（建立新紀錄取代舊紀錄）"><i class="fas fa-pen" aria-hidden="true"></i></button>
        <button type="button" data-void="${b.id}" class="icon-btn hover:text-red-400" aria-label="作廢 ${what}" title="作廢（保留稽核紀錄）"><i class="fas fa-ban" aria-hidden="true"></i></button>` : ''}
      </td>
    </tr>`;
  }

  /** 注單摘要（對話框裡用，讓使用者確認改的是哪一筆） */
  const betSummary = (b) => `<dl class="kv mb-3">
      <div><dt>注單</dt><dd>#${b.id} · ${esc(NBA.teamName(b.away_abbr))} @ ${esc(NBA.teamName(b.home_abbr))}</dd></div>
      <div><dt>玩法 / 方向</dt><dd>${esc(label(MARKETS, b.market))} · ${esc(sideName(b.selection, b.home_abbr, b.away_abbr))}${b.line != null ? ' ' + NBA.signed(b.line) : ''}</dd></div>
      <div><dt>賠率 / 金額</dt><dd class="num">${NBA.odds(b.odds)} · ${NBA.money(b.stake)}</dd></div>
    </dl>`;

  function confirmResult(sel, b) {
    const to = sel.value;
    const revert = () => { sel.value = sel.dataset.prev; };
    NBA.dialog({
      title: `把 #${b.id} 結算為「${esc(label(RESULTS, to))}」？`,
      body: `${betSummary(b)}<p class="text-slate-400">手動結算會留下稽核紀錄；之後仍可改回，但每次變更都會被記錄。</p>`,
      confirmText: '確認結算',
      onCancel: revert,
      onConfirm: async () => {
        try {
          await NBA.patch(`/api/bets/${b.id}`, { result: to });
        } catch (e) { throw new Error(e.data?.message || e.message); }
        loadBets();
      },
    });
  }

  function confirmVoid(b) {
    NBA.dialog({
      title: `作廢注單 #${b.id}？`,
      body: `${betSummary(b)}
        <label class="block"><span class="text-xs text-slate-400">作廢原因（必填；紀錄不會刪除，會保留稽核軌跡）</span>
          <input name="reason" type="text" maxlength="300" required class="inp" /></label>`,
      confirmText: '作廢這筆',
      tone: 'danger',
      onConfirm: async (form) => {
        const reason = form.reason.value.trim();
        if (!reason) { form.reason.focus(); throw new Error('請填寫作廢原因'); }
        try {
          await NBA.post(`/api/bets/${b.id}/void`, { reason });
        } catch (e) { throw new Error(e.data?.message || e.message); }
        loadBets();
      },
    });
  }

  function openCorrection(b) {
    const requestId = newRequestId();
    let needConfirm = null;
    NBA.dialog({
      title: `更正注單 #${b.id}`,
      body: `${betSummary(b)}
        <p class="text-slate-400 mb-3">更正會建立一筆新紀錄並把舊紀錄標為「已更正」，不會覆寫原紀錄。</p>
        <div class="grid grid-cols-2 gap-3">
          <label class="block"><span class="text-xs text-slate-400">更正後賠率</span>
            <input name="odds" type="number" inputmode="decimal" step="0.01" min="1.01" value="${esc(b.odds)}" class="inp num" /></label>
          <label class="block"><span class="text-xs text-slate-400">更正後金額</span>
            <input name="stake" type="number" inputmode="numeric" step="1" min="1" value="${esc(b.stake)}" class="inp num" /></label>
          ${b.line != null ? `<label class="block col-span-2"><span class="text-xs text-slate-400">更正後盤線</span>
            <input name="line" type="number" inputmode="decimal" step="0.5" value="${esc(b.line)}" class="inp num" /></label>` : ''}
          <label class="block col-span-2"><span class="text-xs text-slate-400">更正原因（必填）</span>
            <input name="reason" type="text" maxlength="300" class="inp" /></label>
        </div>
        <div class="corr-confirm"></div>`,
      confirmText: '建立更正',
      onConfirm: async (form, { root }) => {
        const odds = Number(form.odds.value);
        const stake = Number(form.stake.value);
        const reason = form.reason.value.trim();
        if (!(odds > 1)) { form.odds.focus(); throw new Error('賠率請填大於 1 的十進位數字'); }
        if (!(stake > 0)) { form.stake.focus(); throw new Error('金額請填大於 0 的數字'); }
        if (form.line && (form.line.value === '' || !Number.isFinite(Number(form.line.value)))) { form.line.focus(); throw new Error('請填盤線'); }
        if (!reason) { form.reason.focus(); throw new Error('請填寫更正原因'); }
        const payload = { odds, stake, reason, client_request_id: requestId };
        if (form.line) payload.line = Number(form.line.value);
        if (needConfirm) {
          if (!form.querySelector('[name=ack]')?.checked) throw new Error('請先勾選確認');
          payload.confirm_override = true;
        }
        try {
          await NBA.post(`/api/bets/${b.id}/correction`, payload);
        } catch (e) {
          if (e.status === 409 && e.data?.error === 'confirmation_required') {
            needConfirm = e.data;
            root.querySelector('.corr-confirm').innerHTML = `<div class="rounded border border-amber-800/60 bg-amber-950/30 p-3 text-xs text-amber-100 mt-3">
              ${NBA.confirmationHtml(e.data, { stake })}
              <label class="flex items-center gap-2 mt-2"><input name="ack" type="checkbox" class="check" />我確認更正後的內容就是實際下注</label></div>`;
            root.querySelector('.dlg-msg').textContent = '需要確認後再送出一次';
            root.querySelector('[name=ack]').focus();
            return false;
          }
          throw new Error(e.data?.message || e.message);
        }
        loadBets();
      },
    });
  }

  async function loadBets() {
    const first = !root.querySelector('table, .bets-head');
    if (first) root.innerHTML = `<div class="skeleton h-40"></div>`;
    try {
      const d = await NBA.get(`/api/bets${showInactive ? '?include_inactive=1' : ''}`);
      const bets = d.bets || [];
      const s = d.summary;
      const head = `
        <div class="bets-head flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-slate-400 mb-3">
          <span>共 <span class="text-slate-100 font-semibold num">${s.total}</span> 筆</span>
          <span>命中率 <span class="text-slate-100 font-semibold num">${NBA.pct(s.hit_rate)}</span></span>
          <span>損益 <span class="${s.pnl > 0 ? 'text-green-400' : s.pnl < 0 ? 'text-red-400' : 'text-slate-100'} font-semibold num">${NBA.signed(s.pnl, 0)}</span></span>
          <span>實際 yield <span class="${(s.roi ?? 0) > 0 ? 'text-green-400' : s.roi != null ? 'text-red-400' : 'text-slate-100'} font-semibold num">${s.roi != null ? NBA.signed(s.roi * 100, 2) + '%' : '—'}</span></span>
          <span class="text-amber-300/90">你的實際下注紀錄（不是策略績效）</span>
          <label class="ml-auto flex items-center gap-2 min-h-[2.75rem] sm:min-h-0"><input type="checkbox" id="show-inactive" class="check" ${showInactive ? 'checked' : ''}/>顯示已作廢 / 已更正</label>
        </div>`;

      const bindToggle = () => {
        const toggle = document.getElementById('show-inactive');
        if (toggle) toggle.addEventListener('change', () => { showInactive = toggle.checked; loadBets(); });
      };
      if (!bets.length) {
        root.innerHTML = head + NBA.empty('尚無投注紀錄。從決策中心按「記錄下注」，或使用上方表單手動記錄。', 'fa-receipt');
        bindToggle();
        return;
      }
      root.innerHTML =
        head +
        `<div class="rounded-xl border border-slate-800 bg-slate-900/60 overflow-x-auto">
          <table class="stat-table bets-table text-sm">
            <caption class="sr-only">投注紀錄</caption>
            <thead><tr><th scope="col">下注日</th><th scope="col">比賽</th><th scope="col">玩法</th><th scope="col">方向</th><th scope="col">盤線</th><th scope="col">實際賠率</th><th scope="col">金額</th><th scope="col">損益</th><th scope="col">結果</th><th scope="col">策略判定</th><th scope="col">備註</th><th scope="col"><span class="sr-only">操作</span></th></tr></thead>
            <tbody>${bets.map(betRow).join('')}</tbody>
          </table>
        </div>`;

      const byId = (id) => bets.find((x) => String(x.id) === String(id));
      root.querySelectorAll('.bet-result').forEach((sel) => sel.addEventListener('change', () => confirmResult(sel, byId(sel.dataset.betId))));
      root.querySelectorAll('[data-void]').forEach((btn) => btn.addEventListener('click', () => confirmVoid(byId(btn.dataset.void))));
      root.querySelectorAll('[data-fix]').forEach((btn) => btn.addEventListener('click', () => openCorrection(byId(btn.dataset.fix))));
      bindToggle();
    } catch (e) {
      if (e.status === 401) {
        root.innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-6 text-center">
          <i class="fas fa-lock text-2xl text-slate-400 mb-2" aria-hidden="true"></i>
          <p class="text-sm text-slate-300 mb-3">投注紀錄為私人資料，請先登入</p>
          <a href="/login?next=/bets" class="rec-primary">前往登入</a>
        </div>`;
        formWrap.innerHTML = '';
      } else {
        root.innerHTML = NBA.error(`載入投注紀錄失敗：${e.message}`);
      }
    }
  }

  (async function init() {
    const user = await NBA.currentUser();
    if (!user) { loadBets(); return; }
    // 表單的比賽選單：今日 + 明日賽事
    try {
      const [t1, t2] = await Promise.all([
        NBA.get(`/api/games?date=${NBA.tpeDateStr(0)}`),
        NBA.get(`/api/games?date=${NBA.tpeDateStr(1)}`),
      ]);
      games = [...(t1.games || []), ...(t2.games || [])];
    } catch { games = []; }
    renderForm();
    loadBets();
  })();
})();
