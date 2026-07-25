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
  ];
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
      <h2 class="text-sm font-semibold text-slate-300 mb-3"><i class="fas fa-plus mr-1.5 text-slate-500"></i>新增投注紀錄</h2>
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
      };
      try {
        await NBA.post('/api/bets', payload);
        msg.className = 'text-xs text-green-400';
        msg.textContent = '已新增';
        ev.target.reset();
        loadBets();
      } catch (e) {
        msg.className = 'text-xs text-red-400';
        msg.textContent = e.message;
      }
    });
  }

  function betRow(b) {
    const resultCls = { win: 'text-green-400', lose: 'text-red-400', pending: 'text-slate-400' }[b.result] || 'text-slate-400';
    const pnl = b.result === 'pending' ? null : (b.payout ?? 0) - b.stake;
    return `<tr>
      <td class="text-xs text-slate-500 whitespace-nowrap">${NBA.tpe(b.placed_at).slice(0, 9)}</td>
      <td class="text-xs">${NBA.esc(b.away_name_zh || b.away_abbr)} @ ${NBA.esc(b.home_name_zh || b.home_abbr)}
        ${b.game_status === 'final' ? `<span class="text-slate-500 num ml-1">(${b.away_pts}-${b.home_pts})</span>` : ''}
      </td>
      <td class="text-xs">${NBA.esc(label(MARKETS, b.market))}</td>
      <td class="text-xs">${NBA.esc(label(SELECTIONS, b.selection))}</td>
      <td class="text-xs num">${b.line != null ? NBA.signed(b.line) : '—'}</td>
      <td class="text-xs num">${NBA.odds(b.odds)}</td>
      <td class="text-xs num">${Number(b.stake).toLocaleString()}</td>
      <td class="text-xs num ${pnl == null ? '' : pnl > 0 ? 'text-green-400' : pnl < 0 ? 'text-red-400' : ''}">${pnl == null ? '—' : NBA.signed(pnl, 0)}</td>
      <td>
        <select data-bet-id="${b.id}" class="bet-result bg-slate-800 border border-slate-700 rounded px-1.5 py-0.5 text-xs ${resultCls}">
          ${RESULTS.map(([v, t]) => `<option value="${v}" ${b.result === v ? 'selected' : ''}>${t}</option>`).join('')}
        </select>
      </td>
      <td class="text-xs text-slate-500 max-w-[10rem] truncate">${NBA.esc(b.note || '')}</td>
      <td class="text-right">
        <button data-del="${b.id}" class="text-slate-500 hover:text-red-400 text-xs"><i class="fas fa-trash"></i></button>
      </td>
    </tr>`;
  }

  async function loadBets() {
    root.innerHTML = `<div class="skeleton h-40"></div>`;
    try {
      const d = await NBA.get('/api/bets');
      const bets = d.bets || [];
      const s = d.summary;
      const head = `
        <div class="flex flex-wrap gap-4 text-xs text-slate-400 mb-3">
          <span>共 <span class="text-slate-100 font-semibold num">${s.total}</span> 筆</span>
          <span>命中率 <span class="text-slate-100 font-semibold num">${NBA.pct(s.hit_rate)}</span></span>
          <span>損益 <span class="${s.pnl > 0 ? 'text-green-400' : s.pnl < 0 ? 'text-red-400' : 'text-slate-100'} font-semibold num">${NBA.signed(s.pnl, 0)}</span></span>
          <span>ROI <span class="${(s.roi ?? 0) > 0 ? 'text-green-400' : 'text-red-400'} font-semibold num">${s.roi != null ? NBA.signed(s.roi * 100, 2) + '%' : '—'}</span></span>
        </div>`;

      if (!bets.length) {
        root.innerHTML = head + NBA.empty('尚無投注紀錄，使用上方表單新增。', 'fa-receipt');
        return;
      }
      root.innerHTML =
        head +
        `<div class="rounded-xl border border-slate-800 bg-slate-900/60 overflow-x-auto">
          <table class="stat-table text-sm">
            <thead><tr><th>下注日</th><th>比賽</th><th>玩法</th><th>方向</th><th>盤線</th><th>賠率</th><th>金額</th><th>損益</th><th>結果</th><th>備註</th><th></th></tr></thead>
            <tbody>${bets.map(betRow).join('')}</tbody>
          </table>
        </div>`;

      root.querySelectorAll('.bet-result').forEach((sel) => {
        sel.addEventListener('change', async () => {
          try {
            await NBA.patch(`/api/bets/${sel.dataset.betId}`, { result: sel.value });
            loadBets();
          } catch (e) { alert(e.message); }
        });
      });
      root.querySelectorAll('[data-del]').forEach((btn) => {
        btn.addEventListener('click', async () => {
          if (!confirm('確定刪除這筆紀錄？')) return;
          try {
            await NBA.del(`/api/bets/${btn.dataset.del}`);
            loadBets();
          } catch (e) { alert(e.message); }
        });
      });
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
