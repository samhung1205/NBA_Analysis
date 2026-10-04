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
  teamName(t) {
    if (!t) return '—';
    return t.name_zh || t.name || t.abbr;
  },

  /* --------------------------- UI 元件 --------------------------- */
  /** edge_vs_fair → 顯示等級（純呈現；與 src/lib/pricing.ts edgeTier 相同門檻） */
  edgeTierOf(edge) {
    if (edge == null || edge <= 0.01) return 'none';
    if (edge < 0.03) return 'low';
    if (edge < 0.06) return 'mid';
    return 'high';
  },
  edgeBadge(tier, label) {
    const cls = { high: 'edge-high', mid: 'edge-mid', low: 'edge-low' }[tier] || 'edge-none';
    const icon = tier === 'high' ? 'fa-fire' : tier === 'mid' ? 'fa-arrow-trend-up' : tier === 'low' ? 'fa-circle-dot' : 'fa-minus';
    return `<span class="edge-badge ${cls}"><i class="fas ${icon}"></i>${this.esc(label)}</span>`;
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
    return `<span class="px-1.5 py-0.5 rounded text-[10px] font-medium ${cls}">${label}</span>`;
  },
  empty(msg, icon = 'fa-inbox') {
    return `<div class="text-center py-14 text-slate-500">
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
