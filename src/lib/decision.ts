/**
 * Phase D.5 決策中心的序列化 / 狀態判斷（只讀、不計算）
 * ------------------------------------------------------------
 * EV / Kelly / 同場與單日上限 / actual-bet exposure / 剩餘額度 / 組合縮放 / bankroll 金額
 * **全部**在 Python（pipeline/core/decision/）計算並物化到 decision_snapshots / decision_opportunities。
 * 這裡只做：欄位命名、JSON 解析、分組、物化結果是否仍有效（版本號比較）、
 * 以及寫入前把使用者輸入的 stake 與 Python 物化的上限「比較」（不做任何加減乘除）。
 *
 * 三個概念分開：
 *   model opportunity   decision_opportunities（台彩 primary；國際盤只作 diagnostic）
 *   paper decision      games[].paper_decision（D.4 execution-v1 T-60；只對照，不是實際下注）
 *   actual bet          bets（使用者在外部下注後手動記錄；平台從不自動下注）
 */

export const DECISION_VERSION = 'decision-v1'
export const MATERIALIZATION_STALE_MS = 10 * 60 * 1000

export const STATUS_TEXT: Record<string, { zh: string; en: string }> = {
  qualified: { zh: '符合條件的機會', en: 'Qualified opportunity' },
  already_recorded: { zh: '已記錄實際下注', en: 'Actual bet logged' },
  no_positive_ev: { zh: '無正 EV', en: 'No positive EV' },
  stale_odds: { zh: '盤口過舊', en: 'Stale odds' },
  no_prediction: { zh: '尚無預測', en: 'No prediction' },
  no_odds: { zh: '無可下注盤口', en: 'No open odds' },
  unsupported: { zh: '不支援的玩法', en: 'Unsupported market' },
  risk_cap_reached: { zh: '風險額度已用完', en: 'Risk cap reached' },
  actual_exposure_over_limit: { zh: '實際 exposure 超過 risk-v1', en: 'Actual exposure over limit' },
  bankroll_unavailable: { zh: '尚未設定 / 無法凍結 bankroll', en: 'Bankroll unavailable' },
  data_incomplete: { zh: '資料不完整', en: 'Data incomplete' },
}

export const GROUP_TEXT: Record<string, { zh: string; en: string }> = {
  actionable: { zh: '可操作', en: 'ACTIONABLE' },
  review: { zh: '需檢視', en: 'REVIEW' },
  recorded: { zh: '已記錄', en: 'RECORDED' },
  blocked: { zh: '受阻', en: 'BLOCKED' },
  inactive: { zh: '無機會', en: 'NO EDGE' },
  diagnostic: { zh: '國際盤診斷', en: 'DIAGNOSTIC' },
}

export const TAIWAN_ODDS_TEXT: Record<string, string> = {
  available: '台彩盤口可用',
  stale: '台彩盤口過舊（超過 2 × 輪詢間隔未再確認）',
  market_closed: '台彩盤口暫停 / 關閉',
  not_yet_published: '台彩盤口尚未取得（可能尚未開盤）',
  ingestion_unavailable: '台彩自動擷取不可用（Cloudflare 阻擋）— 需匯入 HAR',
  not_configured: '台彩擷取尚未設定 / 從未執行',
  outside_window: '比賽超出 48 小時定價視窗',
  game_started: '比賽已開始 / 已結束',
}

export function num(v: any): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

export function json(v: any, fallback: any) {
  if (v == null) return fallback
  if (typeof v === 'object') return v
  try {
    return JSON.parse(v)
  } catch {
    return fallback
  }
}

export function day(v: any): string | null {
  if (v == null) return null
  if (v instanceof Date) return v.toISOString().slice(0, 10)
  return String(v).slice(0, 10)
}

export function iso(v: any): string | null {
  if (v == null) return null
  if (v instanceof Date) return v.toISOString()
  return String(v)
}

const NUM_FIELDS = [
  'line', 'display_line', 'model_threshold', 'decimal_odds', 'raw_implied_prob', 'fair_no_vig_prob', 'market_overround',
  'model_prob', 'push_prob', 'loss_prob', 'edge_vs_fair', 'ev_per_unit', 'full_kelly_fraction',
  'fractional_kelly_fraction', 'single_bet_capped_fraction', 'theoretical_final_fraction', 'actual_game_exposure',
  'remaining_game_fraction', 'remaining_day_fraction', 'game_scale_factor', 'day_scale_factor',
  'user_adjusted_fraction', 'day_start_bankroll', 'max_additional_stake_amount', 'suggested_stake_amount',
  'linked_actual_fraction',
]
const STR_FIELDS = [
  'scope_kind', 'evidence_label', 'source', 'bookmaker', 'market', 'market_type', 'period', 'outcome_set', 'side',
  'model_target', 'comparator', 'settlement_rule', 'artifact_version', 'pricing_version', 'd3_qualification_status',
  'decision_status', 'status_group',
]

export function shapeOpportunity(r: any) {
  const o: any = {
    id: Number(r.id),
    snapshot_id: Number(r.snapshot_id),
    game_id: Number(r.game_id),
    odds_snapshot_id: r.odds_snapshot_id == null ? null : Number(r.odds_snapshot_id),
    pricing_snapshot_id: r.pricing_snapshot_id == null ? null : Number(r.pricing_snapshot_id),
    sizing_snapshot_id: r.sizing_snapshot_id == null ? null : Number(r.sizing_snapshot_id),
    prediction_id: r.prediction_id == null ? null : Number(r.prediction_id),
    paper_decision_id: r.paper_decision_id == null ? null : Number(r.paper_decision_id),
    odds_fetched_at: iso(r.odds_fetched_at),
    odds_last_seen_at: iso(r.odds_last_seen_at),
    display_rank: num(r.display_rank),
    linked_bet_ids: json(r.linked_bet_ids, []),
    reasons: json(r.reasons, []),
    warnings: json(r.warnings, []),
  }
  for (const k of NUM_FIELDS) o[k] = num(r[k])
  for (const k of STR_FIELDS) o[k] = r[k] ?? null
  o.status_text = STATUS_TEXT[o.decision_status] ?? { zh: o.decision_status, en: o.decision_status }
  o.group_text = GROUP_TEXT[o.status_group] ?? { zh: o.status_group, en: o.status_group }
  return o
}

export function shapeSnapshot(r: any) {
  return {
    id: Number(r.id),
    betting_day: day(r.betting_day),
    as_of: iso(r.as_of),
    last_confirmed_at: iso(r.last_confirmed_at),
    decision_version: r.decision_version,
    risk_policy_version: r.risk_policy_version,
    execution_policy_version: r.execution_policy_version,
    scope: r.scope,
    status: r.status,
    risk_state_version: num(r.risk_state_version),
    ledger_watermark: num(r.ledger_watermark),
    bet_event_watermark: num(r.bet_event_watermark),
    bankroll_day_snapshot_id: r.bankroll_day_snapshot_id == null ? null : Number(r.bankroll_day_snapshot_id),
    summary: json(r.summary, {}),
    bankroll: json(r.bankroll, {}),
    actual_exposure: json(r.actual_exposure, {}),
    risk_limits: json(r.risk_limits, {}),
    games: json(r.games, []),
    evidence: json(r.evidence, {}),
    actual_performance: json(r.actual_performance, {}),
    warnings: json(r.warnings, []),
  }
}

export type LiveState = { accountVersion: number | null; ledgerMax: number; eventMax: number; now: number }

/**
 * 物化結果是否仍代表目前的 bets / bankroll：只比較版本號（Python 物化時記下的 vs 目前 DB）。
 * 不一致 → risk_recalculation_pending：UI 不得把舊的剩餘額度當成有效。
 */
export function riskStateOf(snap: ReturnType<typeof shapeSnapshot> | null, live: LiveState) {
  if (!snap) return { status: 'not_materialized', capacity_valid: false, reasons: ['decision_board_not_materialized'] }
  const reasons: string[] = []
  if (live.accountVersion != null && snap.risk_state_version !== live.accountVersion) reasons.push('bets_or_bankroll_changed')
  if (live.accountVersion == null && snap.risk_state_version != null) reasons.push('bankroll_account_changed')
  if ((snap.ledger_watermark ?? 0) !== live.ledgerMax) reasons.push('ledger_changed')
  if ((snap.bet_event_watermark ?? 0) !== live.eventMax) reasons.push('bet_records_changed')
  const confirmed = snap.last_confirmed_at ? new Date(snap.last_confirmed_at).getTime() : 0
  const stale = live.now > confirmed + MATERIALIZATION_STALE_MS
  if (stale) reasons.push('materialization_stale')
  const pending = reasons.some((r) => r !== 'materialization_stale')
  return {
    status: pending ? 'recalculation_pending' : stale ? 'stale' : 'current',
    capacity_valid: reasons.length === 0,
    materialization_stale: stale,
    reasons,
  }
}

/* ------------------------------------------------------------------ */
/* System readiness（整合既有 data_sources 心跳）                         */
/* ------------------------------------------------------------------ */

export const READINESS_COMPONENTS: { key: string; label: string; sources: string[] }[] = [
  { key: 'schedule', label: '賽程 / 比分', sources: ['nba_cdn', 'nba_api', 'espn'] },
  { key: 'predictions', label: '預測（ml-v2.0）', sources: ['model_predict'] },
  { key: 'injuries', label: '傷病報告', sources: ['nbainjuries'] },
  { key: 'taiwan_odds', label: '台彩盤口', sources: ['twsport'] },
  { key: 'odds_api', label: '國際盤（Odds API，診斷）', sources: ['oddsapi'] },
  { key: 'pricing', label: '定價（D.2）', sources: ['market_pricing'] },
  { key: 'sizing', label: '理論注碼（D.3）', sources: ['bet_sizing'] },
  { key: 'paper_strategy', label: 'Paper strategy（D.4）', sources: ['paper_strategy'] },
  { key: 'decision_board', label: '決策物化（D.5）', sources: ['decision_board'] },
]

function sourceStatus(r: any, now: number) {
  if (!r) return 'not_configured'
  if (r.last_status === 'error') return 'error'
  if (r.last_outcome === 'blocked') return 'blocked'
  if (r.last_outcome === 'not_configured' || r.last_outcome === 'auth_failed') return 'not_configured'
  const last = r.last_success_at ? new Date(r.last_success_at).getTime() : null
  const expectedMs = r.expected_interval_min != null ? Number(r.expected_interval_min) * 60000 : null
  if (last != null && expectedMs != null && now > last + expectedMs + expectedMs) return 'stale'
  if (last == null && r.last_status === 'ok') return 'waiting'
  if (r.last_status === 'warn') return 'waiting'
  if (r.last_status === 'ok') return 'healthy'
  return 'waiting'
}

export function readiness(rows: any[], now: number, extra: { bankroll: string; actualExposure: string }) {
  const by = new Map<string, any>()
  for (const r of rows) by.set(r.source_key, r)
  const components = READINESS_COMPONENTS.map((c) => {
    const cands = c.sources.map((k) => by.get(k)).filter(Boolean)
    const best = cands.sort((a, b) => String(b.last_success_at ?? '').localeCompare(String(a.last_success_at ?? '')))[0]
    return {
      key: c.key,
      label: c.label,
      status: sourceStatus(best, now),
      source_key: best?.source_key ?? c.sources[0],
      last_successful_update: iso(best?.last_success_at ?? null),
      last_attempt: iso(best?.last_attempt_at ?? null),
      detail: best?.last_error ?? best?.last_outcome ?? null,
    }
  })
  components.push({ key: 'bankroll', label: 'Bankroll（個人）', status: extra.bankroll, source_key: 'bankroll_accounts',
    last_successful_update: null, last_attempt: null, detail: null })
  components.push({ key: 'actual_exposure', label: 'Actual exposure', status: extra.actualExposure,
    source_key: 'decision_snapshots', last_successful_update: null, last_attempt: null, detail: null })
  const order = ['error', 'blocked', 'stale', 'not_configured', 'waiting', 'healthy']
  const worst = components.map((c) => c.status).sort((a, b) => order.indexOf(a) - order.indexOf(b))[0] ?? 'healthy'
  return { overall: worst === 'healthy' ? 'healthy' : worst === 'waiting' ? 'waiting' : 'degraded', components }
}

/* ------------------------------------------------------------------ */
/* 記錄下注前的 server-side 檢查（只比較 Python 物化的上限，不計算）            */
/* ------------------------------------------------------------------ */

export type BetCheckContext = {
  stake: number
  hasAccount: boolean
  snapshot: ReturnType<typeof shapeSnapshot> | null
  riskState: ReturnType<typeof riskStateOf>
  gameId: number
  linked: any | null           // 同一 game × source × bookmaker × market × side 在「最新」snapshot 的 opportunity（shapeOpportunity）
  requestedOpportunity: boolean
  placedAfterTipoff: boolean
}

export type BetCheck = {
  checks: string[]
  warnings: string[]
  compliance: 'compliant' | 'manual_unlinked' | 'user_override' | 'outside_model' | 'missing_context'
  requires_confirmation: boolean
  requires_reason: boolean
  limits: Record<string, number | null>
}

export function evaluateBet(ctx: BetCheckContext): BetCheck {
  const checks: string[] = []
  const warnings: string[] = []
  let missing = false
  let override = false
  let outside = false
  const exp: any = ctx.snapshot?.actual_exposure ?? {}
  const g = (ctx.snapshot?.games ?? []).find((x: any) => Number(x.game_id) === ctx.gameId)
  const limits = {
    max_bet_amount: num(exp.max_bet_amount),
    remaining_game_amount: num(g?.remaining_game_amount),
    remaining_day_amount: num(exp.remaining_day_amount),
    max_additional_stake_amount: ctx.linked ? num(ctx.linked.max_additional_stake_amount) : null,
  }
  if (!ctx.hasAccount) {
    checks.push('bankroll_not_configured')
    missing = true
  }
  if (!ctx.snapshot) {
    checks.push('decision_board_not_materialized')
    missing = true
  } else if (!ctx.riskState.capacity_valid) {
    checks.push('risk_recalculation_pending')
    missing = true
  }
  if (ctx.snapshot && ctx.riskState.capacity_valid) {
    if (limits.max_bet_amount == null || limits.remaining_day_amount == null) {
      checks.push('day_start_unavailable')
      missing = true
    } else {
      if (ctx.stake > limits.max_bet_amount) { checks.push('exceeds_risk_v1_single_bet_limit'); override = true }
      if (ctx.stake > limits.remaining_day_amount) { checks.push('exceeds_risk_v1_daily_remaining'); override = true }
      if (!g) { checks.push('game_not_in_decision_board'); missing = true }
      else if (limits.remaining_game_amount == null) { checks.push('game_capacity_unavailable'); missing = true }
      else if (ctx.stake > limits.remaining_game_amount) { checks.push('exceeds_risk_v1_game_remaining'); override = true }
    }
    if (ctx.requestedOpportunity) {
      if (!ctx.linked) {
        checks.push('opportunity_not_in_current_board')
        missing = true
      } else if (ctx.linked.decision_status === 'already_recorded') {
        checks.push('already_recorded_no_top_up')
        override = true
      } else if (ctx.linked.decision_status !== 'qualified') {
        checks.push(`opportunity_not_qualified:${ctx.linked.decision_status}`)
        outside = true
      } else if (limits.max_additional_stake_amount != null && ctx.stake > limits.max_additional_stake_amount) {
        checks.push('above_decision_v1_proposed_capacity')
        override = true
      }
    }
  }
  if (ctx.placedAfterTipoff) warnings.push('placed_after_tipoff')
  const compliance = override ? 'user_override' : missing ? 'missing_context' : outside ? 'outside_model'
    : ctx.requestedOpportunity ? 'compliant' : 'manual_unlinked'
  return {
    checks,
    warnings,
    compliance,
    requires_confirmation: override || missing || outside,
    requires_reason: override,
    limits,
  }
}
