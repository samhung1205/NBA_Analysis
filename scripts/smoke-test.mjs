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
  ['/decision', 'decision-games', '決策中心（D.5）'],
  ['/bankroll', 'bankroll-summary', '資金（D.5）'],
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

// D.3：理論注碼（Python sizing engine 寫入 bet_sizing_snapshots；API 只讀）——這裡只驗證不變量，不重算 Kelly
const EPS = 1e-9
const sz = g0?.odds?.sizing
ok('  odds.sizing 存在（risk-v1、已 sizing、policy 0.25 / 2% / 3% / 8%）',
  sz?.risk_policy_version === 'risk-v1' && sz?.status === 'sized' && sz?.policy?.kelly_multiplier === 0.25 &&
  sz?.policy?.max_bet_fraction === 0.02 && sz?.policy?.max_game_fraction === 0.03 && sz?.policy?.max_day_fraction === 0.08,
  `status=${sz?.status}`)
const sOut = (pr?.markets || []).flatMap((m) => m.outcomes).map((o) => o.sizing).filter(Boolean)
ok('  每個定價 outcome 都有 sizing（full / 分數 / 上限後 / 最終 / 資格 / 原因 / 警示）',
  sOut.length > 0 && sOut.length === (pr?.markets || []).flatMap((m) => m.outcomes).length &&
  sOut.every((s) => 'full_kelly_fraction' in s && 'fractional_kelly_fraction' in s && 'single_bet_capped_fraction' in s &&
    s.final_stake_fraction != null && !!s.qualification_status && Array.isArray(s.reasons) && Array.isArray(s.warnings)))
ok('  最終 ≤ 單筆上限後 ≤ 分數 Kelly ≤ full Kelly；單筆 ≤ 2%',
  sOut.every((s) => s.full_kelly_fraction == null ||
    (s.final_stake_fraction <= s.single_bet_capped_fraction + EPS && s.single_bet_capped_fraction <= s.fractional_kelly_fraction + EPS &&
     s.fractional_kelly_fraction <= s.full_kelly_fraction + EPS && s.single_bet_capped_fraction <= 0.02 + EPS)))
ok('  EV ≤ 0 → stake 0；actionable ⇔ 最終 > 0',
  sOut.every((s) => (s.ev_per_unit > 0 || s.final_stake_fraction === 0) && s.actionable === (s.final_stake_fraction > 0)))
ok('  同一場合計 ≤ 3%（含台彩 + 國際盤 + 各玩法）',
  sOut.reduce((a, s) => a + s.final_stake_fraction, 0) <= 0.03 + EPS && sOut.some((s) => s.actionable))
ok('  同一市場最多一個 outcome 有正注碼', (pr?.markets || []).every((m) => m.outcomes.filter((o) => o.sizing?.final_stake_fraction > 0).length <= 1))
ok('  deprecated edges[].kelly_quarter 仍為 null（Kelly 只在 odds.sizing）', (g0?.odds?.edges || []).every((e) => e.kelly_quarter === null))
const daySz = await req(`/api/sizing?date=${sz?.betting_day}`)
const dayOut = (daySz.body?.games || []).flatMap((g) => g.outcomes)
ok('GET /api/sizing?date=<betting day> 200：單日合計 ≤ 8%、等於 daily_exposure.after',
  daySz.status === 200 && daySz.body?.status === 'sized' && dayOut.length > 0 &&
  daySz.body.daily_exposure.after <= 0.08 + EPS &&
  Math.abs(dayOut.reduce((a, s) => a + s.final_stake_fraction, 0) - daySz.body.daily_exposure.after) < 1e-9,
  `status=${daySz.status}/${daySz.body?.status}`)
ok('  每場合計 ≤ 3%', (daySz.body?.games || []).every((g) => g.outcomes.reduce((a, s) => a + s.final_stake_fraction, 0) <= 0.03 + EPS))
ok('GET /api/sizing?date=abc 回 400', (await req('/api/sizing?date=abc')).status === 400)

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
// D.5：新帳號沒有 bankroll → 風險額度無法驗證 → 需要明確確認（不拒絕事實、不自動縮小金額）
ok('POST /api/bets（無 bankroll）先回 409 confirmation_required（missing_context）',
  created.status === 409 && created.body?.error === 'confirmation_required' &&
  created.body?.checks?.includes('bankroll_not_configured') && created.body?.compliance_if_confirmed === 'missing_context',
  JSON.stringify(created.body))
const createdOk = await req('/api/bets', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    game_id: g0.id, market: 'spread', selection: 'home',
    line: -1.5, odds: 1.87, stake: 1000, note: 'smoke test', confirm_override: true,
  }),
})
ok('POST /api/bets 201 並回傳 id（明確確認後；compliance = missing_context）', createdOk.status === 201 && createdOk.body?.id != null &&
  createdOk.body?.bet?.strategy_compliance === 'missing_context' && createdOk.body?.bet?.origin === 'manual_unlinked',
  JSON.stringify(createdOk.body))
const betId = createdOk.body?.id

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
ok('DELETE /api/bets/:id 200（D.5：改為作廢，不刪除）', deleted.status === 200 && deleted.body?.record_status === 'voided')
const afterDel = await req('/api/bets')
ok('  作廢後不再出現於預設清單', !afterDel.body?.bets?.some((b) => b.id === betId))
const withInactive = await req('/api/bets?include_inactive=1')
ok('  include_inactive=1 仍可看到（稽核軌跡保留）', withInactive.body?.bets?.some((b) => b.id === betId && b.record_status === 'voided'))
const betDetail = await req(`/api/bets/${betId}`)
ok('  GET /api/bets/:id 含 bet_events（recorded → settled_manual → voided）',
  ['recorded', 'settled_manual', 'voided'].every((t) => betDetail.body?.events?.some((e) => e.event_type === t)),
  JSON.stringify(betDetail.body?.events?.map((e) => e.event_type)))
const settleVoided = await req(`/api/bets/${betId}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ result: 'lose' }) })
ok('  已作廢紀錄不可再結算（409）', settleVoided.status === 409)

/* ===================== D.5 bankroll（新帳號） ===================== */
section('D.5 bankroll ledger（新帳號，append-only）')
const post = (path, body) => req(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
ok('GET /api/bankroll 未設定 → not_configured', (await req('/api/bankroll')).body?.status === 'not_configured')
ok('deposit 在 initial_funding 前 → 409', (await post('/api/bankroll/entries', { entry_type: 'deposit', amount: 100 })).status === 409)
const fund = await post('/api/bankroll/entries', { entry_type: 'initial_funding', amount: 50000, currency: 'TWD' })
ok('initial_funding 201（建立 bankroll）', fund.status === 201, JSON.stringify(fund.body))
ok('第二次 initial_funding → 409', (await post('/api/bankroll/entries', { entry_type: 'initial_funding', amount: 1 })).status === 409)
ok('deposit 負數 → 400（方向由類型決定）', (await post('/api/bankroll/entries', { entry_type: 'deposit', amount: -5 })).status === 400)
ok('adjustment 無原因 → 400', (await post('/api/bankroll/entries', { entry_type: 'adjustment', amount: -5 })).status === 400)
const dep = await post('/api/bankroll/entries', { entry_type: 'deposit', amount: 1000, client_request_id: 'smoke-dep-0001' })
ok('deposit 201', dep.status === 201)
ok('同一 client_request_id 重送 → replay（不重複入帳）',
  (await post('/api/bankroll/entries', { entry_type: 'deposit', amount: 1000, client_request_id: 'smoke-dep-0001' })).body?.replay === true)
const rev = await post('/api/bankroll/entries', { entry_type: 'reversal', reverses_entry_id: dep.body?.entry?.id, reason: 'smoke' })
ok('reversal 201（沖銷，不修改原筆）', rev.status === 201)
const br = await req('/api/bankroll')
ok('GET /api/bankroll：ledger 3 筆（initial / deposit / reversal），尚待 Python 重新計算', br.body?.ledger?.length === 3 &&
  br.body?.summary == null && br.body?.account?.currency === 'TWD', JSON.stringify(br.body?.ledger?.map((e) => e.entry_type)))
ok('withdrawal 在 Python 物化前 → 409 recalculation_pending（不以過期餘額判斷）',
  (await post('/api/bankroll/entries', { entry_type: 'withdrawal', amount: 10 })).body?.error === 'recalculation_pending')

/* ===================== D.5 決策中心（demo 帳號，seed 物化） ===================== */
section('D.5 決策中心 / 記錄下注（demo seed）')
const otherUserBets = (await req('/api/bets?include_inactive=1')).body?.bets || []
await req('/api/auth/logout', { method: 'POST' })
cookie = ''
ok('未登入 GET /api/decision-board → 401', (await req('/api/decision-board')).status === 401)
ok('未登入 GET /api/bankroll → 401', (await req('/api/bankroll')).status === 401)
ok('未登入 POST /api/bankroll/entries → 401', (await post('/api/bankroll/entries', { entry_type: 'deposit', amount: 1 })).status === 401)
ok('未登入 POST /api/bets/1/void → 401', (await post('/api/bets/1/void', { reason: 'x' })).status === 401)
await post('/api/auth/login', { email: 'demo@example.com', password: 'nba12345678' })
const tomorrowDate = tomorrow.body?.tpe_date
const board = await req(`/api/decision-board?date=${tomorrowDate}`)
const B = board.body || {}
ok('GET /api/decision-board?date=<明日> 200、已物化、risk_state current', board.status === 200 && B.materialized === true &&
  B.risk_state?.status === 'current' && B.risk_state?.capacity_valid === true, JSON.stringify(B.risk_state))
ok('  回傳 summary / games / actual_exposure / risk_limits / bankroll / evidence / system_health',
  ['summary', 'games', 'actual_exposure', 'risk_limits', 'bankroll', 'evidence', 'system_health'].every((k) => B[k] != null))
ok('  risk-v1 未修改（¼ Kelly / 2% / 3% / 8%）', B.risk_limits?.kelly_multiplier === 0.25 && B.risk_limits?.max_bet_fraction === 0.02 &&
  B.risk_limits?.max_game_fraction === 0.03 && B.risk_limits?.max_day_fraction === 0.08 && B.risk_limits?.risk_policy_version === 'risk-v1')
const X = B.actual_exposure || {}
ok('  actual exposure = Python 物化（Σ bets stake = day_stake；3,000 / 100,000 = 3%）',
  Math.abs((X.bets || []).reduce((a, b) => a + (b.counted ? b.stake : 0), 0) - X.day_stake) < 1e-9 &&
  Math.abs(X.day_fraction - 0.03) < 1e-12 && Math.abs(X.remaining_day_fraction - 0.05) < 1e-12, JSON.stringify({ d: X.day_fraction, r: X.remaining_day_fraction }))
ok('  legacy 注單計入且有警示', (X.bets || []).some((b) => b.legacy && b.counted) && (X.warnings || []).some((w) => w.includes('legacy_unlinked_bet')))
ok('  bankroll：day-start 100,000、目前 100,000、未結算 stake 3,000', B.bankroll?.day_start_bankroll === 100000 &&
  B.bankroll?.current_bankroll === 100000 && B.bankroll?.committed_open_stake === 3000)
const allTw = (B.games || []).flatMap((g) => g.taiwan)
const allIntl = (B.games || []).flatMap((g) => g.international)
const qual = allTw.filter((o) => o.decision_status === 'qualified')
ok('  有 qualified 台彩機會，capacity_valid 且建議金額 ≤ 最大新增額度', qual.length > 0 &&
  qual.every((o) => o.capacity_valid && o.suggested_stake_amount <= o.max_additional_stake_amount + 1e-9 && o.ev_per_unit > 0))
ok('  新增額度合計 ≤ 當日剩餘（Python 物化值）', qual.reduce((a, o) => a + o.user_adjusted_fraction, 0) <= X.remaining_day_fraction + 1e-12)
ok('  每場新增額度 ≤ 同場剩餘', (B.games || []).every((g) => g.taiwan.filter((o) => o.decision_status === 'qualified')
  .reduce((a, o) => a + o.user_adjusted_fraction, 0) <= (g.remaining_game_fraction ?? 0.03) + 1e-12))
const recorded = allTw.filter((o) => o.decision_status === 'already_recorded')
ok('  已實際下注的市場 → already_recorded、新增額度 0（不 top-up）', recorded.length > 0 &&
  recorded.every((o) => o.user_adjusted_fraction === 0 && o.status_group === 'recorded' && o.capacity_valid === false))
ok('  國際盤只作 diagnostic：沒有額度、標 international_market_diagnostic', allIntl.length > 0 &&
  allIntl.every((o) => o.status_group === 'diagnostic' && o.max_additional_stake_amount == null &&
    o.evidence_label === 'international_market_diagnostic' && o.capacity_valid === false))
ok('  無 Python 端以外的 stake：TS 只傳遞物化值（qualified 金額 = 物化列）', qual.every((o) => typeof o.max_additional_stake_amount === 'number'))
ok('  evidence：歷史投注證據 unavailable、bootstrap insufficient、prospective badge',
  B.evidence?.historical_betting_evidence?.status === 'unavailable' && B.evidence?.prospective_paper?.bootstrap?.status === 'insufficient_sample' &&
  B.evidence?.badge === 'prospective_validation' && B.evidence?.affects_strategy === false)
ok('  system_health 含 台彩 / Odds API / 定價 / sizing / paper / 決策物化 / bankroll / exposure',
  ['schedule', 'predictions', 'injuries', 'taiwan_odds', 'odds_api', 'pricing', 'sizing', 'paper_strategy', 'decision_board', 'bankroll',
    'actual_exposure'].every((k) => B.system_health?.components?.some((c) => c.key === k)))
const todayBoard = await req(`/api/decision-board?date=${today.body?.tpe_date}`)
ok('今日（無物化）→ not_materialized、無任何有效額度', todayBoard.body?.materialized === false &&
  todayBoard.body?.risk_state?.capacity_valid === false && (todayBoard.body?.games || []).every((g) => g.taiwan.length === 0))
ok('GET /api/decision-board?date=abc → 400', (await req('/api/decision-board?date=abc')).status === 400)

// 記錄下注：超出 risk-v1 → 需要明確確認 + 原因（不會默默截斷）
const q0 = qual[0]
const tooBig = await post('/api/bets', { decision_opportunity_id: q0.id, odds: q0.decimal_odds, stake: 999999 })
ok('超出 risk-v1 → 409 confirmation_required（Exceeds risk-v1 limit、user_override、需原因）', tooBig.status === 409 &&
  tooBig.body?.checks?.some((c) => c.startsWith('exceeds_risk_v1')) && tooBig.body?.compliance_if_confirmed === 'user_override' &&
  tooBig.body?.requires_reason === true, JSON.stringify(tooBig.body))
ok('  確認但未填原因 → 400', (await post('/api/bets', { decision_opportunity_id: q0.id, odds: q0.decimal_odds, stake: 999999,
  confirm_override: true })).status === 400)
const dup = await post('/api/bets', { decision_opportunity_id: recorded[0].id, odds: recorded[0].decimal_odds, stake: 10 })
ok('已記錄的機會 → 409（already_recorded_no_top_up）', dup.status === 409 && dup.body?.checks?.includes('already_recorded_no_top_up'))
const intlTry = await post('/api/bets', { decision_opportunity_id: allIntl[0].id, odds: 2, stake: 10 })
ok('國際盤 diagnostic 不可當平台機會記錄 → 400', intlTry.status === 400)

// 兩個分頁同時記錄（同一份剩餘額度）→ 只有一筆成功
const q1 = qual.find((o) => o.id !== q0.id) || q0
const stakeA = Math.floor(q0.suggested_stake_amount)
const [ra, rb] = await Promise.all([
  post('/api/bets', { decision_opportunity_id: q0.id, odds: q0.decimal_odds - 0.03, stake: stakeA, client_request_id: 'smoke-tab-a-001' }),
  post('/api/bets', { decision_opportunity_id: q1.id, odds: q1.decimal_odds, stake: Math.floor(q1.suggested_stake_amount), client_request_id: 'smoke-tab-b-001' }),
])
const codes = [ra.status, rb.status].sort()
ok('兩個同時送出 → 一筆 201、一筆 409（不會兩筆都用掉同一份額度）', codes[0] === 201 && codes[1] === 409,
  JSON.stringify([ra.status, ra.body?.error, rb.status, rb.body?.error]))
const won = ra.status === 201 ? ra : rb
const wonReq = ra.status === 201 ? 'smoke-tab-a-001' : 'smoke-tab-b-001'
ok('  成功的一筆：platform_opportunity / compliant / 保存參考賠率與實際賠率', won.body?.bet?.origin === 'platform_opportunity' &&
  won.body?.bet?.strategy_compliance === 'compliant' && won.body?.bet?.reference_decimal_odds != null && won.body?.bet?.odds != null &&
  won.body?.bet?.reference_odds_snapshot_id != null && won.body?.bet?.decision_opportunity_id != null, JSON.stringify(won.body?.bet))
if (ra.status === 201) ok('  實際賠率 ≠ 平台觀察賠率時兩者都保存', Math.abs(won.body.bet.odds - won.body.bet.reference_decimal_odds) > 0.01)
const replayed = await post('/api/bets', { decision_opportunity_id: q0.id, odds: q0.decimal_odds, stake: 1, client_request_id: wonReq })
ok('  同一 client_request_id 重送 → replay，不重複建立', replayed.status === 200 && replayed.body?.replay === true && replayed.body?.id === won.body?.id)
const after = (await req(`/api/decision-board?date=${tomorrowDate}`)).body
ok('記錄後決策中心 → risk_recalculation_pending，所有額度暫不有效', after?.risk_state?.status === 'recalculation_pending' &&
  (after?.games || []).flatMap((g) => g.taiwan).every((o) => o.capacity_valid === false))
const third = await post('/api/bets', { decision_opportunity_id: q1.id, odds: q1.decimal_odds, stake: 10 })
ok('重新計算前再記錄 → 409 confirmation_required（risk_recalculation_pending → missing_context）', third.status === 409 &&
  third.body?.checks?.includes('risk_recalculation_pending') && third.body?.compliance_if_confirmed === 'missing_context')
const demoBets = (await req('/api/bets')).body
ok('GET /api/bets：含 legacy 注單（origin null）與 provenance 欄位', demoBets?.d5_schema === true &&
  demoBets?.bets?.some((b) => b.legacy === true) && demoBets?.bets?.some((b) => b.origin === 'platform_opportunity'))
ok('  actual_performance（Python 物化）標示 User actual betting record',
  demoBets?.actual_performance?.label === 'user_actual_betting_record')
ok('  其他使用者的注單看不到 / 改不到', !(demoBets?.bets || []).some((b) => otherUserBets.some((o) => o.id === b.id)) &&
  (await req(`/api/bets/${otherUserBets[0]?.id ?? 999999}`)).status === 404)
const corr = await post(`/api/bets/${won.body?.id}/correction`, { stake: stakeA - 1, reason: 'smoke typo' })
ok('更正 → 新紀錄 supersedes 舊紀錄（舊紀錄不覆寫）', corr.status === 201 && corr.body?.supersedes_bet_id === won.body?.id &&
  corr.body?.bet?.strategy_compliance === won.body?.bet?.strategy_compliance)
const oldAfter = (await req(`/api/bets/${won.body?.id}`)).body?.bet
ok('  舊紀錄 record_status = superseded、stake 不變', oldAfter?.record_status === 'superseded' && oldAfter?.stake === won.body?.bet?.stake)

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
