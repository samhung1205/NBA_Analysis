/**
 * API 路由 (規格書 §3.3)
 * ------------------------------------------------------------
 * 全部從資料庫讀取，前端不寫死任何假資料。
 * 階段二把真實資料寫進同一個資料庫後，這些端點與前端都無需修改。
 */

import { Hono } from 'hono'
import type { AppBindings } from '../db'
import { getDb } from '../db'
import * as q from '../db/queries'
import {
  todayTpe,
  tomorrowTpe,
  formatTpe,
} from '../lib/time'
import { devig, calcEdge, kellyFraction, edgeTier, impliedProb } from '../lib/edge'
import { getCurrentUser } from '../lib/auth'

type Env = { Bindings: AppBindings }

const api = new Hono<Env>()

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

function num(v: any): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

function parseJson(v: any) {
  if (v == null) return null
  if (typeof v === 'object') return v // Postgres JSONB 已是物件
  try {
    return JSON.parse(v)
  } catch {
    return null
  }
}

function shapeTeam(row: any, side: 'home' | 'away') {
  return {
    id: row[`${side}_team_id`],
    abbr: row[`${side}_abbr`],
    name: row[`${side}_name`],
    name_zh: row[`${side}_name_zh`],
  }
}

function shapeGame(row: any) {
  return {
    id: row.id,
    nba_game_id: row.nba_game_id,
    season: row.season,
    date_utc: row.date_utc,
    date_tpe_display: formatTpe(row.date_utc),
    status: row.status,
    arena: row.arena,
    home: shapeTeam(row, 'home'),
    away: shapeTeam(row, 'away'),
    score: {
      home: num(row.home_pts),
      away: num(row.away_pts),
      home_h1: num(row.home_h1),
      away_h1: num(row.away_h1),
      home_h2: num(row.home_h2),
      away_h2: num(row.away_h2),
      quarters: {
        home: [row.home_q1, row.home_q2, row.home_q3, row.home_q4].map(num),
        away: [row.away_q1, row.away_q2, row.away_q3, row.away_q4].map(num),
        home_ot: num(row.home_ot),
        away_ot: num(row.away_ot),
      },
    },
  }
}

function shapePrediction(p: any) {
  if (!p) return null
  return {
    model_version: p.model_version,
    created_at: p.created_at,
    home_win_prob: num(p.home_win_prob),
    pred_margin: num(p.pred_margin),
    pred_total: num(p.pred_total),
    half1: {
      home: num(p.pred_home_h1),
      away: num(p.pred_away_h1),
      margin:
        num(p.pred_home_h1) != null && num(p.pred_away_h1) != null
          ? Number((num(p.pred_home_h1)! - num(p.pred_away_h1)!).toFixed(2))
          : null,
      total:
        num(p.pred_home_h1) != null && num(p.pred_away_h1) != null
          ? Number((num(p.pred_home_h1)! + num(p.pred_away_h1)!).toFixed(2))
          : null,
    },
    half2: {
      home: num(p.pred_home_h2),
      away: num(p.pred_away_h2),
    },
    confidence: num(p.confidence),
    features: parseJson(p.features_json),
  }
}

function shapeOdds(o: any) {
  return {
    id: o.id,
    fetched_at: o.fetched_at,
    source: o.source,
    market: o.market,
    line: num(o.line),
    home_odds: num(o.home_odds),
    away_odds: num(o.away_odds),
    over_odds: num(o.over_odds),
    under_odds: num(o.under_odds),
    // ---- D.1 新增（純新增欄位；舊資料為 null）----
    bookmaker: o.bookmaker ?? null,
    market_type: o.market_type ?? null,
    period: o.period ?? null,
    outcome_set: o.outcome_set ?? null,
    away_line: num(o.away_line),
    draw_odds: num(o.draw_odds),
    model_target: o.model_target ?? null,
    model_threshold: num(o.model_threshold),
    market_status: o.market_status ?? null,
    source_updated_at: o.source_updated_at ?? null,
    last_seen_at: o.last_seen_at ?? null,
  }
}

/**
 * 同一來源有多家 bookmaker（The Odds API）時，總覽卡片只顯示一家作對照：
 * 固定偏好順序，其餘依 bookmaker key 字母序 —— 這是「顯示用的決定性選擇」，不是挑最佳盤、不是平均。
 * 全部 bookmaker 另在 international_books 回傳。
 */
const DISPLAY_BOOK_ORDER = ['pinnacle', 'draftkings', 'fanduel', 'betmgm', 'williamhill_us', 'betrivers']
function bookRank(b: string | null) {
  const i = DISPLAY_BOOK_ORDER.indexOf(b ?? '')
  return i === -1 ? DISPLAY_BOOK_ORDER.length : i
}

/**
 * 綜合模型預測與盤口，計算 edge。
 * 台彩(twsport)優先作為下注標的；國際盤(oddsapi)僅作對照。
 */
function buildEdgeAnalysis(pred: any, oddsList: any[]) {
  const shaped = oddsList.map(shapeOdds)
  const pick = (source: string, market: string) =>
    shaped
      .filter((o) => o.source === source && o.market === market)
      .sort((a, b) => bookRank(a.bookmaker) - bookRank(b.bookmaker) || String(a.bookmaker ?? '').localeCompare(String(b.bookmaker ?? '')))[0] ?? null

  const tw = {
    ml: pick('twsport', 'ml'),
    spread: pick('twsport', 'spread'),
    total: pick('twsport', 'total'),
    h1_spread: pick('twsport', 'h1_spread'),
    h1_total: pick('twsport', 'h1_total'),
  }
  const intl = {
    ml: pick('oddsapi', 'ml'),
    spread: pick('oddsapi', 'spread'),
    total: pick('oddsapi', 'total'),
  }

  const modelProb = pred ? num(pred.home_win_prob) : null
  const analysis: any = {
    twsport: tw,
    international: intl,
    international_books: shaped.filter((o) => o.source === 'oddsapi'),
    edges: [] as any[],
  }

  // 獨贏 (ML)：edge = 模型機率 − 台彩去抽水後公允機率
  if (tw.ml && modelProb != null) {
    const fair = devig(tw.ml.home_odds, tw.ml.away_odds)
    const homeEdge = calcEdge(modelProb, fair.home)
    const awayEdge = calcEdge(1 - modelProb, fair.away)
    const best =
      (homeEdge ?? -1) >= (awayEdge ?? -1)
        ? { side: 'home', edge: homeEdge, odds: tw.ml.home_odds, prob: modelProb }
        : { side: 'away', edge: awayEdge, odds: tw.ml.away_odds, prob: 1 - modelProb }
    analysis.edges.push({
      market: 'ml',
      market_label: '不讓分(獨贏)',
      selection: best.side,
      line: null,
      odds: best.odds,
      model_prob: best.prob,
      market_fair_prob: best.side === 'home' ? fair.home : fair.away,
      market_implied_prob: impliedProb(best.odds ?? 0),
      vig: fair.vig,
      edge: best.edge,
      tier: edgeTier(best.edge),
      kelly_quarter: kellyFraction(best.prob, best.odds, 0.25),
    })
  }

  // 讓分：以模型預測分差 vs 盤中線判斷方向（機率換算留待階段二模型輸出分佈後精算）
  if (tw.spread && pred && num(pred.pred_margin) != null) {
    const line = tw.spread.line ?? 0 // 慣例：主隊讓分為負值
    const predMargin = num(pred.pred_margin)!
    const diff = predMargin + line // >0 表示模型認為主隊可打贏此讓分盤
    analysis.edges.push({
      market: 'spread',
      market_label: '讓分',
      selection: diff > 0 ? 'home' : 'away',
      line,
      odds: diff > 0 ? tw.spread.home_odds : tw.spread.away_odds,
      model_value: Number(predMargin.toFixed(2)),
      line_gap: Number(diff.toFixed(2)), // 模型與盤口的分差落差
      edge: null,
      tier: Math.abs(diff) >= 3 ? 'mid' : Math.abs(diff) >= 1.5 ? 'low' : 'none',
      note: 'edge 需階段二模型輸出分差分佈後精算',
    })
  }

  // 大小分
  if (tw.total && pred && num(pred.pred_total) != null) {
    const line = tw.total.line ?? 0
    const predTotal = num(pred.pred_total)!
    const diff = predTotal - line
    analysis.edges.push({
      market: 'total',
      market_label: '大小分',
      selection: diff > 0 ? 'over' : 'under',
      line,
      odds: diff > 0 ? tw.total.over_odds : tw.total.under_odds,
      model_value: Number(predTotal.toFixed(2)),
      line_gap: Number(diff.toFixed(2)),
      edge: null,
      tier: Math.abs(diff) >= 5 ? 'mid' : Math.abs(diff) >= 2.5 ? 'low' : 'none',
      note: 'edge 需階段二模型輸出總分分佈後精算',
    })
  }

  // 上半場讓分
  if (tw.h1_spread && pred && num(pred.pred_home_h1) != null && num(pred.pred_away_h1) != null) {
    const line = tw.h1_spread.line ?? 0
    const predH1Margin = num(pred.pred_home_h1)! - num(pred.pred_away_h1)!
    const diff = predH1Margin + line
    analysis.edges.push({
      market: 'h1_spread',
      market_label: '上半場讓分',
      selection: diff > 0 ? 'home' : 'away',
      line,
      odds: diff > 0 ? tw.h1_spread.home_odds : tw.h1_spread.away_odds,
      model_value: Number(predH1Margin.toFixed(2)),
      line_gap: Number(diff.toFixed(2)),
      edge: null,
      tier: Math.abs(diff) >= 2 ? 'mid' : Math.abs(diff) >= 1 ? 'low' : 'none',
    })
  }

  // 上半場大小分
  if (tw.h1_total && pred && num(pred.pred_home_h1) != null && num(pred.pred_away_h1) != null) {
    const line = tw.h1_total.line ?? 0
    const predH1Total = num(pred.pred_home_h1)! + num(pred.pred_away_h1)!
    const diff = predH1Total - line
    analysis.edges.push({
      market: 'h1_total',
      market_label: '上半場大小分',
      selection: diff > 0 ? 'over' : 'under',
      line,
      odds: diff > 0 ? tw.h1_total.over_odds : tw.h1_total.under_odds,
      model_value: Number(predH1Total.toFixed(2)),
      line_gap: Number(diff.toFixed(2)),
      edge: null,
      tier: Math.abs(diff) >= 3 ? 'mid' : Math.abs(diff) >= 1.5 ? 'low' : 'none',
    })
  }

  return analysis
}

/** 組裝賽事列表（含預測與盤口）—— 總覽頁的核心資料 */
async function buildGameList(db: any, tpeDate: string) {
  const games = await q.getGamesByTpeDate(db, tpeDate)
  const ids = games.map((g) => g.id)
  const [predMap, oddsMap] = await Promise.all([
    q.getLatestPredictions(db, ids),
    q.getLatestOdds(db, ids),
  ])
  return games.map((row) => {
    const pred = predMap.get(row.id) ?? null
    const oddsList = oddsMap.get(row.id) ?? []
    return {
      ...shapeGame(row),
      prediction: shapePrediction(pred),
      odds: buildEdgeAnalysis(pred, oddsList),
    }
  })
}

/* ------------------------------------------------------------------ */
/* §3.3-5  GET /api/games/tomorrow — 隔日(台灣時間)賽事                  */
/* ------------------------------------------------------------------ */

api.get('/games/tomorrow', async (c) => {
  const db = await getDb(c.env)
  const date = tomorrowTpe()
  return c.json({ tpe_date: date, games: await buildGameList(db, date) })
})

/** GET /api/games/today — 今日賽事（總覽頁切換用） */
api.get('/games/today', async (c) => {
  const db = await getDb(c.env)
  const date = todayTpe()
  return c.json({ tpe_date: date, games: await buildGameList(db, date) })
})

/** GET /api/games?date=YYYY-MM-DD — 任意台灣日期 */
api.get('/games', async (c) => {
  const db = await getDb(c.env)
  const date = c.req.query('date') || todayTpe()
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return c.json({ error: 'date 格式須為 YYYY-MM-DD（台灣時間）' }, 400)
  }
  return c.json({ tpe_date: date, games: await buildGameList(db, date) })
})

/* ------------------------------------------------------------------ */
/* §3.3-6  GET /api/games/:id — 單場詳情                                */
/* ------------------------------------------------------------------ */

api.get('/games/:id', async (c) => {
  const db = await getDb(c.env)
  const id = Number(c.req.param('id'))
  if (!Number.isInteger(id)) return c.json({ error: 'invalid game id' }, 400)

  const row = await q.getGameById(db, id)
  if (!row) return c.json({ error: 'game not found' }, 404)

  const [pred, oddsList, oddsHistory, teamStats, playerStats, h2h, homeRecent, awayRecent, injuries] =
    await Promise.all([
      q.getLatestPredictionForGame(db, id),
      q.getLatestOdds(db, [id]),
      q.getOddsHistory(db, id),
      q.getTeamGameStats(db, id),
      q.getPlayerGameStats(db, id),
      q.getHeadToHead(db, row.home_team_id, row.away_team_id, 10),
      q.getTeamRecentGames(db, row.home_team_id, 10),
      q.getTeamRecentGames(db, row.away_team_id, 10),
      db.all(
        `SELECT i.status, i.reason, i.report_time_utc, p.name AS player_name, p.is_starter,
                t.abbr AS team_abbr
           FROM injuries i
           LEFT JOIN players p ON p.id = i.player_id
           LEFT JOIN teams t ON t.id = i.team_id
          WHERE i.game_id = ? ORDER BY t.abbr, i.status`,
        [id]
      ),
    ])

  return c.json({
    game: shapeGame(row),
    prediction: shapePrediction(pred),
    odds: buildEdgeAnalysis(pred, oddsMapToList(oddsList, id)),
    odds_history: oddsHistory.map(shapeOdds),
    team_game_stats: teamStats,
    player_game_stats: playerStats,
    head_to_head: h2h.map(shapeGame),
    recent_form: {
      home: homeRecent.map(shapeGame),
      away: awayRecent.map(shapeGame),
    },
    injuries,
  })
})

function oddsMapToList(map: Map<number, any[]>, id: number) {
  return map.get(id) ?? []
}

/* ------------------------------------------------------------------ */
/* §3.3-7  GET /api/injuries/today                                     */
/* ------------------------------------------------------------------ */

api.get('/injuries/today', async (c) => {
  const db = await getDb(c.env)
  const date = c.req.query('date') || todayTpe()
  const rows = await q.getInjuriesByTpeDate(db, date)

  // 依球隊分組，並標示主力缺陣警示
  const byTeam = new Map<string, any>()
  for (const r of rows) {
    const key = r.team_abbr ?? 'UNK'
    if (!byTeam.has(key)) {
      byTeam.set(key, {
        team: { id: r.team_id, abbr: r.team_abbr, name: r.team_name, name_zh: r.team_name_zh },
        players: [] as any[],
        key_players_out: 0,
      })
    }
    const t = byTeam.get(key)!
    const isKeyOut = !!r.is_starter && ['Out', 'Doubtful'].includes(r.status)
    if (isKeyOut) t.key_players_out += 1
    t.players.push({
      player_id: r.player_id,
      name: r.player_name,
      position: r.position,
      is_starter: !!r.is_starter,
      status: r.status,
      reason: r.reason,
      report_time_utc: r.report_time_utc,
      source: r.source,
      key_absence_alert: isKeyOut,
    })
  }

  return c.json({
    tpe_date: date,
    total_reports: rows.length,
    teams: [...byTeam.values()].sort((a, b) => b.key_players_out - a.key_players_out),
  })
})

/* ------------------------------------------------------------------ */
/* §3.3-8  GET /api/predictions/:gameId                                */
/* ------------------------------------------------------------------ */

api.get('/predictions/:gameId', async (c) => {
  const db = await getDb(c.env)
  const gameId = Number(c.req.param('gameId'))
  if (!Number.isInteger(gameId)) return c.json({ error: 'invalid game id' }, 400)
  const pred = await q.getLatestPredictionForGame(db, gameId)
  if (!pred) return c.json({ game_id: gameId, prediction: null, message: '尚無預測資料' }, 200)
  return c.json({ game_id: gameId, prediction: shapePrediction(pred) })
})

/* ------------------------------------------------------------------ */
/* §3.3-9  GET /api/odds/:gameId — 盤口快照歷史（供折線圖）              */
/* ------------------------------------------------------------------ */

api.get('/odds/:gameId', async (c) => {
  const db = await getDb(c.env)
  const gameId = Number(c.req.param('gameId'))
  if (!Number.isInteger(gameId)) return c.json({ error: 'invalid game id' }, 400)
  const rows = (await q.getOddsHistory(db, gameId)).map(shapeOdds)

  // 依 source+market 分組為時間序列，前端直接餵給 Chart.js
  const series = new Map<string, any>()
  for (const r of rows) {
    // D.1：同一來源的不同 bookmaker 是不同時間序列（不可混成一條線）
    const key = `${r.source}:${r.bookmaker ?? r.source}:${r.market}`
    if (!series.has(key)) {
      series.set(key, { source: r.source, bookmaker: r.bookmaker ?? r.source, market: r.market, points: [] as any[] })
    }
    series.get(key)!.points.push({
      t: r.fetched_at,
      line: r.line,
      home_odds: r.home_odds,
      away_odds: r.away_odds,
      over_odds: r.over_odds,
      under_odds: r.under_odds,
      draw_odds: r.draw_odds,
      market_status: r.market_status,
      last_seen_at: r.last_seen_at,
    })
  }
  return c.json({ game_id: gameId, snapshots: rows, series: [...series.values()] })
})

/* ------------------------------------------------------------------ */
/* §3.3-10  /api/bets — 個人下單紀錄 CRUD（需登入）                      */
/* ------------------------------------------------------------------ */

api.get('/bets', async (c) => {
  const user = await getCurrentUser(c)
  if (!user) return c.json({ error: 'unauthorized', message: '請先登入' }, 401)
  const db = await getDb(c.env)
  const rows = await q.getBets(db, user.uid)

  // 績效統計（以台彩實際賠率計算，符合規格書 §6）
  let staked = 0
  let returned = 0
  let win = 0
  let lose = 0
  let push = 0
  let pending = 0
  const curve: any[] = []
  const settled = [...rows].reverse().filter((r) => r.result !== 'pending')
  for (const r of rows) {
    if (r.result === 'pending') pending++
    else if (r.result === 'win') win++
    else if (r.result === 'lose') lose++
    else if (r.result === 'push' || r.result === 'void') push++
  }
  let cum = 0
  for (const r of settled) {
    staked += Number(r.stake) || 0
    const payout = r.payout != null ? Number(r.payout) : r.result === 'win' ? Number(r.stake) * Number(r.odds) : r.result === 'push' || r.result === 'void' ? Number(r.stake) : 0
    returned += payout
    cum += payout - (Number(r.stake) || 0)
    curve.push({ placed_at: r.placed_at, cumulative_pnl: Number(cum.toFixed(2)) })
  }

  return c.json({
    bets: rows,
    summary: {
      total: rows.length,
      win,
      lose,
      push,
      pending,
      hit_rate: win + lose > 0 ? win / (win + lose) : null,
      total_staked: Number(staked.toFixed(2)),
      total_returned: Number(returned.toFixed(2)),
      pnl: Number((returned - staked).toFixed(2)),
      roi: staked > 0 ? Number(((returned - staked) / staked).toFixed(4)) : null,
    },
    pnl_curve: curve,
  })
})

api.post('/bets', async (c) => {
  const user = await getCurrentUser(c)
  if (!user) return c.json({ error: 'unauthorized', message: '請先登入' }, 401)
  const db = await getDb(c.env)

  let body: any
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: '無效的 JSON 請求內容' }, 400)
  }

  const gameId = Number(body.game_id)
  const market = String(body.market ?? '')
  const selection = String(body.selection ?? '')
  const odds = Number(body.odds)
  const stake = Number(body.stake)
  const line = body.line === '' || body.line == null ? null : Number(body.line)

  const validMarkets = ['ml', 'spread', 'total', 'h1_ml', 'h1_spread', 'h1_total']
  const validSelections = ['home', 'away', 'over', 'under']
  if (!Number.isInteger(gameId)) return c.json({ error: 'game_id 必填且須為整數' }, 400)
  if (!validMarkets.includes(market))
    return c.json({ error: `market 須為: ${validMarkets.join(', ')}` }, 400)
  if (!validSelections.includes(selection))
    return c.json({ error: `selection 須為: ${validSelections.join(', ')}` }, 400)
  if (!(odds > 1)) return c.json({ error: 'odds 須為大於 1 的十進位賠率（台彩實際賠率）' }, 400)
  if (!(stake > 0)) return c.json({ error: 'stake 須大於 0' }, 400)

  const game = await q.getGameById(db, gameId)
  if (!game) return c.json({ error: '找不到該場比賽' }, 404)

  const isPg = db.driver === 'postgres'
  const sql =
    `INSERT INTO bets (user_id, game_id, market, selection, line, odds, stake, result, note)
     VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)` + (isPg ? ' RETURNING id' : '')
  const res = await db.run(sql, [
    user.uid,
    gameId,
    market,
    selection,
    line,
    odds,
    stake,
    body.note ? String(body.note) : null,
  ])
  return c.json({ ok: true, id: res.lastInsertId }, 201)
})

/** PATCH /api/bets/:id — 更新結算結果（個人手動結算，階段二可自動回寫） */
api.patch('/bets/:id', async (c) => {
  const user = await getCurrentUser(c)
  if (!user) return c.json({ error: 'unauthorized' }, 401)
  const db = await getDb(c.env)
  const id = Number(c.req.param('id'))
  const body = await c.req.json().catch(() => ({}))
  const result = String((body as any).result ?? '')
  if (!['pending', 'win', 'lose', 'push', 'void'].includes(result))
    return c.json({ error: 'result 須為 pending/win/lose/push/void' }, 400)

  const bet = await db.one<any>('SELECT * FROM bets WHERE id = ? AND user_id = ?', [id, user.uid])
  if (!bet) return c.json({ error: '找不到該筆紀錄' }, 404)

  const payout =
    result === 'win'
      ? Number(bet.stake) * Number(bet.odds)
      : result === 'push' || result === 'void'
        ? Number(bet.stake)
        : result === 'lose'
          ? 0
          : null
  await db.run('UPDATE bets SET result = ?, payout = ? WHERE id = ? AND user_id = ?', [
    result,
    payout,
    id,
    user.uid,
  ])
  return c.json({ ok: true, id, result, payout })
})

api.delete('/bets/:id', async (c) => {
  const user = await getCurrentUser(c)
  if (!user) return c.json({ error: 'unauthorized' }, 401)
  const db = await getDb(c.env)
  const id = Number(c.req.param('id'))
  await db.run('DELETE FROM bets WHERE id = ? AND user_id = ?', [id, user.uid])
  return c.json({ ok: true, id })
})

/* ------------------------------------------------------------------ */
/* §3.3-11  GET /api/system/status — 各資料來源最後更新時間               */
/* ------------------------------------------------------------------ */

api.get('/system/status', async (c) => {
  const db = await getDb(c.env)
  const rows = await q.getDataSources(db)
  const now = Date.now()

  const sources = rows.map((r) => {
    const last = r.last_success_at ? new Date(r.last_success_at).getTime() : null
    const ageMin = last ? Math.round((now - last) / 60000) : null
    const expected = r.expected_interval_min ?? null
    // 過期判定：超過預期間隔 2 倍視為 stale
    const stale = ageMin != null && expected != null ? ageMin > expected * 2 : null
    return {
      source_key: r.source_key,
      display_name: r.display_name,
      category: r.category,
      last_success_at: r.last_success_at,
      last_attempt_at: r.last_attempt_at,
      last_status: r.last_status,
      last_error: r.last_error,
      expected_interval_min: expected,
      records_updated: r.records_updated,
      last_outcome: r.last_outcome ?? null, // D.1：success / partial / no_nba_markets / parser_changed / quota_low …
      age_minutes: ageMin,
      stale,
      // 健康度：排程器回報的狀態優先，其次才用「資料是否過期」推斷
      health:
        r.last_status === 'error'
          ? 'error'
          : r.last_status === 'warn' || stale
            ? 'warn'
            : r.last_status === 'ok'
              ? 'ok'
              : 'unknown',
    }
  })

  return c.json({
    checked_at: new Date().toISOString(),
    db_driver: db.driver,
    stage: 'stage-1',
    note: '資料來源狀態由階段二排程器寫入 data_sources 表；目前為 seed 資料。',
    overall:
      sources.some((s) => s.health === 'error')
        ? 'error'
        : sources.some((s) => s.health === 'warn')
          ? 'warn'
          : 'ok',
    sources,
  })
})

/* ------------------------------------------------------------------ */
/* 回測 / 績效：模型指標（規格書 §3.4-15）                                */
/* ------------------------------------------------------------------ */

api.get('/metrics', async (c) => {
  const db = await getDb(c.env)
  const rows = await q.getModelMetrics(db)
  return c.json({ metrics: rows })
})

api.get('/teams', async (c) => {
  const db = await getDb(c.env)
  return c.json({ teams: await q.getAllTeams(db) })
})

export default api
