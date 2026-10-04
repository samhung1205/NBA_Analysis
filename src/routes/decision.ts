/**
 * Phase D.5 API：決策中心 / 實際下注紀錄 / 個人 strategy bankroll（全部需登入）
 * ------------------------------------------------------------
 *   GET  /api/decision-board?date=YYYY-MM-DD   當日決策檢視（Python 物化；summary / games[] / actual_exposure /
 *                                               risk_limits / bankroll / evidence / system_health 一次回傳）
 *   GET  /api/bets[?date=&include_inactive=1]   個人實際下注（含 provenance / compliance）
 *   GET  /api/bets/:id                          單筆 + 稽核紀錄（bet_events）
 *   POST /api/bets                              「記錄下注」：使用者已在外部下注後記錄（平台從不自動下注）
 *   PATCH /api/bets/:id                         手動結算（稽核）
 *   POST /api/bets/:id/void                     作廢（不刪除）
 *   POST /api/bets/:id/correction               更正：新列 supersedes 舊列（執行欄位不覆寫）
 *   DELETE /api/bets/:id                        = void（相容舊前端；資料不會真的刪除）
 *   GET  /api/bankroll                          bankroll 摘要（Python）+ ledger + day-start
 *   POST /api/bankroll/entries                  initial_funding / deposit / withdrawal / adjustment / reversal
 *
 * 數學（EV / Kelly / exposure / 剩餘額度 / bankroll 金額）全部在 Python；這裡只把輸入與 Python 物化的上限「比較」。
 * 0008 未套用 → bets 端點退回 D.5 以前的行為、決策中心 / bankroll 回 unavailable（部署順序見 docs/phase-d5-report.md）。
 */

import { Hono } from 'hono'
import type { AppBindings, Db } from '../db'
import { getDb } from '../db'
import * as q from '../db/queries'
import * as dq from '../db/decision-queries'
import { getCurrentUser } from '../lib/auth'
import { formatTpe, todayTpe, tpeDateString } from '../lib/time'
import {
  DECISION_VERSION, GROUP_TEXT, TAIWAN_ODDS_TEXT, day, evaluateBet, iso, json, num, readiness, riskStateOf,
  shapeOpportunity, shapeSnapshot,
} from '../lib/decision'

type Env = { Bindings: AppBindings }
const r = new Hono<Env>()

const VALID_MARKETS = ['ml', 'spread', 'total', 'h1_ml', 'h1_spread', 'h1_total']
const VALID_SELECTIONS = ['home', 'away', 'over', 'under', 'draw']
const VALID_SOURCES = ['twsport', 'oddsapi', 'other']
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/

function nowIso() {
  return new Date().toISOString()
}

async function requireUser(c: any) {
  const user = await getCurrentUser(c)
  return user
}

function unauthorized(c: any) {
  return c.json({ error: 'unauthorized', message: '請先登入（個人 bankroll 與實際下注為私人資料）' }, 401)
}

function shapeBet(b: any) {
  return {
    ...b,
    line: num(b.line),
    odds: num(b.odds),
    stake: num(b.stake),
    payout: num(b.payout),
    reference_decimal_odds: num(b.reference_decimal_odds),
    model_threshold: num(b.model_threshold),
    stake_fraction_at_placement: num(b.stake_fraction_at_placement),
    betting_day: day(b.betting_day),
    placed_at: iso(b.placed_at),
    recorded_at: iso(b.recorded_at),
    voided_at: iso(b.voided_at),
    settled_at: iso(b.settled_at),
    date_utc: iso(b.date_utc),
    reference_context: json(b.reference_context, null),
    risk_check: json(b.risk_check, null),
    record_status: b.record_status ?? 'active',
    legacy: b.origin == null,
  }
}

/* ------------------------------------------------------------------ */
/* GET /api/decision-board                                             */
/* ------------------------------------------------------------------ */

r.get('/decision-board', async (c) => {
  const date = c.req.query('date') || todayTpe()
  if (!DATE_RE.test(date)) return c.json({ error: 'date 格式須為 YYYY-MM-DD（Asia/Taipei betting day）' }, 400)
  const user = await requireUser(c)
  if (!user) return unauthorized(c)
  const db = await getDb(c.env)
  const now = Date.now()
  const [games, sources] = await Promise.all([q.getGamesByTpeDate(db, date), q.getDataSources(db)])
  const gameIds = games.map((g) => Number(g.id))
  const [predMap, absences] = await Promise.all([q.getLatestPredictions(db, gameIds), dq.getKeyAbsences(db, gameIds)])

  if (!(await dq.hasD5Schema(db))) {
    return c.json({
      betting_day: date, decision_version: DECISION_VERSION, status: 'unavailable',
      reason: 'migration_0008_not_applied', risk_state: { status: 'not_materialized', capacity_valid: false, reasons: [] },
      games: games.map((g) => displayGame(g, null, [], predMap.get(g.id), absences.get(Number(g.id)) ?? 0, false)),
      system_health: readiness(sources, now, { bankroll: 'not_configured', actualExposure: 'not_configured' }),
    })
  }

  const account = await dq.getAccount(db, user.uid)
  const [snapRow, ledgerMax, eventMax] = await Promise.all([
    dq.getLatestSnapshot(db, user.uid, date),
    dq.getLedgerMax(db, account ? Number(account.id) : null),
    dq.getUserEventMax(db, user.uid),
  ])
  const snap = snapRow ? shapeSnapshot(snapRow) : null
  const opps = snap ? (await dq.getOpportunities(db, snap.id)).map(shapeOpportunity) : []
  const risk = riskStateOf(snap, { accountVersion: account ? num(account.risk_state_version) : null, ledgerMax, eventMax, now })
  for (const o of opps) o.capacity_valid = risk.capacity_valid && o.decision_status === 'qualified' && o.scope_kind === 'taiwan_primary'
  const bankrollStatus = !account ? 'not_configured' : risk.capacity_valid ? 'healthy' : 'waiting'
  const exposureStatus = !snap ? 'waiting' : snap.actual_exposure?.incomplete ? 'blocked'
    : risk.status === 'current' ? 'healthy' : risk.materialization_stale ? 'stale' : 'waiting'
  const health = readiness(sources, now, { bankroll: bankrollStatus, actualExposure: exposureStatus })

  return c.json({
    betting_day: date,
    decision_version: DECISION_VERSION,
    status: snap ? snap.status : games.length ? 'not_materialized' : 'no_games',
    materialized: !!snap,
    snapshot_id: snap?.id ?? null,
    as_of: snap?.as_of ?? null,
    last_confirmed_at: snap?.last_confirmed_at ?? null,
    risk_state: risk,
    summary: snap?.summary ?? null,
    risk_limits: snap?.risk_limits ?? null,
    bankroll: snap?.bankroll ?? (account ? null : { configured: false, reason: 'bankroll_not_configured' }),
    actual_exposure: snap?.actual_exposure ?? null,
    evidence: snap?.evidence ?? null,
    warnings: snap?.warnings ?? [],
    games: games.map((g) => displayGame(g, snap, opps, predMap.get(g.id), absences.get(Number(g.id)) ?? 0, true)),
    system_health: health,
    labels: { groups: GROUP_TEXT, taiwan_odds: TAIWAN_ODDS_TEXT },
    disclaimer: 'Qualified opportunity / Positive EV / Theoretical sizing / Risk-adjusted capacity：不是獲利保證；'
      + '目前沒有歷史投注績效證據，2026-27 為前瞻驗證期。平台不會自動下注，所有下注由使用者自行在台灣運彩完成後記錄。',
  })
})

function displayGame(g: any, snap: any, opps: any[], pred: any, keyAbsences: number, d5: boolean) {
  const sg = snap ? (snap.games || []).find((x: any) => Number(x.game_id) === Number(g.id)) : null
  const mine = opps.filter((o) => o.game_id === Number(g.id))
  return {
    game_id: Number(g.id),
    tipoff_utc: iso(g.date_utc),
    tipoff_display: formatTpe(iso(g.date_utc) as string),
    game_status: g.status,
    home: { id: g.home_team_id, abbr: g.home_abbr, name: g.home_name, name_zh: g.home_name_zh },
    away: { id: g.away_team_id, abbr: g.away_abbr, name: g.away_name, name_zh: g.away_name_zh },
    score: { home: num(g.home_pts), away: num(g.away_pts) },
    status_group: sg?.status_group ?? null,
    taiwan_odds_state: sg?.taiwan_odds_state ?? null,
    taiwan_odds_reason: sg?.taiwan_odds_reason ?? null,
    blockers: sg?.blockers ?? (d5 ? ['decision_board_not_materialized'] : ['migration_0008_not_applied']),
    prediction: sg?.prediction ?? null,
    prediction_display: pred
      ? { model_version: pred.model_version, created_at: iso(pred.created_at), pred_margin: num(pred.pred_margin),
          pred_total: num(pred.pred_total), home_win_prob: num(pred.home_win_prob), note: 'display_only_latest_row' }
      : null,
    key_absences: keyAbsences,
    actual_game_stake: num(sg?.actual_game_stake),
    actual_game_fraction: num(sg?.actual_game_fraction),
    remaining_game_fraction: num(sg?.remaining_game_fraction),
    remaining_game_amount: num(sg?.remaining_game_amount),
    actual_bet_ids: sg?.actual_bet_ids ?? [],
    paper_decision: sg?.paper_decision ?? null,
    taiwan: mine.filter((o) => o.scope_kind === 'taiwan_primary'),
    international: mine.filter((o) => o.scope_kind !== 'taiwan_primary'),
  }
}

/* ------------------------------------------------------------------ */
/* /api/bets                                                           */
/* ------------------------------------------------------------------ */

r.get('/bets', async (c) => {
  const user = await requireUser(c)
  if (!user) return unauthorized(c)
  const db = await getDb(c.env)
  const d5 = await dq.hasD5Schema(db)
  const date = c.req.query('date')
  if (date && !DATE_RE.test(date)) return c.json({ error: 'date 格式須為 YYYY-MM-DD' }, 400)
  const rows = d5
    ? await dq.getUserBets(db, user.uid, { day: date, includeInactive: c.req.query('include_inactive') === '1' })
    : await q.getBets(db, user.uid)
  const active = rows.filter((b) => (b.record_status ?? 'active') === 'active')
  const latest = d5 ? await dq.getLatestSnapshotAnyDay(db, user.uid) : null
  return c.json({
    d5_schema: d5,
    bets: rows.map(shapeBet),
    ...legacySummary(active),
    actual_performance: latest ? shapeSnapshot(latest).actual_performance : null,
    actual_performance_as_of: latest ? iso(latest.last_confirmed_at) : null,
  })
})

/**
 * 舊版（階段一）統計：保留欄位形狀供既有前端 / smoke test。
 * 權威的 User actual betting record 由 Python 物化（actual_performance）；這裡不含 exposure / Kelly / 額度。
 */
function legacySummary(rows: any[]) {
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
    curve.push({ placed_at: iso(r.placed_at), cumulative_pnl: Number(cum.toFixed(2)) })
  }
  return {
    summary: {
      total: rows.length, win, lose, push, pending,
      hit_rate: win + lose > 0 ? win / (win + lose) : null,
      total_staked: Number(staked.toFixed(2)),
      total_returned: Number(returned.toFixed(2)),
      pnl: Number((returned - staked).toFixed(2)),
      roi: staked > 0 ? Number(((returned - staked) / staked).toFixed(4)) : null,
      label: 'user_actual_betting_record（legacy summary；不是策略績效）',
    },
    pnl_curve: curve,
  }
}

r.get('/bets/:id', async (c) => {
  const user = await requireUser(c)
  if (!user) return unauthorized(c)
  const db = await getDb(c.env)
  const id = Number(c.req.param('id'))
  if (!Number.isInteger(id)) return c.json({ error: 'invalid bet id' }, 400)
  const bet = await dq.getBetForUser(db, id, user.uid)
  if (!bet) return c.json({ error: '找不到該筆紀錄' }, 404)
  const events = (await dq.hasD5Schema(db)) ? await dq.getBetEvents(db, id) : []
  return c.json({ bet: shapeBet(bet), events: events.map((e) => ({ ...e, payload: json(e.payload, {}) })) })
})

r.post('/bets', async (c) => {
  const user = await requireUser(c)
  if (!user) return unauthorized(c)
  const db = await getDb(c.env)
  let body: any
  try {
    body = await c.req.json()
  } catch {
    return c.json({ error: '無效的 JSON 請求內容' }, 400)
  }
  if (!(await dq.hasD5Schema(db))) return legacyCreate(c, db, user.uid, body)

  const requestId = typeof body.client_request_id === 'string' && /^[\w:-]{8,100}$/.test(body.client_request_id)
    ? body.client_request_id : crypto.randomUUID()
  const replay = await dq.getBetByRequest(db, user.uid, requestId)
  if (replay) return c.json({ ok: true, id: Number(replay.id), replay: true, bet: shapeBet(replay) }, 200)

  // ---- 機會來源（平台 opportunity）或手動 ---- //
  let opp: any = null
  if (body.decision_opportunity_id != null && body.decision_opportunity_id !== '') {
    const oid = Number(body.decision_opportunity_id)
    if (!Number.isInteger(oid)) return c.json({ error: 'decision_opportunity_id 須為整數' }, 400)
    const row = await dq.getOpportunityForUser(db, oid, user.uid)
    if (!row) return c.json({ error: '找不到該機會（或不屬於目前使用者）' }, 404)
    opp = shapeOpportunity(row)
    if (opp.scope_kind !== 'taiwan_primary') {
      return c.json({ error: 'international_diagnostic_not_recordable',
        message: '國際盤只作診斷（不是台彩證據）；若真的在其他管道下注，請用手動記錄。' }, 400)
    }
  }
  const gameId = opp ? opp.game_id : Number(body.game_id)
  const market = opp ? opp.market : String(body.market ?? '')
  const selection = opp ? opp.side : String(body.selection ?? '')
  if (opp && ((body.game_id != null && Number(body.game_id) !== gameId) || (body.market && body.market !== market)
      || (body.selection && body.selection !== selection))) {
    return c.json({ error: 'opportunity_mismatch', message: '比賽 / 玩法 / 方向與所選機會不一致' }, 400)
  }
  const odds = Number(body.odds)
  const stake = Number(body.stake)
  const line = opp ? opp.display_line : body.line === '' || body.line == null ? null : Number(body.line)
  if (!Number.isInteger(gameId)) return c.json({ error: 'game_id 必填且須為整數' }, 400)
  if (!VALID_MARKETS.includes(market)) return c.json({ error: `market 須為: ${VALID_MARKETS.join(', ')}` }, 400)
  if (!VALID_SELECTIONS.includes(selection) || (selection === 'draw' && market !== 'h1_ml'))
    return c.json({ error: `selection 須為: ${VALID_SELECTIONS.join(', ')}（draw 只用於上半場三向）` }, 400)
  if (!(Number.isFinite(odds) && odds > 1)) return c.json({ error: 'odds 須為大於 1 的十進位賠率（實際下注賠率）' }, 400)
  if (!(Number.isFinite(stake) && stake > 0)) return c.json({ error: 'stake 須大於 0' }, 400)
  if (line != null && !Number.isFinite(Number(line))) return c.json({ error: 'line 須為數字' }, 400)
  const game = await q.getGameById(db, gameId)
  if (!game) return c.json({ error: '找不到該場比賽' }, 404)

  const now = nowIso()
  let placedAt = now
  if (body.placed_at) {
    const t = new Date(String(body.placed_at))
    if (Number.isNaN(t.getTime())) return c.json({ error: 'placed_at 須為 ISO8601 時間' }, 400)
    if (t.getTime() > Date.now() + 5 * 60000) return c.json({ error: 'placed_at 不可在未來' }, 400)
    placedAt = t.toISOString()
  }
  const source = opp ? opp.source : VALID_SOURCES.includes(body.source) ? body.source : 'twsport'
  const bookmaker = opp ? opp.bookmaker : source === 'twsport' ? 'twsport' : body.bookmaker ? String(body.bookmaker).slice(0, 60) : null
  const bettingDay = tpeDateString(new Date(iso(game.date_utc) as string))

  // ---- server-side 風險檢查（以最新 DB 狀態；只比較 Python 物化的上限） ---- //
  const account = await dq.getAccount(db, user.uid)
  const [snapRow, ledgerMax, eventMax] = await Promise.all([
    dq.getLatestSnapshot(db, user.uid, bettingDay),
    dq.getLedgerMax(db, account ? Number(account.id) : null),
    dq.getUserEventMax(db, user.uid),
  ])
  const snap = snapRow ? shapeSnapshot(snapRow) : null
  const risk = riskStateOf(snap, { accountVersion: account ? num(account.risk_state_version) : null, ledgerMax, eventMax,
    now: Date.now() })
  let linked: any = null
  if (opp && snap) {
    const current = (await dq.getOpportunities(db, snap.id)).map(shapeOpportunity)
    linked = current.find((o) => o.scope_kind === 'taiwan_primary' && o.game_id === opp.game_id && o.source === opp.source
      && o.bookmaker === opp.bookmaker && o.market === opp.market && o.side === opp.side) ?? null
  }
  const check = evaluateBet({ stake, hasAccount: !!account, snapshot: snap, riskState: risk, gameId,
    linked, requestedOpportunity: !!opp, placedAfterTipoff: new Date(placedAt).getTime() >= new Date(iso(game.date_utc) as string).getTime() })
  const confirmed = body.confirm_override === true
  const reason = typeof body.override_reason === 'string' ? body.override_reason.trim().slice(0, 500) : ''
  if (check.requires_confirmation && !confirmed) {
    return c.json({ error: 'confirmation_required', message: confirmationMessage(check.checks), checks: check.checks,
      warnings: check.warnings, compliance_if_confirmed: check.compliance, requires_reason: check.requires_reason,
      limits: check.limits, risk_state: risk }, 409)
  }
  if (check.requires_reason && !reason) {
    return c.json({ error: 'override_reason_required', message: '超出 risk-v1 / decision-v1 額度時必須填寫原因', checks: check.checks }, 400)
  }

  const row: Record<string, unknown> = {
    user_id: user.uid, game_id: gameId, market, selection, line, odds, stake, result: 'pending',
    note: body.note ? String(body.note).slice(0, 500) : null, placed_at: placedAt,
    account_id: account ? Number(account.id) : null, betting_day: bettingDay, source, bookmaker,
    market_type: opp?.market_type ?? null, period: opp?.period ?? null, outcome_set: opp?.outcome_set ?? null,
    model_target: opp?.model_target ?? null, model_threshold: opp?.model_threshold ?? null,
    comparator: opp?.comparator ?? null, settlement_rule: opp?.settlement_rule ?? null,
    origin: opp ? (opp.paper_decision_id ? 'paper_decision' : 'platform_opportunity') : 'manual_unlinked',
    strategy_compliance: check.compliance,
    reference_odds_snapshot_id: opp?.odds_snapshot_id ?? null,
    reference_decimal_odds: opp?.decimal_odds ?? null,
    reference_pricing_snapshot_id: opp?.pricing_snapshot_id ?? null,
    reference_sizing_snapshot_id: opp?.sizing_snapshot_id ?? null,
    decision_snapshot_id: opp ? opp.snapshot_id : null,
    decision_opportunity_id: opp ? opp.id : null,
    paper_decision_id: opp?.paper_decision_id ?? null,
    reference_context: opp ? JSON.stringify({
      recorded_from_status: opp.decision_status, current_status: linked?.decision_status ?? null,
      reference_decimal_odds: opp.decimal_odds, actual_decimal_odds: odds, odds_fetched_at: opp.odds_fetched_at,
      odds_last_seen_at: opp.odds_last_seen_at, ev_per_unit: opp.ev_per_unit, model_prob: opp.model_prob,
      fair_no_vig_prob: opp.fair_no_vig_prob, theoretical_final_fraction: opp.theoretical_final_fraction,
      user_adjusted_fraction: linked?.user_adjusted_fraction ?? opp.user_adjusted_fraction,
      max_additional_stake_amount: linked?.max_additional_stake_amount ?? opp.max_additional_stake_amount,
      evidence_label: opp.evidence_label, artifact_version: opp.artifact_version,
    }) : null,
    bankroll_day_snapshot_id: snap?.bankroll_day_snapshot_id ?? null,
    risk_check: JSON.stringify({ ...check, risk_state: risk.status, risk_reasons: risk.reasons,
      snapshot_id: snap?.id ?? null, evaluated_at: now, confirmed }),
    override_reason: confirmed ? reason || null : null,
    override_confirmed_at: confirmed ? now : null,
    client_request_id: requestId, recorded_at: now, record_status: 'active', supersedes_bet_id: null,
  }
  const steps = [] as { sql: string; params?: unknown[] }[]
  if (account) {
    const expected = Number(account.risk_state_version)
    steps.push(...dq.claimSteps(Number(account.id), expected, expected + 1, 'bet_recorded', requestId, now))
  }
  steps.push(dq.betInsertStep(row))
  steps.push(dq.betEventStep(user.uid, requestId, 'recorded', { compliance: check.compliance, checks: check.checks,
    confirmed, origin: row.origin }, now))
  try {
    await db.atomic(steps)
  } catch (e) {
    const again = await dq.getBetByRequest(db, user.uid, requestId)
    if (again) return c.json({ ok: true, id: Number(again.id), replay: true, bet: shapeBet(again) }, 200)
    console.warn('bet record conflict', (e as Error)?.message)
    return c.json({ error: 'risk_state_changed', message: '另一個請求剛更新了下注或 bankroll（風險額度需要重新計算），請重新整理後再確認。' }, 409)
  }
  const saved = await dq.getBetByRequest(db, user.uid, requestId)
  return c.json({ ok: true, id: Number(saved?.id), bet: saved ? shapeBet(saved) : null, risk_check: check,
    risk_state: 'recalculation_pending' }, 201)
})

function confirmationMessage(checks: string[]) {
  if (checks.some((x) => x.startsWith('exceeds_risk_v1'))) return 'Exceeds risk-v1 limit：需要明確確認並填寫原因（記錄為 user_override，不視為策略合規）'
  if (checks.includes('already_recorded_no_top_up')) return '此機會已記錄實際下注；decision-v1 不補單（需要明確確認並填寫原因）'
  if (checks.includes('above_decision_v1_proposed_capacity')) return '高於 decision-v1 建議的新增額度（需要明確確認並填寫原因）'
  if (checks.some((x) => x.startsWith('opportunity_not_qualified'))) return '此機會目前不是 qualified（記錄為 outside_model）'
  return '目前無法驗證風險額度（bankroll 未設定或重新計算中）：確認後記錄為 missing_context'
}

async function legacyCreate(c: any, db: Db, uid: number, body: any) {
  const gameId = Number(body.game_id)
  const market = String(body.market ?? '')
  const selection = String(body.selection ?? '')
  const odds = Number(body.odds)
  const stake = Number(body.stake)
  const line = body.line === '' || body.line == null ? null : Number(body.line)
  if (!Number.isInteger(gameId)) return c.json({ error: 'game_id 必填且須為整數' }, 400)
  if (!VALID_MARKETS.includes(market)) return c.json({ error: `market 須為: ${VALID_MARKETS.join(', ')}` }, 400)
  if (!['home', 'away', 'over', 'under'].includes(selection)) return c.json({ error: 'selection 須為: home, away, over, under' }, 400)
  if (!(odds > 1)) return c.json({ error: 'odds 須為大於 1 的十進位賠率（台彩實際賠率）' }, 400)
  if (!(stake > 0)) return c.json({ error: 'stake 須大於 0' }, 400)
  if (!(await q.getGameById(db, gameId))) return c.json({ error: '找不到該場比賽' }, 404)
  const sql = `INSERT INTO bets (user_id, game_id, market, selection, line, odds, stake, result, note)
     VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)` + (db.driver === 'postgres' ? ' RETURNING id' : '')
  const res = await db.run(sql, [uid, gameId, market, selection, line, odds, stake, body.note ? String(body.note) : null])
  return c.json({ ok: true, id: res.lastInsertId, d5_schema: false }, 201)
}

/** PATCH /api/bets/:id — 手動結算（稽核；Python settle-v1 只處理有 canonical 身分的 pending 注單） */
r.patch('/bets/:id', async (c) => {
  const user = await requireUser(c)
  if (!user) return c.json({ error: 'unauthorized' }, 401)
  const db = await getDb(c.env)
  const id = Number(c.req.param('id'))
  const body = await c.req.json().catch(() => ({}))
  const result = String((body as any).result ?? '')
  if (!['pending', 'win', 'lose', 'push', 'void'].includes(result))
    return c.json({ error: 'result 須為 pending/win/lose/push/void' }, 400)
  const bet = await dq.getBetForUser(db, id, user.uid)
  if (!bet) return c.json({ error: '找不到該筆紀錄' }, 404)
  // 舊版返還金額欄位（payout = 返還總額）；bankroll 損益由 Python ledger 以 settle-v1 同一公式入帳
  const payout =
    result === 'win' ? Number(bet.stake) * Number(bet.odds)
      : result === 'push' || result === 'void' ? Number(bet.stake)
        : result === 'lose' ? 0 : null
  if (!(await dq.hasD5Schema(db))) {
    await db.run('UPDATE bets SET result = ?, payout = ? WHERE id = ? AND user_id = ?', [result, payout, id, user.uid])
    return c.json({ ok: true, id, result, payout })
  }
  if ((bet.record_status ?? 'active') !== 'active') return c.json({ error: `此紀錄已 ${bet.record_status}，不可再結算` }, 409)
  const now = nowIso()
  await db.atomic([
    {
      sql: `UPDATE bets SET result = ?, payout = ?, settled_at = ?, settlement_source = 'manual', settlement_reason = ?
             WHERE id = ? AND user_id = ? AND record_status = 'active'`,
      params: [result, payout, result === 'pending' ? null : now, 'manual', id, user.uid],
    },
    dq.betEventByIdStep(id, user.uid, 'settled_manual', { from: bet.result, to: result }, now),
  ])
  return c.json({ ok: true, id, result, payout, settlement_source: 'manual' })
})

async function voidBet(c: any, reasonText: string | null, kind: 'voided' | 'deleted') {
  const user = await requireUser(c)
  if (!user) return c.json({ error: 'unauthorized' }, 401)
  const db = await getDb(c.env)
  const id = Number(c.req.param('id'))
  if (!Number.isInteger(id)) return c.json({ error: 'invalid bet id' }, 400)
  if (!(await dq.hasD5Schema(db))) {
    if (kind !== 'deleted') return c.json({ error: 'migration 0008 未套用' }, 409)
    await db.run('DELETE FROM bets WHERE id = ? AND user_id = ?', [id, user.uid])
    return c.json({ ok: true, id })
  }
  const bet = await dq.getBetForUser(db, id, user.uid)
  if (!bet) return c.json({ error: '找不到該筆紀錄' }, 404)
  if ((bet.record_status ?? 'active') !== 'active') return c.json({ ok: true, id, record_status: bet.record_status, replay: true })
  const reason = (reasonText ?? '').trim().slice(0, 500) || (kind === 'deleted' ? 'deleted_by_user' : '')
  if (!reason) return c.json({ error: 'void 需要原因（reason）' }, 400)
  const now = nowIso()
  const account = await dq.getAccount(db, user.uid)
  const steps = [] as { sql: string; params?: unknown[] }[]
  if (account) {
    const expected = Number(account.risk_state_version)
    steps.push(...dq.claimSteps(Number(account.id), expected, expected + 1, 'bet_voided', `void:${id}`, now))
  }
  steps.push({
    sql: `UPDATE bets SET record_status = 'voided', voided_at = ?, void_reason = ?
           WHERE id = ? AND user_id = ? AND record_status = 'active'`,
    params: [now, reason, id, user.uid],
  })
  steps.push(dq.betEventByIdStep(id, user.uid, 'voided', { reason }, now))
  try {
    await db.atomic(steps)
  } catch {
    return c.json({ error: 'risk_state_changed', message: '另一個請求剛更新了紀錄，請重新整理後再試。' }, 409)
  }
  return c.json({ ok: true, id, record_status: 'voided', note: '紀錄已作廢（保留稽核軌跡，不會刪除）' })
}

r.post('/bets/:id/void', async (c) => {
  const body = await c.req.json().catch(() => ({}))
  return voidBet(c, (body as any).reason ?? null, 'voided')
})

r.delete('/bets/:id', async (c) => voidBet(c, 'deleted_by_user', 'deleted'))

/** POST /api/bets/:id/correction — 輸入錯誤更正：新列 supersedes 舊列；舊列執行欄位不變 */
r.post('/bets/:id/correction', async (c) => {
  const user = await requireUser(c)
  if (!user) return unauthorized(c)
  const db = await getDb(c.env)
  if (!(await dq.hasD5Schema(db))) return c.json({ error: 'migration 0008 未套用' }, 409)
  const id = Number(c.req.param('id'))
  const body: any = await c.req.json().catch(() => ({}))
  const old = await dq.getBetForUser(db, id, user.uid)
  if (!old) return c.json({ error: '找不到該筆紀錄' }, 404)
  if ((old.record_status ?? 'active') !== 'active') return c.json({ error: `此紀錄已 ${old.record_status}` }, 409)
  const reason = typeof body.reason === 'string' ? body.reason.trim().slice(0, 500) : ''
  if (!reason) return c.json({ error: '更正需要原因（reason）' }, 400)
  const odds = body.odds != null ? Number(body.odds) : Number(old.odds)
  const stake = body.stake != null ? Number(body.stake) : Number(old.stake)
  const line = body.line !== undefined ? (body.line === '' || body.line == null ? null : Number(body.line)) : num(old.line)
  const selection = body.selection ? String(body.selection) : old.selection
  if (!(Number.isFinite(odds) && odds > 1) || !(Number.isFinite(stake) && stake > 0))
    return c.json({ error: '更正後的 odds 須 > 1、stake 須 > 0' }, 400)
  if (!VALID_SELECTIONS.includes(selection)) return c.json({ error: 'selection 不合法' }, 400)
  let placedAt = iso(old.placed_at)
  if (body.placed_at) {
    const t = new Date(String(body.placed_at))
    if (Number.isNaN(t.getTime())) return c.json({ error: 'placed_at 須為 ISO8601 時間' }, 400)
    placedAt = t.toISOString()
  }
  const now = nowIso()
  const requestId = crypto.randomUUID()
  const keep = (k: string) => (old[k] === undefined ? null : old[k])
  const canonicalChanged = selection !== old.selection || line !== num(old.line)
  const row: Record<string, unknown> = {}
  for (const k of dq.BET_INSERT_COLUMNS) row[k] = keep(k)
  Object.assign(row, {
    odds, stake, line, selection, placed_at: placedAt, result: 'pending', client_request_id: requestId, recorded_at: now,
    record_status: 'active', supersedes_bet_id: id, betting_day: day(old.betting_day),
    note: body.note !== undefined ? (body.note ? String(body.note).slice(0, 500) : null) : old.note,
    // 方向 / 盤線改變 → canonical 結算身分不再對應原機會（改為手動結算）
    model_target: canonicalChanged ? null : keep('model_target'),
    model_threshold: canonicalChanged ? null : keep('model_threshold'),
    comparator: canonicalChanged ? null : keep('comparator'),
    settlement_rule: canonicalChanged ? null : keep('settlement_rule'),
    reference_context: typeof old.reference_context === 'object' && old.reference_context ? JSON.stringify(old.reference_context) : keep('reference_context'),
    risk_check: JSON.stringify({ skipped: 'correction', corrected_from: id, reason, evaluated_at: now }),
  })
  const account = await dq.getAccount(db, user.uid)
  const steps = [] as { sql: string; params?: unknown[] }[]
  if (account) {
    const expected = Number(account.risk_state_version)
    steps.push(...dq.claimSteps(Number(account.id), expected, expected + 1, 'bet_corrected', requestId, now))
  }
  steps.push(dq.betInsertStep(row))
  steps.push({
    sql: `UPDATE bets SET record_status = 'superseded', voided_at = ?, void_reason = ?,
                 superseded_by_bet_id = (SELECT id FROM bets WHERE user_id = ? AND client_request_id = ?)
           WHERE id = ? AND user_id = ? AND record_status = 'active'`,
    params: [now, `corrected:${reason}`, user.uid, requestId, id, user.uid],
  })
  steps.push(dq.betEventByIdStep(id, user.uid, 'superseded', { reason, request_id: requestId }, now))
  steps.push(dq.betEventStep(user.uid, requestId, 'correction_recorded', { supersedes_bet_id: id, reason }, now))
  try {
    await db.atomic(steps)
  } catch (e) {
    console.warn('bet correction conflict', (e as Error)?.message)
    return c.json({ error: 'risk_state_changed', message: '另一個請求剛更新了紀錄，請重新整理後再試。' }, 409)
  }
  const saved = await dq.getBetByRequest(db, user.uid, requestId)
  return c.json({ ok: true, id: Number(saved?.id), supersedes_bet_id: id, bet: saved ? shapeBet(saved) : null }, 201)
})

/* ------------------------------------------------------------------ */
/* /api/bankroll                                                       */
/* ------------------------------------------------------------------ */

r.get('/bankroll', async (c) => {
  const user = await requireUser(c)
  if (!user) return unauthorized(c)
  const db = await getDb(c.env)
  if (!(await dq.hasD5Schema(db))) return c.json({ status: 'unavailable', reason: 'migration_0008_not_applied' })
  const account = await dq.getAccount(db, user.uid)
  if (!account) return c.json({ status: 'not_configured', configured: false, account: null, ledger: [], day_snapshots: [] })
  const accountId = Number(account.id)
  const [ledger, days, snapRow, ledgerMax, eventMax] = await Promise.all([
    dq.getLedger(db, accountId), dq.getDaySnapshots(db, accountId), dq.getLatestSnapshotAnyDay(db, user.uid),
    dq.getLedgerMax(db, accountId), dq.getUserEventMax(db, user.uid),
  ])
  const snap = snapRow ? shapeSnapshot(snapRow) : null
  const risk = riskStateOf(snap, { accountVersion: num(account.risk_state_version), ledgerMax, eventMax, now: Date.now() })
  return c.json({
    status: 'configured',
    configured: true,
    account: { id: accountId, currency: account.currency, label: account.label, created_at: iso(account.created_at),
      risk_state_version: num(account.risk_state_version) },
    summary: snap?.bankroll ?? null,
    summary_betting_day: snap?.betting_day ?? null,
    summary_as_of: snap?.last_confirmed_at ?? null,
    summary_state: risk,
    actual_performance: snap?.actual_performance ?? null,
    evidence: snap?.evidence ?? null,
    ledger: ledger.map((e) => ({ ...e, amount: num(e.amount), recorded_at: iso(e.recorded_at) })),
    day_snapshots: days.map((d) => ({ ...d, betting_day: day(d.betting_day), day_start_bankroll: num(d.day_start_bankroll),
      ledger_balance: num(d.ledger_balance), open_stake_excluded: num(d.open_stake_excluded),
      basis_as_of: iso(d.basis_as_of), created_at: iso(d.created_at) })),
    note: '手動維護的 strategy bankroll（不連銀行 / bookmaker）。所有異動都是 append-only ledger；金額合計由 Python 計算。',
  })
})

const USER_ENTRY_TYPES = ['initial_funding', 'deposit', 'withdrawal', 'adjustment', 'reversal']

r.post('/bankroll/entries', async (c) => {
  const user = await requireUser(c)
  if (!user) return unauthorized(c)
  const db = await getDb(c.env)
  if (!(await dq.hasD5Schema(db))) return c.json({ error: 'migration 0008 未套用' }, 409)
  const body: any = await c.req.json().catch(() => ({}))
  const type = String(body.entry_type ?? '')
  if (!USER_ENTRY_TYPES.includes(type)) return c.json({ error: `entry_type 須為: ${USER_ENTRY_TYPES.join(', ')}` }, 400)
  const reason = typeof body.reason === 'string' ? body.reason.trim().slice(0, 500) : ''
  const amount = body.amount == null || body.amount === '' ? null : Number(body.amount)
  if (type === 'reversal') {
    if (amount != null) return c.json({ error: 'reversal 不填金額（自動沖銷原筆）' }, 400)
    if (!reason) return c.json({ error: 'reversal 需要原因' }, 400)
  } else if (amount == null || !Number.isFinite(amount)) {
    return c.json({ error: 'amount 須為數字' }, 400)
  } else if (type === 'adjustment') {
    if (amount === 0) return c.json({ error: 'adjustment 不得為 0' }, 400)
    if (!reason) return c.json({ error: 'adjustment 需要原因' }, 400)
  } else if (!(amount > 0)) {
    return c.json({ error: `${type} 金額須 > 0（方向由類型決定，不接受負數）` }, 400)
  }
  const requestId = typeof body.client_request_id === 'string' && /^[\w:-]{8,100}$/.test(body.client_request_id)
    ? body.client_request_id : crypto.randomUUID()
  const now = nowIso()
  let account = await dq.getAccount(db, user.uid)
  if (account) {
    const dup = await dq.getLedgerByRequest(db, Number(account.id), requestId)
    if (dup) return c.json({ ok: true, replay: true, entry: dup })
  }

  if (!account) {
    if (type !== 'initial_funding') return c.json({ error: '請先建立 bankroll（initial_funding）' }, 409)
    const currency = typeof body.currency === 'string' && /^[A-Z]{3}$/.test(body.currency) ? body.currency : 'TWD'
    try {
      await db.atomic([
        { sql: `INSERT INTO bankroll_accounts (user_id, currency, label, created_at, risk_state_version, risk_state_changed_at)
                VALUES (?, ?, ?, ?, 1, ?)`, params: [user.uid, currency, body.label ? String(body.label).slice(0, 60) : null, now, now] },
        { sql: `INSERT INTO risk_state_claims (account_id, version, kind, request_id, created_at)
                VALUES ((SELECT id FROM bankroll_accounts WHERE user_id = ?), 1, 'ledger_entry', ?, ?)`, params: [user.uid, requestId, now] },
        { sql: `INSERT INTO bankroll_ledger (account_id, entry_type, amount, reason, recorded_by, client_request_id, recorded_at)
                VALUES ((SELECT id FROM bankroll_accounts WHERE user_id = ?), 'initial_funding', ?, ?, 'user', ?, ?)`,
          params: [user.uid, amount, reason || null, requestId, now] },
      ])
    } catch {
      return c.json({ error: 'bankroll 已存在或同時建立中，請重新整理' }, 409)
    }
    account = await dq.getAccount(db, user.uid)
    return c.json({ ok: true, account_id: Number(account?.id), entry_type: type, recalculation: 'pending' }, 201)
  }

  const accountId = Number(account.id)
  let reverses: number | null = null
  if (type === 'reversal') {
    reverses = Number(body.reverses_entry_id)
    const target = Number.isInteger(reverses)
      ? await db.one<any>('SELECT * FROM bankroll_ledger WHERE id = ? AND account_id = ?', [reverses, accountId]) : null
    if (!target) return c.json({ error: '找不到要沖銷的 ledger entry' }, 404)
    if (target.recorded_by !== 'user' || target.entry_type === 'reversal')
      return c.json({ error: '只能沖銷使用者輸入的入金 / 出金 / 調整（結算損益由 pipeline 管理）' }, 400)
  }
  if (type === 'withdrawal') {
    // 只與 Python 物化的 available_bankroll 比較；物化結果過期 → 不猜，請稍後再試
    const ledgerMax = await dq.getLedgerMax(db, accountId)
    const snapRow = await dq.getLatestSnapshotAnyDay(db, user.uid)
    const snap = snapRow ? shapeSnapshot(snapRow) : null
    const risk = riskStateOf(snap, { accountVersion: num(account.risk_state_version), ledgerMax,
      eventMax: await dq.getUserEventMax(db, user.uid), now: Date.now() })
    const available = num(snap?.bankroll?.available_bankroll)
    if (!snap || risk.status === 'recalculation_pending' || available == null)
      return c.json({ error: 'recalculation_pending', message: 'bankroll 尚在重新計算，請約一分鐘後再試（不以過期餘額判斷出金）' }, 409)
    if ((amount as number) > available)
      return c.json({ error: 'exceeds_available_bankroll', message: '出金金額大於可用 bankroll（已扣除未結算 stake）', available_bankroll: available }, 409)
  }
  const expected = Number(account.risk_state_version)
  try {
    await db.atomic([
      ...dq.claimSteps(accountId, expected, expected + 1, 'ledger_entry', requestId, now),
      { sql: `INSERT INTO bankroll_ledger (account_id, entry_type, amount, reverses_entry_id, reason, recorded_by, client_request_id, recorded_at)
              VALUES (?, ?, ?, ?, ?, 'user', ?, ?)`,
        params: [accountId, type, type === 'reversal' ? null : amount, reverses, reason || null, requestId, now] },
    ])
  } catch (e) {
    const dup = await dq.getLedgerByRequest(db, accountId, requestId)
    if (dup) return c.json({ ok: true, replay: true, entry: dup })
    const msg = (e as Error)?.message ?? ''
    if (type === 'initial_funding') return c.json({ error: 'initial_funding 只能一次（之後請用 deposit / adjustment）' }, 409)
    if (type === 'reversal' && /unique|UNIQUE/.test(msg)) return c.json({ error: '該筆已被沖銷' }, 409)
    return c.json({ error: 'risk_state_changed', message: '另一個請求剛更新了 bankroll，請重新整理後再試。' }, 409)
  }
  const saved = await dq.getLedgerByRequest(db, accountId, requestId)
  return c.json({ ok: true, entry: saved ? { ...saved, amount: num(saved.amount), recorded_at: iso(saved.recorded_at) } : null,
    recalculation: 'pending' }, 201)
})

export default r
