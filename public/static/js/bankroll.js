/**
 * Strategy bankroll（Phase D.5）
 * 手動維護的策略資金：append-only ledger、day-start 凍結、使用者實際下注紀錄（不是策略績效）。
 * 所有金額合計 / 未結算 stake / 可用資金 / day-start 皆由 Python 物化；這裡只顯示與送出使用者輸入。
 */

(function () {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => NBA.esc(s);
  const amt = (v, cur) => (v == null ? '—' : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}${cur ? ' ' + esc(cur) : ''}`);
  const TYPE = {
    initial_funding: '初始資金', deposit: '存入', withdrawal: '提出', adjustment: '手動調整', reversal: '沖銷',
    bet_settlement: '注單結算損益', bet_settlement_reversal: '結算沖銷',
  };
  let data = null;

  function tile(label, value, sub = '') {
    return `<div class="rounded-lg bg-slate-900/70 border border-slate-800 px-3 py-2"><div class="text-[10px] text-slate-500">${label}</div>
      <div class="text-sm font-semibold num">${value}</div>${sub ? `<div class="text-[10px] text-slate-500">${sub}</div>` : ''}</div>`;
  }

  function renderSummary() {
    const el = $('bankroll-summary');
    if (data.status === 'unavailable') { el.innerHTML = NBA.error('bankroll 資料表尚未建立（migration 0008 未套用）'); return; }
    if (!data.configured) {
      el.innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-4 text-sm text-slate-300">
        <i class="fas fa-wallet mr-1.5 text-slate-500"></i>尚未設定 strategy bankroll。設定初始資金後，決策中心才會產生可操作金額（day-start 凍結於當日第一個需要時點）。</div>`;
      return;
    }
    const s = data.summary;
    const cur = data.account.currency;
    const pending = data.summary_state && data.summary_state.status !== 'current';
    el.innerHTML = `
      ${pending ? `<div class="rounded-lg border border-amber-900/60 bg-amber-950/20 px-3 py-2 text-xs text-amber-200 mb-2">
        <i class="fas fa-rotate mr-1"></i>最近的異動尚未重新計算（${esc((data.summary_state.reasons || []).join('、'))}），以下數字可能過期，約一分鐘內更新。</div>` : ''}
      ${s ? `<div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        ${tile('目前 bankroll', amt(s.current_bankroll, cur), `ledger ${s.ledger_entries} 筆`)}
        ${tile('未結算 stake（pending capital）', amt(s.committed_open_stake, cur), `${s.n_open_bets} 筆`)}
        ${tile('可用 bankroll', amt(s.available_bankroll, cur))}
        ${tile(`Day-start（${esc(data.summary_betting_day || '—')}）`, amt(s.day_start_bankroll, cur), s.day_start_basis_as_of ? `凍結於 ${NBA.tpe(s.day_start_basis_as_of)}` : (s.day_start_unavailable_reason || '尚未凍結'))}
        ${tile('當日已用風險', NBA.pct(s.daily_risk_used_fraction, 2), `上限 ${NBA.pct(s.daily_risk_cap_fraction, 0)}`)}
        ${tile('剩餘風險預算', NBA.pct(s.remaining_risk_budget_fraction, 2), amt(s.remaining_risk_budget_amount, cur))}
      </div>` : '<p class="text-xs text-slate-500">尚無 Python 物化摘要（排程每分鐘執行）。</p>'}`;
  }

  function renderForm() {
    const el = $('bankroll-form');
    if (data.status === 'unavailable') { el.innerHTML = ''; return; }
    const first = !data.configured;
    const userEntries = (data.ledger || []).filter((e) => e.recorded_by === 'user' && e.entry_type !== 'reversal');
    el.innerHTML = `
    <form id="entry-form" class="panel">
      <h2><i class="fas fa-plus mr-1.5"></i>${first ? '建立 bankroll（初始資金）' : '新增異動'}</h2>
      <div class="grid grid-cols-2 gap-3">
        <label class="block"><span class="text-[11px] text-slate-500">類型</span>
          <select name="entry_type" class="inp">
            ${first ? '<option value="initial_funding">初始資金</option>'
              : ['deposit', 'withdrawal', 'adjustment', 'reversal'].map((t) => `<option value="${t}">${TYPE[t]}</option>`).join('')}
          </select></label>
        <label class="block" id="amount-wrap"><span class="text-[11px] text-slate-500">金額</span>
          <input name="amount" type="number" step="0.01" class="inp num" /></label>
        ${first ? `<label class="block"><span class="text-[11px] text-slate-500">幣別</span>
          <input name="currency" value="TWD" maxlength="3" class="inp" /></label>` : ''}
        <label class="block hidden" id="reverse-wrap"><span class="text-[11px] text-slate-500">沖銷哪一筆</span>
          <select name="reverses_entry_id" class="inp">${userEntries.map((e) => `<option value="${e.id}">#${e.id} ${TYPE[e.entry_type]} ${amt(e.amount)}</option>`).join('')}</select></label>
        <label class="block col-span-2"><span class="text-[11px] text-slate-500">原因（調整 / 沖銷必填）</span>
          <input name="reason" type="text" maxlength="300" class="inp" /></label>
      </div>
      <p class="text-[10px] text-slate-500 mt-2">金額一律填正數（方向由類型決定；調整可為負數）。紀錄不可修改或刪除，錯誤請用「沖銷」。</p>
      <div class="flex items-center gap-3 mt-3">
        <button type="submit" class="px-3 py-1.5 rounded bg-orange-500 hover:bg-orange-400 text-white text-xs font-medium">送出</button>
        <span id="entry-msg" class="text-xs"></span>
      </div>
    </form>`;
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
      const t = form.entry_type.value;
      const payload = { entry_type: t, reason: form.reason.value || null,
        client_request_id: crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) };
      if (t === 'reversal') payload.reverses_entry_id = Number(form.reverses_entry_id.value);
      else payload.amount = form.amount.value === '' ? null : Number(form.amount.value);
      if (form.currency) payload.currency = form.currency.value.toUpperCase();
      try {
        await NBA.post('/api/bankroll/entries', payload);
        msg.className = 'text-xs text-green-400';
        msg.textContent = '已記錄（Python 約一分鐘內重新計算）';
        load();
      } catch (e) {
        msg.className = 'text-xs text-red-400';
        msg.textContent = e.data?.message || e.message;
      }
    });
  }

  function renderPerformance() {
    const el = $('bankroll-performance');
    const p = data.actual_performance;
    if (!p) { el.innerHTML = '<section class="panel"><h2>User actual betting record</h2><p class="text-xs text-slate-500">尚無資料</p></section>'; return; }
    const cur = data.account?.currency;
    el.innerHTML = `
    <section class="panel">
      <h2><i class="fas fa-user mr-1.5"></i>User actual betting record</h2>
      <p class="text-[10px] text-amber-400/90 mb-2">使用者實際下注紀錄 — 不是模型策略績效（可能包含手動、override、legacy 注單）。策略的 prospective paper 績效見決策中心 Evidence。</p>
      <dl class="kv">
        <div><dt>已結算 stake（turnover）</dt><dd>${amt(p.total_staked_graded, cur)}</dd></div>
        <div><dt>已實現損益</dt><dd class="${p.realized_pnl > 0 ? 'text-green-400' : p.realized_pnl < 0 ? 'text-red-400' : ''}">${NBA.signed(p.realized_pnl, 0)}</dd></div>
        <div><dt>未結算 stake</dt><dd>${amt(p.open_stake, cur)}</dd></div>
        <div><dt>勝 / 負 / 退 / 作廢 / 待結算</dt><dd>${p.wins} / ${p.losses} / ${p.pushes} / ${p.voids} / ${p.pending}</dd></div>
        <div><dt>Actual yield</dt><dd>${p.actual_yield == null ? '—' : NBA.signed(p.actual_yield * 100, 2) + '%'}</dd></div>
        <div><dt>組成（來源）</dt><dd>${Object.entries(p.composition_by_origin || {}).map(([k, v]) => `${esc(k)} ${v}`).join('、') || '—'}</dd></div>
        <div><dt>組成（策略合規）</dt><dd>${Object.entries(p.composition_by_compliance || {}).map(([k, v]) => `${esc(k)} ${v}`).join('、') || '—'}</dd></div>
      </dl>
    </section>`;
  }

  function renderDays() {
    const el = $('bankroll-days');
    const ds = data.day_snapshots || [];
    if (!data.configured) { el.innerHTML = ''; return; }
    el.innerHTML = `<section class="panel"><h2><i class="fas fa-calendar-day mr-1.5"></i>Day-start bankroll（凍結，不可修改）</h2>
      ${ds.length ? `<div class="overflow-x-auto"><table class="stat-table text-xs"><thead><tr><th>Betting day</th><th>Day-start</th><th>Ledger 餘額</th><th>排除未結算</th><th>基準時點</th><th>原因</th></tr></thead>
      <tbody>${ds.map((d) => `<tr><td>${esc(d.betting_day)}</td><td class="num">${amt(d.day_start_bankroll)}</td><td class="num">${amt(d.ledger_balance)}</td>
        <td class="num">${amt(d.open_stake_excluded)}</td><td>${NBA.tpe(d.basis_as_of)}</td><td>${esc(d.established_reason)}</td></tr>`).join('')}</tbody></table></div>`
        : '<p class="text-xs text-slate-500">尚未凍結任何 betting day。</p>'}</section>`;
  }

  function renderLedger() {
    const el = $('bankroll-ledger');
    const ls = data.ledger || [];
    if (!data.configured) { el.innerHTML = ''; return; }
    el.innerHTML = `<section class="panel"><h2><i class="fas fa-list mr-1.5"></i>Ledger（append-only 稽核紀錄）</h2>
      <div class="overflow-x-auto"><table class="stat-table text-xs"><thead><tr><th>#</th><th>時間</th><th>類型</th><th>金額</th><th>注單</th><th>沖銷</th><th>原因</th><th>來源</th></tr></thead>
      <tbody>${ls.map((e) => `<tr><td>${e.id}</td><td>${NBA.tpe(e.recorded_at)}</td><td>${esc(TYPE[e.entry_type] || e.entry_type)}</td>
        <td class="num">${e.amount == null ? '（沖銷原筆）' : amt(e.amount)}</td><td>${e.bet_id ? '#' + e.bet_id : ''}</td>
        <td>${e.reverses_entry_id ? '#' + e.reverses_entry_id : ''}</td><td class="max-w-[14rem] truncate">${esc(e.reason || '')}</td><td>${esc(e.recorded_by)}</td></tr>`).join('')}</tbody></table></div></section>`;
  }

  async function load() {
    $('bankroll-summary').innerHTML = '<div class="skeleton h-20"></div>';
    try {
      data = await NBA.get('/api/bankroll');
      renderSummary(); renderForm(); renderPerformance(); renderDays(); renderLedger();
    } catch (e) {
      if (e.status === 401) {
        $('bankroll-summary').innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-6 text-center">
          <i class="fas fa-lock text-2xl text-slate-600 mb-2"></i><p class="text-sm text-slate-400 mb-3">Bankroll 為私人資料，請先登入</p>
          <a href="/login?next=/bankroll" class="inline-block px-3 py-1.5 rounded bg-orange-500 text-white text-sm">前往登入</a></div>`;
      } else {
        $('bankroll-summary').innerHTML = NBA.error(`載入 bankroll 失敗：${e.message}`);
      }
    }
  }
  load();
})();
