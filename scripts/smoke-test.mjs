#!/usr/bin/env node
/**
 * 階段一驗收測試 (規格書 §3.5)
 * ------------------------------------------------------------
 * 驗收標準：所有頁面能跑、所有 API 能讀寫資料庫、seed 測試資料後畫面正確顯示。
 *
 * 用法：BASE=http://localhost:3000 node scripts/smoke-test.mjs
 */

const BASE = process.env.BASE || 'http://localhost:3000'
let pass = 0
let fail = 0
const failures = []

function ok(name, cond, detail = '') {
  if (cond) {
    pass++
    console.log(`  ✓ ${name}`)
  } else {
    fail++
    failures.push(`${name}${detail ? ` — ${detail}` : ''}`)
    console.log(`  ✗ ${name}${detail ? ` — ${detail}` : ''}`)
  }
}

function section(title) {
  console.log(`\n▌${title}`)
}

let cookie = ''

async function req(path, init = {}) {
  const res = await fetch(BASE + path, {
    ...init,
    headers: { ...(init.headers || {}), ...(cookie ? { Cookie: cookie } : {}) },
    redirect: 'manual',
  })
  const setCookie = res.headers.get('set-cookie')
  if (setCookie) cookie = setCookie.split(';')[0]
  const ct = res.headers.get('content-type') || ''
  const body = ct.includes('json') ? await res.json().catch(() => null) : await res.text()
  return { status: res.status, body, ct }
}

/* ============================ 頁面 ============================ */
section('前端頁面 (§3.4)')
for (const [path, needle, label] of [
  ['/', 'games-list', '賽事總覽'],
  ['/injuries', 'injuries-root', '傷病中心'],
  ['/performance', 'metrics-root', '回測 / 績效'],
  ['/bets', 'bets-root', '投注紀錄'],
  ['/status', 'status-root', '系統狀態'],
  ['/login', 'auth-form', '登入頁'],
]) {
  const r = await req(path)
  ok(`${label} ${path} 回應 200 且含掛載點`, r.status === 200 && String(r.body).includes(needle), `status=${r.status}`)
  ok(`${label} 含免責聲明`, String(r.body).includes('不保證獲利'))
}

/* ============================ API ============================ */
section('API 路由 (§3.3)')

// 5. GET /api/games/tomorrow
const tomorrow = await req('/api/games/tomorrow')
ok('GET /api/games/tomorrow 200', tomorrow.status === 200)
ok('  回傳台灣日期欄位', !!tomorrow.body?.tpe_date)
ok('  有 seed 賽事資料', Array.isArray(tomorrow.body?.games) && tomorrow.body.games.length > 0,
  `games=${tomorrow.body?.games?.length}`)
const g0 = tomorrow.body?.games?.[0]
ok('  賽事含球隊中文名', !!g0?.home?.name_zh && !!g0?.away?.name_zh)
ok('  賽事含台灣時間顯示字串', !!g0?.date_tpe_display)
ok('  賽事含模型預測(勝率/分差/總分)',
  g0?.prediction?.home_win_prob != null && g0?.prediction?.pred_margin != null && g0?.prediction?.pred_total != null)
ok('  賽事含上半場預測', g0?.prediction?.half1?.home != null && g0?.prediction?.half1?.total != null)
ok('  賽事含台彩盤口', !!g0?.odds?.twsport?.ml && !!g0?.odds?.twsport?.spread && !!g0?.odds?.twsport?.total)
ok('  賽事含國際盤對照', !!g0?.odds?.international?.ml)
ok('  賽事含 edge 分析', Array.isArray(g0?.odds?.edges) && g0.odds.edges.length > 0)
const mlEdge = g0?.odds?.edges?.find((e) => e.market === 'ml')
ok('  ML edge 含模型機率/市場公允機率/抽水（D.2：Kelly 已停用為 null）',
  mlEdge?.model_prob != null && mlEdge?.market_fair_prob != null && mlEdge?.vig != null && mlEdge?.kelly_quarter === null)
ok('  台彩抽水 > 0（返還率低於 100%）', (mlEdge?.vig ?? 0) > 0, `vig=${mlEdge?.vig}`)

// D.2：定價（Python pricing engine 寫入；API 只讀）
const pr = g0?.odds?.pricing
ok('  odds.pricing 存在且已定價（pricing-v1 / proportional-v1）',
  pr?.pricing_version === 'pricing-v1' && pr?.no_vig_method === 'proportional-v1' && pr?.status === 'priced' && pr.markets.length > 0,
  `status=${pr?.status}`)
const twMl = pr?.markets?.find((m) => m.source === 'twsport' && m.market === 'ml')
const fields = ['decimal_odds', 'raw_implied_prob', 'fair_no_vig_prob', 'model_prob', 'push_prob', 'edge_vs_fair', 'ev_per_unit']
ok('  每個 outcome 同時有 賠率/原始隱含/去水公允/模型/push/edge/EV',
  !!twMl && twMl.outcomes.length === 2 && twMl.outcomes.every((o) => fields.every((f) => o[f] != null)))
ok('  去水公允機率總和 = 1、抽水 = Σ原始隱含 − 1',
  !!twMl && Math.abs(twMl.outcomes.reduce((a, o) => a + o.fair_no_vig_prob, 0) - 1) < 1e-9 &&
  Math.abs(twMl.market_overround - (twMl.outcomes.reduce((a, o) => a + o.raw_implied_prob, 0) - 1)) < 1e-9)
ok('  edge_vs_fair = 模型 − 去水公允（與 EV 不同欄位）',
  !!twMl && twMl.outcomes.every((o) => Math.abs(o.edge_vs_fair - (o.model_prob - o.fair_no_vig_prob)) < 1e-9 && o.edge_vs_fair !== o.ev_per_unit))
ok('  國際盤各 bookmaker 獨立定價（不平均）',
  pr?.markets?.some((m) => m.source === 'oddsapi') && pr.markets.every((m) => m.outcomes.length >= 2))
const spEdge = g0?.odds?.edges?.find((e) => e.market === 'spread')
ok('  讓分 edge 以機率計算（不再用 line_gap）', spEdge?.edge != null && spEdge?.line_gap === null && spEdge?.deprecated === true)

// today / 任意日期
const today = await req('/api/games/today')
ok('GET /api/games/today 200 且有進行中/已結束賽事', today.status === 200 && today.body.games.length > 0)
ok('  含 live 或 final 狀態賽事', today.body.games.some((g) => ['live', 'final'].includes(g.status)))
const finalGame = today.body.games.find((g) => g.status === 'final')
ok('  已結束賽事含逐節與半場比分',
  finalGame?.score?.quarters?.home?.[0] != null && finalGame?.score?.home_h1 != null && finalGame?.score?.home_h2 != null)

const byDate = await req('/api/games?date=2020-01-01')
ok('GET /api/games?date=... 200（無資料日回空陣列）', byDate.status === 200 && byDate.body.games.length === 0)
const badDate = await req('/api/games?date=abc')
ok('GET /api/games?date=abc 回 400', badDate.status === 400)

// 6. GET /api/games/:id
const detail = await req(`/api/games/${g0.id}`)
ok(`GET /api/games/${g0.id} 200`, detail.status === 200)
ok('  含 head_to_head 歷史對戰', Array.isArray(detail.body?.head_to_head))
ok('  含雙方近況 recent_form', Array.isArray(detail.body?.recent_form?.home) && Array.isArray(detail.body?.recent_form?.away))
ok('  含特徵拆解 features', !!detail.body?.prediction?.features)
ok('  features 含 contributions（為什麼這樣預測）',
  Array.isArray(detail.body?.prediction?.features?.contributions) && detail.body.prediction.features.contributions.length > 0)
ok('  含 odds_history（供折線圖）', Array.isArray(detail.body?.odds_history) && detail.body.odds_history.length > 0)
ok('  含本場傷病', Array.isArray(detail.body?.injuries) && detail.body.injuries.length > 0)
const missing = await req('/api/games/999999')
ok('GET /api/games/999999 回 404', missing.status === 404)

// box score 驗證（用已結束比賽）
const finalDetail = await req(`/api/games/${finalGame.id}`)
ok('  已結束賽事含 team_game_stats', finalDetail.body?.team_game_stats?.length === 2)
ok('  team_game_stats 含進階數據 (pace/off_rtg/def_rtg)',
  finalDetail.body?.team_game_stats?.[0]?.pace != null && finalDetail.body.team_game_stats[0].off_rtg != null)
ok('  已結束賽事含 player_game_stats', (finalDetail.body?.player_game_stats?.length ?? 0) > 0)

// 7. GET /api/injuries/today
const inj = await req('/api/injuries/today')
ok('GET /api/injuries/today 200', inj.status === 200)
ok('  含 total_reports 與依球隊分組', inj.body?.total_reports > 0 && Array.isArray(inj.body?.teams))
const alertTeam = inj.body?.teams?.find((t) => t.key_players_out > 0)
ok('  有主力缺陣警示 (key_players_out)', !!alertTeam, `teams=${inj.body?.teams?.length}`)
ok('  球員層級含 key_absence_alert 旗標',
  inj.body?.teams?.some((t) => t.players.some((p) => p.key_absence_alert === true)))
ok('  依主力缺陣數排序（警示優先）',
  inj.body?.teams?.length < 2 || inj.body.teams[0].key_players_out >= inj.body.teams[inj.body.teams.length - 1].key_players_out)

// 8. GET /api/predictions/:gameId
const pred = await req(`/api/predictions/${g0.id}`)
ok(`GET /api/predictions/${g0.id} 200`, pred.status === 200)
ok('  含 model_version 與 confidence',
  !!pred.body?.prediction?.model_version && pred.body.prediction.confidence != null)
ok('  含上/下半場預測',
  pred.body?.prediction?.half1?.home != null && pred.body?.prediction?.half2?.home != null)

// 9. GET /api/odds/:gameId
const odds = await req(`/api/odds/${g0.id}`)
ok(`GET /api/odds/${g0.id} 200`, odds.status === 200)
ok('  含 snapshots 歷史', Array.isArray(odds.body?.snapshots) && odds.body.snapshots.length > 1)
ok('  含 series 時間序列（供 Chart.js）', Array.isArray(odds.body?.series) && odds.body.series.length > 0)
const spreadSeries = odds.body?.series?.find((s) => s.source === 'twsport' && s.market === 'spread')
ok('  台彩讓分盤有多個時間點（可畫盤口變動）', (spreadSeries?.points?.length ?? 0) >= 3,
  `points=${spreadSeries?.points?.length}`)

// 11. GET /api/system/status
const status = await req('/api/system/status')
ok('GET /api/system/status 200', status.status === 200)
ok('  含各資料來源與 overall', Array.isArray(status.body?.sources) && status.body.sources.length > 0 && !!status.body?.overall)
ok('  含 db_driver（確認資料庫驅動）', !!status.body?.db_driver)
ok('  含資料新鮮度 age_minutes', status.body?.sources?.[0]?.age_minutes != null)
ok('  台彩來源存在 (twsport)', status.body?.sources?.some((s) => s.source_key === 'twsport'))
ok('  有 warn 狀態來源（告警機制可顯示）', status.body?.sources?.some((s) => s.health === 'warn'))

// /api/metrics
const metrics = await req('/api/metrics')
ok('GET /api/metrics 200 且有回測資料', metrics.status === 200 && metrics.body?.metrics?.length > 0)
ok('  含 accuracy / ats_accuracy / sim_roi',
  metrics.body?.metrics?.[0]?.accuracy != null && metrics.body.metrics[0].ats_accuracy != null && metrics.body.metrics[0].sim_roi != null)

/* ========================= 驗證 / 授權 ========================= */
section('驗證機制 (§3.2) 與私人資料保護')

const anonBets = await req('/api/bets')
ok('未登入 GET /api/bets 回 401', anonBets.status === 401)
const anonPost = await req('/api/bets', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ game_id: g0.id, market: 'ml', selection: 'home', odds: 1.9, stake: 100 }),
})
ok('未登入 POST /api/bets 回 401', anonPost.status === 401)

const me0 = await req('/api/auth/me')
ok('GET /api/auth/me 未登入回 authenticated:false', me0.body?.authenticated === false)

// 用 seed 測試帳號登入
const badLogin = await req('/api/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: 'demo@example.com', password: 'wrongpassword' }),
})
ok('錯誤密碼登入回 401', badLogin.status === 401)

const login = await req('/api/auth/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: 'demo@example.com', password: 'nba12345678' }),
})
ok('seed 測試帳號登入成功 (demo@example.com)', login.status === 200 && login.body?.ok === true,
  JSON.stringify(login.body))

const me1 = await req('/api/auth/me')
ok('登入後 /api/auth/me 回 authenticated:true', me1.body?.authenticated === true)

// 註冊流程
const rndEmail = `test${Date.now()}@example.com`
const reg = await req('/api/auth/register', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: rndEmail, password: 'testpass1234' }),
})
ok('註冊新帳號 201', reg.status === 201)
const regDup = await req('/api/auth/register', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: rndEmail, password: 'testpass1234' }),
})
ok('重複 email 註冊回 409', regDup.status === 409)
const regWeak = await req('/api/auth/register', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: `w${Date.now()}@example.com`, password: 'short' }),
})
ok('密碼過短回 400', regWeak.status === 400)

/* ======================== bets CRUD ======================== */
section('個人下單紀錄 CRUD (§3.3-10) — 寫入資料庫驗證')

// 目前 cookie 為剛註冊的新帳號
const created = await req('/api/bets', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    game_id: g0.id, market: 'spread', selection: 'home',
    line: -1.5, odds: 1.87, stake: 1000, note: 'smoke test',
  }),
})
ok('POST /api/bets 201 並回傳 id', created.status === 201 && created.body?.id != null,
  JSON.stringify(created.body))
const betId = created.body?.id

const invalidBet = await req('/api/bets', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ game_id: g0.id, market: 'invalid', selection: 'home', odds: 1.9, stake: 100 }),
})
ok('POST /api/bets 無效 market 回 400', invalidBet.status === 400)
const badOdds = await req('/api/bets', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ game_id: g0.id, market: 'ml', selection: 'home', odds: 0.5, stake: 100 }),
})
ok('POST /api/bets 賠率 <= 1 回 400', badOdds.status === 400)

const listed = await req('/api/bets')
ok('GET /api/bets 200 讀回剛寫入資料', listed.status === 200 && listed.body?.bets?.some((b) => b.id === betId))
ok('  含 summary 統計', listed.body?.summary?.total >= 1)
ok('  含 pnl_curve', Array.isArray(listed.body?.pnl_curve))
ok('  bets 含比賽資訊 join', !!listed.body?.bets?.[0]?.home_abbr)

const settled = await req(`/api/bets/${betId}`, {
  method: 'PATCH',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ result: 'win' }),
})
ok('PATCH /api/bets/:id 結算為 win', settled.status === 200 && settled.body?.result === 'win')
ok('  payout = stake × 台彩賠率 (1000 × 1.87)', Math.abs((settled.body?.payout ?? 0) - 1870) < 0.01,
  `payout=${settled.body?.payout}`)

const afterSettle = await req('/api/bets')
ok('結算後 summary 更新（win=1, ROI 以台彩賠率計算）',
  afterSettle.body?.summary?.win === 1 && afterSettle.body?.summary?.roi != null,
  JSON.stringify(afterSettle.body?.summary))
ok('  ROI = 87%（1870/1000 - 1）', Math.abs((afterSettle.body?.summary?.roi ?? 0) - 0.87) < 0.01,
  `roi=${afterSettle.body?.summary?.roi}`)
ok('  pnl_curve 有資料點', afterSettle.body?.pnl_curve?.length >= 1)

const deleted = await req(`/api/bets/${betId}`, { method: 'DELETE' })
ok('DELETE /api/bets/:id 200', deleted.status === 200)
const afterDel = await req('/api/bets')
ok('  刪除後不再出現於清單', !afterDel.body?.bets?.some((b) => b.id === betId))

// 登出
const logout = await req('/api/auth/logout', { method: 'POST' })
ok('POST /api/auth/logout 200', logout.status === 200)
cookie = ''
const afterLogout = await req('/api/bets')
ok('登出後 /api/bets 回 401', afterLogout.status === 401)

/* ========================== 其他 ========================== */
section('其他')
const health = await req('/healthz')
ok('GET /healthz 200', health.status === 200 && health.body?.ok === true)
const nf = await req('/api/nonexistent')
ok('未知 API 路徑回 404 JSON', nf.status === 404 && nf.body?.error === 'not_found')
const nfPage = await req('/nonexistent-page')
ok('未知頁面回 404 HTML', nfPage.status === 404)
const css = await req('/static/style.css')
ok('靜態檔 /static/style.css 可存取', css.status === 200)
const js = await req('/static/js/common.js')
ok('靜態檔 /static/js/common.js 可存取', js.status === 200)

/* ========================== 總結 ========================== */
console.log('\n' + '='.repeat(52))
console.log(`結果：${pass} 通過 / ${fail} 失敗（共 ${pass + fail} 項）`)
if (fail) {
  console.log('\n失敗項目：')
  failures.forEach((f) => console.log(`  ✗ ${f}`))
  process.exit(1)
} else {
  console.log('✓ 階段一驗收全部通過')
}
