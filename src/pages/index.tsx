/**
 * 前端頁面殼子 (規格書 §3.4)
 * ------------------------------------------------------------
 * 這些 JSX 僅輸出「結構」，實際資料一律由前端 JS 呼叫 /api/* 取得，
 * 前端程式碼裡沒有任何寫死的假資料（符合 §0 銜接原則）。
 */

import { Hono } from 'hono'
import type { AppBindings } from '../db'
import { renderer } from '../renderer'
import { getDb } from '../db'
import { getGameById } from '../db/queries'

const pages = new Hono<{ Bindings: AppBindings }>()
pages.use(renderer)

const CDN_CHART = 'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js'

/* ----------------------- 賽事總覽（首頁） ----------------------- */

pages.get('/', (c) => {
  return c.render(
    <>
      <section id="games-page">
        <div class="flex items-start justify-between gap-3 flex-wrap mb-4">
          <div>
            <h1 class="text-lg font-bold mb-1">
              <i class="fas fa-basketball text-orange-400 mr-1.5"></i>賽事總覽
            </h1>
            <p class="text-xs text-slate-400">
              台灣時間 <span id="date-label" class="text-slate-300 num"></span>
              　·　模型預測 vs 台彩盤口 vs 國際盤，並標示 Edge
            </p>
          </div>
          <div class="flex items-center gap-2">
            <div class="flex rounded-lg border border-slate-800 overflow-hidden text-xs" role="group" aria-label="日期">
              <button type="button" data-day-tab="0" aria-pressed="false" class="px-3 py-1.5 text-slate-400 hover:bg-slate-800">今日</button>
              <button type="button" data-day-tab="1" aria-pressed="true" class="px-3 py-1.5 bg-orange-500/15 text-orange-300 font-medium">明日</button>
              <button type="button" data-day-tab="2" aria-pressed="false" class="px-3 py-1.5 text-slate-400 hover:bg-slate-800">後天</button>
            </div>
            <input
              type="date"
              aria-label="選擇日期"
              id="date-picker"
              class="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-xs"
            />
          </div>
        </div>
        <div id="games-summary" class="mb-4"></div>
        <div id="games-list"></div>
      </section>
    </>,
    { title: '賽事總覽', nav: 'games', script: '/static/js/games.js' }
  )
})

/* ----------------------- D.5 今日決策中心 ----------------------- */

pages.get('/decision', (c) => {
  return c.render(
    <section id="decision-page">
      <div class="flex items-start justify-between gap-3 flex-wrap mb-3">
        <div>
          <h1 class="text-lg font-bold mb-1">
            <i class="fas fa-compass text-orange-400 mr-1.5" aria-hidden="true"></i><span id="decision-title">今日決策中心</span>
          </h1>
          <p class="text-xs text-slate-400">
            台彩盤口 × 模型 × risk-v1（已扣除你實際下注的額度）· 平台不會自動下注；在台彩下注後回來按「記錄下注」
          </p>
        </div>
        <div class="flex items-center gap-2">
          <div class="flex rounded-lg border border-slate-800 overflow-hidden text-xs" role="group" aria-label="Betting day">
            <button type="button" data-dday="0" aria-pressed="true" class="px-3 py-1.5 bg-orange-500/15 text-orange-300 font-medium">今日</button>
            <button type="button" data-dday="1" aria-pressed="false" class="px-3 py-1.5 text-slate-400 hover:bg-slate-800">明日</button>
          </div>
          <input type="date" id="decision-date" aria-label="選擇 betting day" class="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-xs" />
          <button type="button" id="decision-refresh" class="px-2 py-1.5 rounded border border-slate-700 text-xs text-slate-300 hover:bg-slate-800" title="重新整理" aria-label="重新整理">
            <i class="fas fa-rotate" aria-hidden="true"></i>
          </button>
        </div>
      </div>
      <section class="panel mb-4" aria-labelledby="twsport-sync-title">
        <div class="flex items-center justify-between flex-wrap gap-3">
          <div>
            <h2 id="twsport-sync-title" class="text-sm font-semibold">台彩盤口同步</h2>
            <p class="text-xs text-slate-400 mt-1">用已開啟的 Chrome 台彩頁面取得盤口，免匯出 HAR。</p>
          </div>
          <button type="button" id="twsport-sync-button" disabled class="px-3 py-2 rounded border border-slate-600 text-sm text-slate-100 hover:bg-slate-800 disabled:opacity-50 disabled:cursor-not-allowed">同步台彩盤口</button>
        </div>
        <p id="twsport-sync-status" role="status" aria-live="polite" class="text-xs text-slate-300 mt-3">正在確認同步功能…</p>
        <p class="text-xs text-slate-400 mt-2"><a href="https://www.sportslottery.com.tw/sportsbook" target="_blank" rel="noopener noreferrer" class="text-orange-300 underline">開啟台彩</a> · <a href="/static/twsport-sync-help.html" target="_blank" rel="noopener noreferrer" class="text-orange-300 underline">同步助手設定與限制</a></p>
      </section>
      <script src="/static/js/twsport-sync.js" defer></script>
      <div id="decision-banner"></div>
      <div id="decision-summary" class="mb-4"></div>
      <div class="grid gap-5 xl:grid-cols-[minmax(0,1fr)_20rem]">
        <div id="decision-games" class="min-w-0"></div>
        <aside class="space-y-4 min-w-0">
          <div id="decision-evidence"></div>
          <div id="decision-health"></div>
        </aside>
      </div>
      <div id="record-modal-root"></div>
      <div id="decision-dock" class="dock" hidden></div>
    </section>,
    { title: '今日決策中心', nav: 'decision', script: '/static/js/decision.js' }
  )
})

/* ------------------------- D.5 資金（bankroll） ------------------------- */

pages.get('/bankroll', (c) => {
  return c.render(
    <section id="bankroll-page">
      <h1 class="text-lg font-bold mb-1">
        <i class="fas fa-wallet text-orange-400 mr-1.5" aria-hidden="true"></i>策略資金
      </h1>
      <p class="text-xs text-slate-400 mb-4">
        手動維護的策略資金（不連結銀行或台彩帳戶）。每筆異動只能新增、不能修改或刪除；金額合計、未結算下注、當日起始資金都由 Python 計算。
      </p>
      <div id="bankroll-summary" class="mb-5"></div>
      <div class="grid gap-5 lg:grid-cols-2">
        <div id="bankroll-form"></div>
        <div id="bankroll-performance"></div>
      </div>
      <div id="bankroll-days" class="mt-5"></div>
      <div id="bankroll-ledger" class="mt-5"></div>
    </section>,
    { title: '資金', nav: 'bankroll', script: '/static/js/bankroll.js' }
  )
})

/* ------------------------- 單場詳情頁 ------------------------- */

pages.get('/games/:id', async (c) => {
  const id = Number(c.req.param('id'))
  if (!Number.isInteger(id)) return c.notFound()

  // 伺服器端先驗證比賽存在，避免顯示空殼頁
  const db = await getDb(c.env)
  const game = await getGameById(db, id)
  if (!game) {
    return c.render(
      <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-10 text-center">
        <i class="fas fa-circle-question text-3xl text-slate-400 mb-3"></i>
        <p class="text-sm text-slate-400 mb-4">找不到編號 {id} 的比賽</p>
        <a href="/" class="text-orange-400 text-sm hover:underline">返回賽事總覽</a>
      </div>,
      { title: '找不到比賽', nav: 'games' }
    )
  }

  return c.render(
    <>
      <script src={CDN_CHART}></script>
      <script dangerouslySetInnerHTML={{ __html: `window.__GAME_ID__=${id};` }}></script>
      <div id="game-detail"></div>
    </>,
    {
      title: `${game.away_abbr} @ ${game.home_abbr}`,
      nav: 'games',
      script: '/static/js/game-detail.js',
    }
  )
})

/* -------------------------- 傷病中心 -------------------------- */

pages.get('/injuries', (c) => {
  return c.render(
    <section id="injuries-page">
      <div class="flex items-start justify-between gap-3 flex-wrap mb-4">
        <div>
          <h1 class="text-lg font-bold mb-1">
            <i class="fas fa-kit-medical text-orange-400 mr-1.5"></i>傷病中心
          </h1>
          <p class="text-xs text-slate-400">
            各隊官方傷病申報與主力缺陣警示（NBA 官方於賽前一日當地 17:00 前申報，賽前會多次更新）
          </p>
        </div>
        <div class="flex items-center gap-2">
          <div class="flex rounded-lg border border-slate-800 overflow-hidden text-xs" role="group" aria-label="日期">
            <button type="button" data-inj-tab="0" aria-pressed="true" class="px-3 py-1.5 bg-orange-500/15 text-orange-300 font-medium">今日</button>
            <button type="button" data-inj-tab="1" aria-pressed="false" class="px-3 py-1.5 text-slate-400 hover:bg-slate-800">明日</button>
          </div>
          <input type="date" aria-label="選擇日期" id="inj-date" class="bg-slate-800 border border-slate-700 rounded px-2 py-1.5 text-xs" />
        </div>
      </div>
      <div id="injuries-summary" class="mb-4"></div>
      <div id="injuries-root"></div>
    </section>,
    { title: '傷病中心', nav: 'injuries', script: '/static/js/injuries.js' }
  )
})

/* ------------------------ 回測 / 績效頁 ------------------------ */

pages.get('/performance', (c) => {
  return c.render(
    <>
      <script src={CDN_CHART}></script>
      <section id="performance-page">
        <h1 class="text-lg font-bold mb-1">
          <i class="fas fa-chart-line text-orange-400 mr-1.5"></i>回測 / 績效
        </h1>
        <p class="text-xs text-slate-400 mb-5">
          本季實際預測、獨立影子實驗、歷史回測與你的實際投注損益。
          <span class="text-amber-300/90">實際投注損益以你記錄的台彩實際賠率計算；模型回測沒有歷史台彩盤口時，ROI 顯示「無證據」（不是 0，也不是獲利證明）。</span>
        </p>

        <div id="model-observability-root" class="mb-8" aria-live="polite"></div>

        <h2 class="text-sm font-semibold text-slate-300 mb-3">
          <i class="fas fa-robot mr-1.5 text-slate-400"></i>模型回測績效
        </h2>
        <div id="metrics-root" class="mb-8"></div>

        <h2 class="text-sm font-semibold text-slate-300 mb-3">
          <i class="fas fa-wallet mr-1.5 text-slate-400"></i>個人實際投注損益
        </h2>
        <div id="pnl-root" class="rounded-xl border border-slate-800 bg-slate-900/60 p-4"></div>
      </section>
    </>,
    { title: '回測 / 績效', nav: 'performance', script: '/static/js/performance.js' }
  )
})

/* -------------------------- 投注紀錄 -------------------------- */

pages.get('/bets', (c) => {
  return c.render(
    <section id="bets-page">
      <h1 class="text-lg font-bold mb-1">
        <i class="fas fa-receipt text-orange-400 mr-1.5"></i>個人投注紀錄
      </h1>
      <p class="text-xs text-slate-400 mb-5">
        僅供個人於台灣運彩投注之紀錄追蹤；賠率請填<strong class="text-slate-300">台彩實際賠率</strong>，
        以確保績效與 ROI 計算真實。
      </p>
      <div id="bet-form-wrap"></div>
      <div id="bets-root"></div>
    </section>,
    { title: '投注紀錄', nav: 'bets', script: '/static/js/bets.js' }
  )
})

/* -------------------------- 系統狀態 -------------------------- */

pages.get('/status', (c) => {
  return c.render(
    <section id="status-page">
      <h1 class="text-lg font-bold mb-1">
        <i class="fas fa-heart-pulse text-orange-400 mr-1.5"></i>系統狀態
      </h1>
      <p class="text-xs text-slate-400 mb-3">
        各資料來源最後更新時間與失敗告警（由階段二排程器寫入 <code>data_sources</code> 表）
      </p>
      <div id="status-overall" class="rounded-lg border border-slate-800 bg-slate-900/60 p-3 mb-5"></div>
      <div id="status-root"></div>
    </section>,
    { title: '系統狀態', nav: 'status', script: '/static/js/status.js' }
  )
})

/* --------------------------- 登入頁 --------------------------- */

pages.get('/login', (c) => {
  return c.render(
    <section id="login-page" class="max-w-sm mx-auto mt-8">
      <div class="rounded-xl border border-slate-800 bg-slate-900/60 p-6">
        <h1 class="text-base font-bold mb-1">
          <i class="fas fa-user-lock text-orange-400 mr-1.5"></i>個人帳號
        </h1>
        <p class="text-xs text-slate-400 mb-4">登入以存取投注紀錄與個人績效等私人資料</p>

        <div class="flex rounded-lg border border-slate-800 overflow-hidden text-xs mb-4">
          <button data-mode="login" class="flex-1 px-3 py-1.5 bg-slate-800 text-slate-100">登入</button>
          <button data-mode="register" class="flex-1 px-3 py-1.5 text-slate-400 hover:bg-slate-800/50">註冊</button>
        </div>

        <form id="auth-form" class="space-y-3">
          <label class="block">
            <span class="text-xs text-slate-400">Email</span>
            <input
              name="email"
              type="email"
              required
              autocomplete="email"
              class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2.5 py-2 text-sm"
            />
          </label>
          <label class="block">
            <span id="pw-label" class="text-xs text-slate-400">密碼</span>
            <input
              name="password"
              type="password"
              required
              autocomplete="current-password"
              class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2.5 py-2 text-sm"
            />
          </label>
          <label id="name-field" class="block hidden">
            <span class="text-xs text-slate-400">顯示名稱（選填）</span>
            <input
              name="display_name"
              type="text"
              class="w-full mt-1 bg-slate-800 border border-slate-700 rounded px-2.5 py-2 text-sm"
            />
          </label>
          <button
            id="auth-submit"
            type="submit"
            class="w-full px-3 py-2 rounded bg-orange-500 hover:bg-orange-400 text-white text-sm font-medium"
          >
            登入
          </button>
          <p id="auth-msg" class="text-xs min-h-4"></p>
        </form>
      </div>
    </section>,
    { title: '登入', nav: '', script: '/static/js/login.js' }
  )
})

export default pages
