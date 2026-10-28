/**
 * 共用前端工具
 * ------------------------------------------------------------
 * 重要原則（規格書 §0）：前端一律透過 API 讀資料庫，
 * 絕不在前端寫死假資料。所有畫面資料皆來自 /api/*。
 */

const NBA = {
  /* ------------------------------ API ------------------------------ */
  async get(path) {
    const res = await fetch(path, { headers: { Accept: 'application/json' } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { status: res.status, data });
    return data;
  },

  async send(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { status: res.status, data });
    return data;
  },
  post(path, body) { return this.send('POST', path, body); },
  patch(path, body) { return this.send('PATCH', path, body); },
  del(path) { return this.send('DELETE', path); },

  /* ---------------------------- 格式化 ---------------------------- */
  pct(v, digits = 1) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return (v * 100).toFixed(digits) + '%';
  },
  signed(v, digits = 1) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    const n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(digits);
  },
  fixed(v, digits = 1) {
    if (v === null || v === undefined || Number.isNaN(v)) return '—';
    return Number(v).toFixed(digits);
  },
  odds(v) {
    if (v === null || v === undefined) return '—';
    return Number(v).toFixed(2);
  },
  /** UTC ISO → 台灣時間顯示 */
  tpe(iso, withDate = true) {
    if (!iso) return '—';
    const d = new Date(iso);
    const t = new Date(d.getTime() + 8 * 3600 * 1000);
    const p = (n) => String(n).padStart(2, '0');
    const wk = ['日', '一', '二', '三', '四', '五', '六'][t.getUTCDay()];
    const time = `${p(t.getUTCHours())}:${p(t.getUTCMinutes())}`;
    return withDate ? `${p(t.getUTCMonth() + 1)}/${p(t.getUTCDate())}(${wk}) ${time}` : time;
  },
  tpeDateStr(offsetDays = 0) {
    const now = new Date(Date.now() + 8 * 3600 * 1000);
    now.setUTCDate(now.getUTCDate() + offsetDays);
    return now.toISOString().slice(0, 10);
  },
  ago(minutes) {
    if (minutes === null || minutes === undefined) return '從未更新';
    if (minutes < 1) return '剛剛';
    if (minutes < 60) return `${minutes} 分鐘前`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)} 小時前`;
    return `${Math.floor(minutes / 1440)} 天前`;
  },
  esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, (m) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));
  },
  /**
   * 球隊顯示名稱：全站統一為中文隊名（台灣運彩譯名，與 pipeline/core/odds/team_aliases.py 一致）。
   * 以縮寫為鍵，不依賴 teams.name_zh（DB 只有部分球隊有中文名）。t 可為 {abbr, name_zh, name} 或縮寫字串。
   */
  TEAM_ZH: {
    ATL: '亞特蘭大老鷹', BOS: '波士頓塞爾提克', BKN: '布魯克林籃網', CHA: '夏洛特黃蜂', CHI: '芝加哥公牛',
    CLE: '克里夫蘭騎士', DAL: '達拉斯獨行俠', DEN: '丹佛金塊', DET: '底特律活塞', GSW: '金州勇士',
    HOU: '休士頓火箭', IND: '印第安納溜馬', LAC: '洛杉磯快艇', LAL: '洛杉磯湖人', MEM: '曼斐斯灰熊',
    MIA: '邁阿密熱火', MIL: '密爾瓦基公鹿', MIN: '明尼蘇達灰狼', NOP: '紐奧良鵜鶘', NYK: '紐約尼克',
    OKC: '奧克拉荷馬雷霆', ORL: '奧蘭多魔術', PHI: '費城76人', PHX: '鳳凰城太陽', POR: '波特蘭拓荒者',
    SAC: '沙加緬度國王', SAS: '聖安東尼奧馬刺', TOR: '多倫多暴龍', UTA: '猶他爵士', WAS: '華盛頓巫師',
  },
  teamName(t) {
    if (!t) return '—';
    if (typeof t === 'string') return NBA.TEAM_ZH[t] || t;
    return NBA.TEAM_ZH[t.abbr] || t.name_zh || t.name || t.abbr || '—';
  },
  /** 對戰顯示：客隊 @ 主隊（中文隊名） */
  matchup(away, home) {
    return `${NBA.esc(NBA.teamName(away))} <span class="text-slate-500" aria-hidden="true">@</span> ${NBA.esc(NBA.teamName(home))}`;
  },

  /* --------------------------- UI 元件 --------------------------- */
  /** edge_vs_fair → 顯示等級（純呈現；與 src/lib/pricing.ts edgeTier 相同門檻） */
  edgeTierOf(edge) {
    if (edge == null || edge <= 0.01) return 'none';
    if (edge < 0.03) return 'low';
    if (edge < 0.06) return 'mid';
    return 'high';
  },
  /**
   * 模型差距 / 資格標籤：全站審慎低飽和——不用綠色或火焰等「鼓勵」訊號，強弱只用字重與邊框表達。
   * 能不能下、能下多少只由決策中心回答。
   */
  edgeBadge(tier, label) {
    const cls = tier === 'high' || tier === 'mid' ? 'edge-chip edge-chip-strong' : tier === 'low' ? 'edge-chip' : 'edge-chip edge-chip-none';
    return `<span class="${cls}">${this.esc(label)}</span>`;
  },
  statusBadge(status) {
    const map = {
      Out: 'status-out', Doubtful: 'status-doubtful', Questionable: 'status-questionable',
      Probable: 'status-probable', Available: 'status-available',
    };
    const zh = { Out: '確定缺陣', Doubtful: '很可能缺陣', Questionable: '出賽存疑', Probable: '很可能出賽', Available: '可出賽' };
    return `<span class="edge-badge ${map[status] || 'edge-none'}">${this.esc(zh[status] || status)}</span>`;
  },
  gameStatusBadge(status) {
    const map = {
      scheduled: ['未開賽', 'bg-slate-700 text-slate-300'],
      live: ['進行中', 'bg-red-500/20 text-red-400 animate-pulse'],
      final: ['已結束', 'bg-slate-800 text-slate-400'],
      postponed: ['延賽', 'bg-amber-500/20 text-amber-400'],
    };
    const [label, cls] = map[status] || [status, 'bg-slate-700 text-slate-300'];
    return `<span class="px-1.5 py-0.5 rounded text-xs font-medium whitespace-nowrap ${cls}">${label}</span>`;
  },
  empty(msg, icon = 'fa-inbox') {
    return `<div class="text-center py-14 text-slate-400">
      <i class="fas ${icon} text-4xl mb-3 opacity-40"></i>
      <p class="text-sm">${this.esc(msg)}</p>
    </div>`;
  },
  error(msg) {
    return `<div class="rounded-lg border border-red-900/60 bg-red-950/30 p-4 text-sm text-red-300">
      <i class="fas fa-circle-exclamation mr-1"></i>${this.esc(msg)}
    </div>`;
  },
  skeletonCards(n = 3) {
    return Array.from({ length: n }, () => '<div class="skeleton h-40"></div>').join('');
  },

  /* ---------------------- 狀態 / 原因字典（zh-TW） ---------------------- */
  /** 金額：固定 zh-TW 千分位（不隨裝置語系變動） */
  money(v, cur) {
    if (v === null || v === undefined || v === '' || Number.isNaN(Number(v))) return '—';
    return `${Number(v).toLocaleString('zh-TW', { maximumFractionDigits: 2 })}${cur ? ' ' + this.esc(cur) : ''}`;
  },
  COMPLIANCE_TEXT: {
    compliant: '符合策略', user_override: '超出額度（已確認）', missing_context: '缺少風險脈絡（額度無法驗證）',
    outside_model: '模型外（非可操作機會）', manual_unlinked: '手動記錄（未連結機會）', legacy: '舊版紀錄',
  },
  ORIGIN_TEXT: {
    platform_opportunity: '決策中心機會', paper_decision: '前瞻驗證機會', manual_unlinked: '手動記錄', legacy_unlinked: '舊版紀錄',
  },
  DECISION_STATUS_TEXT: {
    qualified: '可操作', already_recorded: '已記錄過', no_positive_ev: '無正 EV', stale_odds: '盤口過舊',
    no_prediction: '尚無可用預測', no_odds: '沒有台彩盤口', unsupported: '不支援的玩法', risk_cap_reached: '額度已用完',
    actual_exposure_over_limit: '實際下注已超過上限', bankroll_unavailable: '尚未設定策略資金', data_incomplete: '資料不完整',
  },
  /** blockers / reasons 代碼 → 中文（未知代碼原樣顯示，避免吞掉資訊） */
  reasonText(code) {
    const c = String(code ?? '');
    const [head, tail] = c.split(':');
    const fixed = {
      decision_board_not_materialized: '決策檢視尚未產生（排程每分鐘物化）', migration_0008_not_applied: '決策中心資料表尚未建立',
      no_prediction: '尚無可用預測', no_taiwan_odds: '沒有台彩盤口', no_positive_ev: '無正 EV', stale_odds: '盤口過舊',
      no_remaining_game_capacity: '同場額度已用完', no_remaining_daily_capacity: '當日額度已用完', risk_cap_reached: '額度已用完',
      game_started: '比賽已開始', market_closed: '盤口已關閉', bankroll_not_configured: '尚未設定策略資金',
      game_capacity_scaled_with_actual_exposure: '同場額度已依實際下注縮小', daily_capacity_scaled_with_actual_exposure: '當日額度已依實際下注縮小',
      market_overround: '盤口抽水異常', seed_fixture: '測試資料',
    };
    if (head === 'taiwan_odds') return `台彩盤口：${({ stale: '過舊', market_closed: '已關閉', not_yet_published: '尚未開盤', ingestion_unavailable: '自動擷取不可用', not_configured: '尚未設定', outside_window: '超出定價視窗', game_started: '比賽已開始' })[tail] || tail}`;
    return fixed[c] || this.DECISION_STATUS_TEXT[c] || c;
  },
  /** 伺服器風險檢查代碼 → 可核對的中文（含 Python 物化上限數字；不在前端計算） */
  checkText(code, ctx = {}) {
    const L = ctx.limits || {};
    const cur = ctx.currency || '';
    const you = ctx.stake != null && ctx.stake !== '' ? `你輸入 ${this.money(ctx.stake, cur)}；` : '';
    const [head, tail] = String(code).split(':');
    const map = {
      bankroll_not_configured: '尚未設定策略資金，無法驗證額度',
      decision_board_not_materialized: '這個 betting day 的決策檢視尚未產生，無法驗證額度',
      risk_recalculation_pending: '風險額度正在重新計算，暫時無法驗證',
      day_start_unavailable: '當日起始資金（day-start）尚未凍結，無法驗證額度',
      exceeds_risk_v1_single_bet_limit: `${you}超過單筆上限 ${this.money(L.max_bet_amount, cur)}`,
      exceeds_risk_v1_daily_remaining: `${you}超過當日剩餘額度 ${this.money(L.remaining_day_amount, cur)}`,
      exceeds_risk_v1_game_remaining: `${you}超過同場剩餘額度 ${this.money(L.remaining_game_amount, cur)}`,
      game_not_in_decision_board: '這場比賽不在目前的決策板',
      game_capacity_unavailable: '這場比賽的同場額度無法取得',
      opportunity_not_in_current_board: '這個機會已不在目前的決策板（可能已重新物化）',
      already_recorded_no_top_up: '這個機會已記錄過下注；decision-v1 不補單',
      above_decision_v1_proposed_capacity: `${you}高於建議新增額度 ${this.money(L.max_additional_stake_amount, cur)}`,
      placed_after_tipoff: '下注時間在開賽之後',
      correction_not_revalidated: '更正後的金額 / 賠率尚未重新驗證額度',
      replacement_capacity_not_materialized: '更正後的額度尚未由 Python 重新計算',
      bets_or_bankroll_changed: '下注或資金剛有變動，額度需要重新計算',
      ledger_changed: '資金異動尚未納入計算',
      bankroll_account_changed: '資金帳戶剛有變動',
      bet_records_changed: '下注紀錄剛有變動',
      materialization_stale: '決策檢視超過 10 分鐘未更新',
    };
    if (head === 'opportunity_not_qualified') return `這個機會目前不是可操作狀態（${this.DECISION_STATUS_TEXT[tail] || tail}）`;
    return map[code] || code;
  },
  /** 409 confirmation_required → 標題 + 條列（取代英文 message 與 snake_case 代碼） */
  confirmationHtml(data, ctx = {}) {
    const comp = data.compliance_if_confirmed;
    const title = comp === 'user_override' ? '超出建議額度：需要你確認並填寫原因'
      : comp === 'missing_context' ? '目前無法驗證風險額度'
        : comp === 'outside_model' ? '這不是目前可操作的機會' : '需要你確認';
    const items = [...(data.checks || []), ...(data.warnings || [])].map((c) => `<li>${this.esc(this.checkText(c, { ...ctx, limits: data.limits }))}</li>`).join('');
    return `<p class="font-semibold mb-1"><i class="fas fa-triangle-exclamation mr-1" aria-hidden="true"></i>${title}</p>
      ${items ? `<ul class="list-disc ml-4 mb-1">${items}</ul>` : ''}
      <p class="mb-1">確認後記錄為「${this.esc(this.COMPLIANCE_TEXT[comp] || comp)}」；平台不會修改你的金額，也不視為策略合規下注。</p>`;
  },

  /* ---------------------------- 對話框 ---------------------------- */
  /**
   * 共用對話框：焦點移入、Esc 關閉、背景 inert、關閉後焦點回原處。
   * onConfirm(form) 丟出 Error 時顯示訊息並保留輸入；回傳 false 表示不關閉。
   */
  dialog({ title, body = '', confirmText = '確認', cancelText = '取消', tone = 'primary', onConfirm, onCancel } = {}) {
    const prev = document.activeElement;
    const root = document.createElement('div');
    root.className = 'modal-backdrop';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    const tid = 'dlg-' + Math.random().toString(36).slice(2, 8);
    root.setAttribute('aria-labelledby', tid);
    root.innerHTML = `<form class="modal-card" novalidate>
        <h2 id="${tid}" class="text-sm font-semibold mb-2">${title}</h2>
        <div class="dlg-body text-xs text-slate-300">${body}</div>
        <div class="modal-actions">
          <span class="dlg-msg text-xs mr-auto" role="status"></span>
          <button type="button" class="rec-secondary" data-cancel>${cancelText}</button>
          <button type="submit" class="${tone === 'danger' ? 'rec-danger' : 'rec-primary'}">${confirmText}</button>
        </div></form>`;
    const siblings = [...document.body.children];
    document.body.appendChild(root);
    siblings.forEach((el) => el.tagName !== 'SCRIPT' && el.toggleAttribute('inert', true));
    document.body.classList.add('modal-open');
    const form = root.querySelector('form');
    const msg = root.querySelector('.dlg-msg');
    const close = () => {
      root.remove();
      siblings.forEach((el) => el.removeAttribute('inert'));
      document.body.classList.remove('modal-open');
      if (prev && prev.isConnected) prev.focus();
    };
    const cancel = () => { close(); onCancel && onCancel(); };
    root.querySelector('[data-cancel]').onclick = cancel;
    root.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); cancel(); } });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const btn = form.querySelector('[type=submit]');
      btn.disabled = true;
      msg.className = 'dlg-msg text-xs mr-auto text-slate-400';
      msg.textContent = '處理中…';
      try {
        const keep = onConfirm ? await onConfirm(form, { msg, root }) : true;
        if (keep === false) { btn.disabled = false; return; }
        close();
      } catch (e) {
        btn.disabled = false;
        msg.className = 'dlg-msg text-xs mr-auto text-red-400';
        msg.textContent = e.message || String(e);
      }
    });
    (form.querySelector('input, select, textarea') || form.querySelector('[type=submit]')).focus();
    return { root, form, close };
  },

  /**
   * 登入後導向目標：只接受站內相對路徑（/path?query#hash）。
   * 擋掉外部網址、協定相對（//host）、反斜線（瀏覽器會當成 /）、javascript: 等協定與控制字元；不合格一律回首頁。
   */
  safeNext(raw, origin = location.origin) {
    if (typeof raw !== 'string' || !raw.startsWith('/') || raw.startsWith('//')) return '/';
    if (/[\\\u0000-\u001f\u007f]/.test(raw)) return '/';
    try {
      const u = new URL(raw, origin);
      return u.origin === origin ? u.pathname + u.search + u.hash : '/';
    } catch { return '/'; }
  },

  /* ---------------------------- 驗證 ---------------------------- */
  async currentUser() {
    try {
      const d = await this.get('/api/auth/me');
      return d.authenticated ? d.user : null;
    } catch { return null; }
  },

  async renderAuthBox() {
    const box = document.getElementById('auth-box');
    if (!box) return null;
    const user = await this.currentUser();
    if (user) {
      box.innerHTML = `
        <div class="flex items-center gap-2">
          <span class="hidden lg:inline text-slate-400 text-xs">${this.esc(user.email)}</span>
          <button id="logout-btn" class="px-2 py-1 rounded border border-slate-700 hover:bg-slate-800 text-xs">登出</button>
        </div>`;
      document.getElementById('logout-btn').onclick = async () => {
        await this.post('/api/auth/logout');
        location.href = '/login';
      };
    } else {
      box.innerHTML = `<a href="/login" class="px-2.5 py-1 rounded bg-orange-500/90 hover:bg-orange-500 text-white text-xs font-medium">登入</a>`;
    }
    return user;
  },
};

document.addEventListener('DOMContentLoaded', () => NBA.renderAuthBox());
window.NBA = NBA;
