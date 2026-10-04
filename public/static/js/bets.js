/**
 * 個人投注紀錄 (規格書 §3.3-10)
 * 需登入；賠率一律填台彩實際賠率。
 */

(function () {
  const root = document.getElementById('bets-root');
  const formWrap = document.getElementById('bet-form-wrap');

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
  const COMPLIANCE = {
    compliant: ['策略合規', 'grp-actionable'], manual_unlinked: ['手動', 'grp-inactive'], user_override: ['Override', 'grp-blocked'],
    outside_model: ['模型外', 'grp-review'], missing_context: ['缺少風險背景', 'grp-review'],
  };
  let showInactive = false;
  let pendingConfirm = null;
  const RESULTS = [
    ['pending', '待結算'],
    ['win', '贏'],
    ['lose', '輸'],
    ['push', '和局退款'],
    ['void', '取消'],
  ];

  const label = (arr, v) => (arr.find((x) => x[0] === v) || [v, v])[1];

  function renderForm(games) {
    const params = new URLSearchParams(location.search);
    const preselect = params.get('game_id');
    const options = games
      .map(
        (g) =>
          `<option value="${g.id}" ${String(g.id) === preselect ? 'selected' : ''}>${NBA.tpe(g.date_utc)} ${NBA.esc(g.away.abbr)} @ ${NBA.esc(g.home.abbr)}</option>`
      )
      .join('');

    formWrap.innerHTML = `
    <form id="bet-form" class="rounded-xl border border-slate-800 bg-slate-900/60 p-4 mb-5">
      <h2 class="text-sm font-semibold text-slate-300 mb-3"><i class="fas fa-plus mr-1.5 text-slate-500"></i>手動記錄實際下注</h2>
      <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label class="block sm:col-span-2">
          <span class="text-[11px] text-slate-500">比賽</span>
          <select name="game_id" required class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm">
            ${options || '<option value="">（無可選比賽，請先讓階段二寫入賽程）</option>'}
          </select>
        </label>
        <label class="block">
          <span class="text-[11px] text-slate-500">玩法</span>
          <select name="market" required class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm">
            ${MARKETS.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}
          </select>
        </label>
        <label class="block">
          <span class="text-[11px] text-slate-500">選擇方向</span>
          <select name="selection" required class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm">
            ${SELECTIONS.map(([v, t]) => `<option value="${v}">${t}</option>`).join('')}
          </select>
        </label>
        <label class="block">
          <span class="text-[11px] text-slate-500">盤線（獨贏可留空）</span>
          <input name="line" type="number" step="0.5" placeholder="-5.5"
            class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm num" />
        </label>
        <label class="block">
          <span class="text-[11px] text-slate-500">台彩賠率</span>
          <input name="odds" type="number" step="0.01" min="1.01" required placeholder="1.85"
            class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm num" />
        </label>
        <label class="block">
          <span class="text-[11px] text-slate-500">投注金額</span>
          <input name="stake" type="number" step="1" min="1" required placeholder="1000"
            class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm num" />
        </label>
        <label class="block">
          <span class="text-[11px] text-slate-500">下注管道</span>
          <select name="source" class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm">
            <option value="twsport">台灣運彩</option><option value="other">其他</option>
          </select>
        </label>
        <label class="block lg:col-span-1">
          <span class="text-[11px] text-slate-500">備註</span>
          <input name="note" type="text" placeholder="模型 edge 4.2%"
            class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-sm" />
        </label>
      </div>
      <div class="flex items-center gap-3 mt-3">
        <button type="submit" class="px-3 py-1.5 rounded bg-orange-500 hover:bg-orange-400 text-white text-sm font-medium">
          新增紀錄
        </button>
        <span id="form-msg" class="text-xs"></span>
      </div>
      <div id="bet-confirm"></div>
      <p class="text-[10px] text-slate-500 mt-2">手動記錄 = manual_unlinked（不是從平台機會記錄）：仍計入當日 / 同場 actual exposure。從決策中心按「記錄下注」可保留平台盤口 / 定價 / 額度的對照。</p>
    </form>`;

    document.getElementById('bet-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const fd = new FormData(ev.target);
      const msg = document.getElementById('form-msg');
      const payload = {
        game_id: Number(fd.get('game_id')),
        market: fd.get('market'),
        selection: fd.get('selection'),
        line: fd.get('line') === '' ? null : Number(fd.get('line')),
        odds: Number(fd.get('odds')),
        stake: Number(fd.get('stake')),
        note: fd.get('note') || null,
        source: fd.get('source') || 'twsport',
        client_request_id: ev.target.dataset.requestId,
      };
      if (pendingConfirm) {
        const ack = document.getElementById('bet-ack');
        if (!ack || !ack.checked) { msg.className = 'text-xs text-red-400'; msg.textContent = '請勾選確認'; return; }
        payload.confirm_override = true;
        payload.override_reason = (document.getElementById('bet-reason')?.value || '').trim();
      }
      try {
        const res = await NBA.post('/api/bets', payload);
        msg.className = 'text-xs text-green-400';
        msg.textContent = `已記錄 #${res.id}${res.bet?.strategy_compliance ? '（' + res.bet.strategy_compliance + '）' : ''}`;
        pendingConfirm = null;
        document.getElementById('bet-confirm').innerHTML = '';
        ev.target.reset();
        ev.target.dataset.requestId = newRequestId();
        loadBets();
      } catch (e) {
        if (e.status === 409 && e.data?.error === 'confirmation_required') {
          pendingConfirm = e.data;
          document.getElementById('bet-confirm').innerHTML = `
            <div class="rounded border border-amber-800/60 bg-amber-950/30 p-2 text-[11px] text-amber-200 mt-3">
              <p class="font-semibold mb-1"><i class="fas fa-triangle-exclamation mr-1"></i>${NBA.esc(e.data.message)}</p>
              <ul class="list-disc ml-4 mb-1">${(e.data.checks || []).map((c) => `<li>${NBA.esc(c)}</li>`).join('')}</ul>
              <p class="mb-1">確認後記錄為 <code>${NBA.esc(e.data.compliance_if_confirmed)}</code>（仍計入 actual exposure；平台不會修改你的金額）。</p>
              <label class="flex items-center gap-1.5"><input id="bet-ack" type="checkbox" />我確認這是已在外部完成的實際下注</label>
              ${e.data.requires_reason ? '<input id="bet-reason" type="text" maxlength="300" placeholder="原因（必填）" class="inp mt-1" />' : ''}
            </div>`;
          msg.className = 'text-xs text-amber-300';
          msg.textContent = '需要明確確認後再送出';
        } else {
          msg.className = 'text-xs text-red-400';
          msg.textContent = e.data?.message || e.message;
        }
      }
    });
    document.getElementById('bet-form').dataset.requestId = newRequestId();
  }

  function newRequestId() {
    return crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2);
  }

  function betRow(b) {
    const active = (b.record_status || 'active') === 'active';
    const resultCls = { win: 'text-green-400', lose: 'text-red-400', pending: 'text-slate-400' }[b.result] || 'text-slate-400';
    const pnl = b.result === 'pending' || !active ? null : (b.payout ?? 0) - b.stake;
    const [cText, cCls] = COMPLIANCE[b.strategy_compliance] || (b.legacy ? ['legacy', 'grp-inactive'] : [b.strategy_compliance || '—', 'grp-inactive']);
    const refOdds = b.reference_decimal_odds != null && b.reference_decimal_odds !== b.odds
      ? `<div class="text-[10px] text-slate-500">平台觀察 ${NBA.odds(b.reference_decimal_odds)}</div>` : '';
    return `<tr class="${active ? '' : 'opacity-50'}">
      <td class="text-xs text-slate-500 whitespace-nowrap">${NBA.tpe(b.placed_at).slice(0, 9)}<div class="text-[10px]">#${b.id}</div></td>
      <td class="text-xs">${NBA.esc(b.away_name_zh || b.away_abbr)} @ ${NBA.esc(b.home_name_zh || b.home_abbr)}
        ${b.game_status === 'final' ? `<span class="text-slate-500 num ml-1">(${b.away_pts}-${b.home_pts})</span>` : ''}
      </td>
      <td class="text-xs">${NBA.esc(label(MARKETS, b.market))}</td>
      <td class="text-xs">${NBA.esc(label(SELECTIONS, b.selection))}</td>
      <td class="text-xs num">${b.line != null ? NBA.signed(b.line) : '—'}</td>
      <td class="text-xs num">${NBA.odds(b.odds)}${refOdds}</td>
      <td class="text-xs num">${Number(b.stake).toLocaleString()}</td>
      <td class="text-xs num ${pnl == null ? '' : pnl > 0 ? 'text-green-400' : pnl < 0 ? 'text-red-400' : ''}">${pnl == null ? '—' : NBA.signed(pnl, 0)}</td>
      <td>
        ${active ? `<select data-bet-id="${b.id}" class="bet-result bg-slate-800 border border-slate-700 rounded px-1.5 py-0.5 text-xs ${resultCls}">
          ${RESULTS.map(([v, t]) => `<option value="${v}" ${b.result === v ? 'selected' : ''}>${t}</option>`).join('')}
        </select>${b.settlement_source ? `<div class="text-[10px] text-slate-500">${NBA.esc(b.settlement_source)}</div>` : ''}`
          : `<span class="text-[10px] text-slate-400">${NBA.esc(b.record_status)}</span>`}
      </td>
      <td class="text-xs"><span class="grp-badge ${cCls}">${NBA.esc(cText)}</span>
        <div class="text-[10px] text-slate-500">${NBA.esc(b.origin || 'legacy_unlinked')}</div></td>
      <td class="text-xs text-slate-500 max-w-[10rem] truncate">${NBA.esc(b.note || b.void_reason || '')}</td>
      <td class="text-right whitespace-nowrap">
        ${active ? `<button data-fix="${b.id}" class="text-slate-500 hover:text-sky-300 text-xs mr-2" title="更正（建立新紀錄取代舊紀錄）"><i class="fas fa-pen"></i></button>
        <button data-void="${b.id}" class="text-slate-500 hover:text-red-400 text-xs" title="作廢（保留稽核紀錄）"><i class="fas fa-ban"></i></button>` : ''}
      </td>
    </tr>`;
  }

  async function loadBets() {
    root.innerHTML = `<div class="skeleton h-40"></div>`;
    try {
      const d = await NBA.get(`/api/bets${showInactive ? '?include_inactive=1' : ''}`);
      const bets = d.bets || [];
      const s = d.summary;
      const head = `
        <div class="flex flex-wrap gap-4 text-xs text-slate-400 mb-3">
          <span>共 <span class="text-slate-100 font-semibold num">${s.total}</span> 筆</span>
          <span>命中率 <span class="text-slate-100 font-semibold num">${NBA.pct(s.hit_rate)}</span></span>
          <span>損益 <span class="${s.pnl > 0 ? 'text-green-400' : s.pnl < 0 ? 'text-red-400' : 'text-slate-100'} font-semibold num">${NBA.signed(s.pnl, 0)}</span></span>
          <span>實際 yield <span class="${(s.roi ?? 0) > 0 ? 'text-green-400' : 'text-red-400'} font-semibold num">${s.roi != null ? NBA.signed(s.roi * 100, 2) + '%' : '—'}</span></span>
          <span class="text-amber-500/80">User actual betting record（不是策略績效）</span>
          <label class="ml-auto flex items-center gap-1"><input type="checkbox" id="show-inactive" ${showInactive ? 'checked' : ''}/>顯示已作廢 / 已更正</label>
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
          <table class="stat-table text-sm">
            <thead><tr><th>下注日</th><th>比賽</th><th>玩法</th><th>方向</th><th>盤線</th><th>實際賠率</th><th>金額</th><th>損益</th><th>結果</th><th>策略合規</th><th>備註</th><th></th></tr></thead>
            <tbody>${bets.map(betRow).join('')}</tbody>
          </table>
        </div>`;

      root.querySelectorAll('.bet-result').forEach((sel) => {
        sel.addEventListener('change', async () => {
          try {
            await NBA.patch(`/api/bets/${sel.dataset.betId}`, { result: sel.value });
            loadBets();
          } catch (e) { alert(e.data?.error || e.message); }
        });
      });
      root.querySelectorAll('[data-void]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const reason = prompt('作廢原因（紀錄不會刪除，會保留稽核軌跡）：');
          if (!reason) return;
          try {
            await NBA.post(`/api/bets/${btn.dataset.void}/void`, { reason });
            loadBets();
          } catch (e) { alert(e.data?.message || e.message); }
        });
      });
      root.querySelectorAll('[data-fix]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          const b = bets.find((x) => String(x.id) === btn.dataset.fix);
          const stake = prompt('更正後的實際金額：', b ? b.stake : '');
          if (stake === null) return;
          const odds = prompt('更正後的實際賠率：', b ? b.odds : '');
          if (odds === null) return;
          const reason = prompt('更正原因（必填；舊紀錄會標為 superseded，不會覆寫）：');
          if (!reason) return;
          try {
            await NBA.post(`/api/bets/${btn.dataset.fix}/correction`, { stake: Number(stake), odds: Number(odds), reason });
            loadBets();
          } catch (e) { alert(e.data?.message || e.message); }
        });
      });
      bindToggle();
    } catch (e) {
      if (e.status === 401) {
        root.innerHTML = `<div class="rounded-lg border border-slate-700 bg-slate-900/60 p-6 text-center">
          <i class="fas fa-lock text-2xl text-slate-600 mb-2"></i>
          <p class="text-sm text-slate-400 mb-3">投注紀錄為私人資料，請先登入</p>
          <a href="/login?next=/bets" class="inline-block px-3 py-1.5 rounded bg-orange-500 text-white text-sm">前往登入</a>
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
      renderForm([...(t1.games || []), ...(t2.games || [])]);
    } catch { renderForm([]); }
    loadBets();
  })();
})();
