/**
 * Strategy bankroll（Phase D.5）
 * 手動維護的策略資金：append-only ledger、day-start 凍結、使用者實際下注紀錄（不是策略績效）。
 * 所有金額合計 / 未結算 stake / 可用資金 / day-start 皆由 Python 物化；這裡只顯示與送出使用者輸入。
 */

(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => NBA.esc(s);
  const amt = (v, cur) => NBA.money(v, cur);
  const TYPE = {
    initial_funding: '初始資金', deposit: '存入', withdrawal: '提出', adjustment: '手動調整', reversal: '沖銷',
    bet_settlement: '注單結算損益', bet_settlement_reversal: '結算沖銷',
  };
  let data = null;
  let flash = null;
  const ERROR_TEXT = {
    recalculation_pending: '資金正在重新計算，請約一分鐘後再試（不以過期餘額判斷出金）',
    exceeds_available_bankroll: '出金金額大於可用資金（已扣除未結算下注）',
    risk_state_changed: '另一個操作剛更新了資金，請重新整理後再試',
  };
  const COMPOSITION = { ...NBA.ORIGIN_TEXT, ...NBA.COMPLIANCE_TEXT };

  function tile(label, value, sub = '') {
    return `<div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2"><div class="text-xs text-slate-400">${label}</div>
      <div class="text-sm font-semibold num">${value}</div>${sub ? `<div class="text-xs text-slate-400">${sub}</div>` : ''}</div>`;
  }

  function renderSummary() {
    const el = $('bankroll-summary');
    if (data.status === 'unavailable') { el.innerHTML = NBA.error('策略資金資料表尚未建立（migration 0008 未套用）'); return; }
    if (!data.configured) {
      el.innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-4 text-sm text-slate-300">
        <i class="fas fa-wallet mr-1.5 text-slate-400"></i>尚未設定策略資金。設定初始資金後，決策中心才會產生可操作金額（當日起始資金在當天第一次需要時凍結）。</div>`;
      return;
    }
    const s = data.summary;
    const cur = data.account.currency;
    const pending = data.summary_state && data.summary_state.status !== 'current';
    el.innerHTML = `
      ${pending ? `<div class="rounded-lg border border-amber-900/60 bg-amber-950/20 px-3 py-2 text-xs text-amber-200 mb-2">
        <i class="fas fa-rotate mr-1"></i>最近的異動尚未重新計算，以下數字可能過期，約一分鐘內更新。</div>` : ''}
      ${s ? `<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        ${tile('目前資金', amt(s.current_bankroll, cur), `異動 ${s.ledger_entries} 筆`)}
        ${tile('未結算下注', amt(s.committed_open_stake, cur), `${s.n_open_bets} 筆`)}
        ${tile('可用資金', amt(s.available_bankroll, cur))}
        ${tile(`當日起始資金（${esc(data.summary_betting_day || '—')}）`, amt(s.day_start_bankroll, cur), s.day_start_basis_as_of ? `凍結於 ${NBA.tpe(s.day_start_basis_as_of)}` : (s.day_start_unavailable_reason || '尚未凍結'))}
        ${tile('當日已用風險', NBA.pct(s.daily_risk_used_fraction, 2), `上限 ${NBA.pct(s.daily_risk_cap_fraction, 0)}`)}
        ${tile('剩餘風險預算', NBA.pct(s.remaining_risk_budget_fraction, 2), amt(s.remaining_risk_budget_amount, cur))}
      </div>` : '<p class="text-xs text-slate-400">尚無資金摘要（排程每分鐘計算一次）。</p>'}`;
  }

  function renderForm() {
    const el = $('bankroll-form');
    if (data.status === 'unavailable') { el.innerHTML = ''; return; }
    const first = !data.configured;
    const userEntries = (data.ledger || []).filter((e) => e.recorded_by === 'user' && e.entry_type !== 'reversal');
    const cur = data.account?.currency || '';
    const requestId = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
    el.innerHTML = `
    <form id="entry-form" class="panel">
      <h2><i class="fas fa-plus mr-1.5" aria-hidden="true"></i>${first ? '建立策略資金（初始資金）' : '新增異動'}</h2>
      <div class="grid grid-cols-2 gap-3">
        <label class="block"><span class="text-xs text-slate-400">類型</span>
          <select name="entry_type" class="inp">
            ${first ? '<option value="initial_funding">初始資金</option>'
              : ['deposit', 'withdrawal', 'adjustment', 'reversal'].map((t) => `<option value="${t}">${TYPE[t]}</option>`).join('')}
          </select></label>
        <label class="block" id="amount-wrap"><span class="text-xs text-slate-400">金額</span>
          <input name="amount" type="number" inputmode="decimal" step="0.01" class="inp num" /></label>
        ${first ? `<label class="block"><span class="text-xs text-slate-400">幣別</span>
          <input name="currency" value="TWD" maxlength="3" class="inp" /></label>` : ''}
        <label class="block hidden" id="reverse-wrap"><span class="text-xs text-slate-400">沖銷哪一筆</span>
          <select name="reverses_entry_id" class="inp">${userEntries.map((e) => `<option value="${e.id}">#${e.id} ${TYPE[e.entry_type]} ${amt(e.amount)}</option>`).join('')}</select></label>
        <label class="block col-span-2"><span class="text-xs text-slate-400">原因（調整 / 沖銷必填）</span>
          <input name="reason" type="text" maxlength="300" class="inp" /></label>
      </div>
      <p class="text-xs text-slate-400 mt-2">金額一律填正數（方向由類型決定；調整可為負數）。紀錄不可修改或刪除，錯誤請用「沖銷」。</p>
      <div class="flex items-center gap-3 mt-3">
        <button type="submit" class="rec-primary">${first ? '建立資金' : '送出異動'}</button>
        <span id="entry-msg" class="text-xs ${flash ? (flash.ok ? 'text-green-400' : 'text-red-400') : ''}" role="status">${flash ? esc(flash.text) : ''}</span>
      </div>
    </form>`;
    flash = null;
    const form = $('entry-form');
    const sync = () => {
      const t = form.entry_type.value;
      $('reverse-wrap').classList.toggle('hidden', t !== 'reversal');
      $('amount-wrap').classList.toggle('hidden', t === 'reversal');
    };
    form.entry_type.addEventListener('change', sync);
    sync();
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const msg = $('entry-msg');
      const fail = (text, el) => { msg.className = 'text-xs text-red-400'; msg.textContent = text; el && el.focus(); };
      const t = form.entry_type.value;
      const reason = form.reason.value.trim();
      const payload = { entry_type: t, reason: reason || null, client_request_id: requestId };
      if (t === 'reversal') {
        if (!form.reverses_entry_id.value) return fail('目前沒有可沖銷的使用者異動');
        payload.reverses_entry_id = Number(form.reverses_entry_id.value);
      } else {
        const a = form.amount.value === '' ? NaN : Number(form.amount.value);
        if (!Number.isFinite(a)) return fail('請填金額（數字）', form.amount);
        if (t === 'adjustment' ? a === 0 : a <= 0) return fail(t === 'adjustment' ? '調整金額不能是 0（可為負數）' : '金額請填大於 0 的數字；方向由類型決定', form.amount);
        payload.amount = a;
      }
      if ((t === 'adjustment' || t === 'reversal') && !reason) return fail(`${TYPE[t]}需要填寫原因`, form.reason);
      if (form.currency) payload.currency = form.currency.value.toUpperCase();
      const send = async () => {
        try {
          await NBA.post('/api/bankroll/entries', payload);
          // 成功訊息跨過重新載入保留（renderForm 會重建表單）
          flash = { ok: true, text: `已記錄${TYPE[t]}${payload.amount != null ? ' ' + amt(payload.amount, cur) : ''}；資金摘要約一分鐘內由 Python 重新計算。` };
          load();
        } catch (e) {
          throw new Error(ERROR_TEXT[e.data?.error] || e.data?.message || e.message);
        }
      };
      // 出金 / 調整 / 沖銷會減少或改寫資金基準：先回顯金額再確認
      if (t === 'withdrawal' || t === 'adjustment' || t === 'reversal') {
        const target = t === 'reversal' ? (data.ledger || []).find((e) => String(e.id) === String(payload.reverses_entry_id)) : null;
        NBA.dialog({
          title: `確認${TYPE[t]}？`,
          body: `<dl class="kv mb-2">
              <div><dt>類型</dt><dd>${esc(TYPE[t])}</dd></div>
              ${t === 'reversal' ? `<div><dt>沖銷</dt><dd>#${target?.id ?? payload.reverses_entry_id} ${esc(TYPE[target?.entry_type] || '')} ${amt(target?.amount, cur)}</dd></div>`
                : `<div><dt>金額</dt><dd class="num">${amt(payload.amount, cur)}</dd></div>`}
              ${reason ? `<div><dt>原因</dt><dd>${esc(reason)}</dd></div>` : ''}
            </dl><p class="text-slate-400">紀錄不可修改或刪除；錯了只能再沖銷。</p>`,
          confirmText: `確認${TYPE[t]}`,
          tone: t === 'withdrawal' ? 'danger' : 'primary',
          onConfirm: send,
        });
        return;
      }
      msg.className = 'text-xs text-slate-400';
      msg.textContent = '送出中…';
      try { await send(); } catch (e) { fail(e.message); }
    });
  }

  function renderPerformance() {
    const el = $('bankroll-performance');
    const p = data.actual_performance;
    if (!p) { el.innerHTML = '<section class="panel"><h2>你的實際下注紀錄</h2><p class="text-xs text-slate-400">尚無資料</p></section>'; return; }
    const cur = data.account?.currency;
    el.innerHTML = `
    <section class="panel">
      <h2><i class="fas fa-user mr-1.5" aria-hidden="true"></i>你的實際下注紀錄</h2>
      <p class="text-xs text-amber-300/90 mb-2">這是你的實際下注，不是模型策略績效（可能包含手動、超額確認、舊版注單）。策略的前瞻驗證見決策中心「證據狀態」。</p>
      <dl class="kv">
        <div><dt>已結算投注額</dt><dd>${amt(p.total_staked_graded, cur)}</dd></div>
        <div><dt>已實現損益</dt><dd class="${p.realized_pnl > 0 ? 'text-green-400' : p.realized_pnl < 0 ? 'text-red-400' : ''}">${NBA.signed(p.realized_pnl, 0)}</dd></div>
        <div><dt>未結算下注</dt><dd>${amt(p.open_stake, cur)}</dd></div>
        <div><dt>勝 / 負 / 退 / 作廢 / 待結算</dt><dd>${p.wins} / ${p.losses} / ${p.pushes} / ${p.voids} / ${p.pending}</dd></div>
        <div><dt>實際 yield</dt><dd>${p.actual_yield == null ? '—' : NBA.signed(p.actual_yield * 100, 2) + '%'}</dd></div>
        <div><dt>組成（來源）</dt><dd>${Object.entries(p.composition_by_origin || {}).map(([k, v]) => `${esc(COMPOSITION[k] || k)} ${v}`).join('、') || '—'}</dd></div>
        <div><dt>組成（策略合規）</dt><dd>${Object.entries(p.composition_by_compliance || {}).map(([k, v]) => `${esc(COMPOSITION[k] || k)} ${v}`).join('、') || '—'}</dd></div>
      </dl>
    </section>`;
  }

  function renderDays() {
    const el = $('bankroll-days');
    const ds = data.day_snapshots || [];
    if (!data.configured) { el.innerHTML = ''; return; }
    el.innerHTML = `<section class="panel"><h2><i class="fas fa-calendar-day mr-1.5"></i>當日起始資金（凍結，不可修改）</h2>
      ${ds.length ? `<div class="overflow-x-auto"><table class="stat-table text-xs"><thead><tr><th scope="col">投注日</th><th scope="col">起始資金</th><th scope="col">帳上餘額</th><th scope="col">扣除未結算</th><th scope="col">基準時點</th><th scope="col">原因</th></tr></thead>
      <tbody>${ds.map((d) => `<tr><td>${esc(d.betting_day)}</td><td class="num">${amt(d.day_start_bankroll)}</td><td class="num">${amt(d.ledger_balance)}</td>
        <td class="num">${amt(d.open_stake_excluded)}</td><td>${NBA.tpe(d.basis_as_of)}</td><td>${esc(NBA.reasonText(d.established_reason))}</td></tr>`).join('')}</tbody></table></div>`
        : '<p class="text-xs text-slate-400">尚未凍結任何投注日。</p>'}</section>`;
  }

  function renderLedger() {
    const el = $('bankroll-ledger');
    const ls = data.ledger || [];
    if (!data.configured) { el.innerHTML = ''; return; }
    el.innerHTML = `<section class="panel"><h2><i class="fas fa-list mr-1.5"></i>資金異動紀錄（只可新增，供稽核）</h2>
      <div class="overflow-x-auto"><table class="stat-table text-xs"><thead><tr><th scope="col">#</th><th scope="col">時間</th><th scope="col">類型</th><th scope="col">金額</th><th scope="col">注單</th><th scope="col">沖銷</th><th scope="col">原因</th><th scope="col">來源</th></tr></thead>
      <tbody>${ls.map((e) => `<tr><td>${e.id}</td><td>${NBA.tpe(e.recorded_at)}</td><td>${esc(TYPE[e.entry_type] || e.entry_type)}</td>
        <td class="num">${e.amount == null ? '（沖銷原筆）' : amt(e.amount)}</td><td>${e.bet_id ? '#' + e.bet_id : ''}</td>
        <td>${e.reverses_entry_id ? '#' + e.reverses_entry_id : ''}</td><td class="max-w-[14rem] truncate">${esc(e.reason || '')}</td><td>${esc(({ user: '你', pipeline: '系統結算' })[e.recorded_by] || e.recorded_by)}</td></tr>`).join('')}</tbody></table></div></section>`;
  }

  async function load() {
    $('bankroll-summary').innerHTML = '<div class="skeleton h-20"></div>';
    try {
      data = await NBA.get('/api/bankroll');
      renderSummary(); renderForm(); renderPerformance(); renderDays(); renderLedger();
    } catch (e) {
      if (e.status === 401) {
        $('bankroll-summary').innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-6 text-center">
          <i class="fas fa-lock text-2xl text-slate-400 mb-2"></i><p class="text-sm text-slate-300 mb-3">策略資金為私人資料，請先登入</p>
          <a href="/login?next=/bankroll" class="inline-block px-3 py-1.5 rounded bg-orange-500 text-white text-sm">前往登入</a></div>`;
      } else {
        $('bankroll-summary').innerHTML = NBA.error(`載入策略資金失敗：${e.message}`);
      }
    }
  }
  load();
})();
