/**
 * Phase D.3 理論注碼（sizing）的序列化（只讀、不計算）
 * ------------------------------------------------------------
 * Kelly / fractional Kelly / 單筆上限 / 同場與單日 exposure 縮放的數學**只**在 Python（pipeline/core/sizing/），
 * 結果寫入 bet_sizing_snapshots；這裡只做欄位命名、分組與對照（serialization contract）。
 * 不重算任何 stake、不導出 bankroll 金額、不排序、不推薦（推薦屬 D.5）。
 *
 * 欄位（全部是 bankroll 比例；0.0125 = 1.25%）：
 *   full_kelly_fraction         push-aware full Kelly = EV / (b·(1 − p_push))，b = 賠率 − 1；EV ≤ 0 → 0
 *   fractional_kelly_fraction   × kelly_multiplier（risk-v1：0.25）
 *   single_bet_capped_fraction  min(·, max_bet_fraction)（risk-v1：0.02）
 *   game_scale_factor           同一場全部 eligible stake 合計 > max_game_fraction（0.03）時的等比例縮放
 *   daily_scale_factor          同一 betting day（台灣日期）合計 > max_day_fraction（0.08）時的等比例縮放
 *   final_stake_fraction        risk-adjusted 理論注碼（canonical output）
 */

export const RISK_POLICY_VERSION = 'risk-v1'

export const SIZING_DEFINITIONS = {
  full_kelly_fraction: 'push-aware full Kelly：EV / (b·(1 − p_push))，b = 賠率 − 1；只用 P(win) / P(push) / P(loss) / 實際賠率，不用 edge',
  fractional_kelly_fraction: 'full Kelly × kelly_multiplier',
  single_bet_capped_fraction: 'min(fractional Kelly, max_bet_fraction)',
  game_scale_factor: '同一場（含不同玩法、不同 bookmaker）合計超過 max_game_fraction 時的等比例縮放',
  daily_scale_factor: '同一 betting day（Asia/Taipei 開賽日期）合計超過 max_day_fraction 時的等比例縮放',
  final_stake_fraction: 'risk-adjusted 理論注碼（bankroll 比例）；不是投注建議',
  actionable: '數學上 eligible 且報價新鮮、final_stake_fraction > 0（仍不是推薦）',
}

function num(v: any): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

function bool(v: any): boolean {
  return v === true || v === 1 || v === '1' || v === 't' || v === 'true'
}

function json(v: any, fallback: any) {
  if (v == null) return fallback
  if (typeof v === 'object') return v
  try {
    return JSON.parse(v)
  } catch {
    return fallback
  }
}

function day(v: any): string | null {
  if (v == null) return null
  if (v instanceof Date) return v.toISOString().slice(0, 10)
  return String(v).slice(0, 10)
}

export function shapeSizingRow(r: any) {
  return {
    sizing_id: Number(r.id),
    pricing_id: Number(r.market_pricing_snapshot_id),
    odds_snapshot_id: r.odds_snapshot_id == null ? null : Number(r.odds_snapshot_id),
    prediction_id: r.prediction_id == null ? null : Number(r.prediction_id),
    game_id: Number(r.game_id),
    betting_day: day(r.betting_day),
    analysis_as_of: r.analysis_as_of,
    risk_policy_version: r.risk_policy_version,
    sizing_version: r.sizing_version,
    portfolio_key: r.portfolio_key,
    source: r.source,
    bookmaker: r.bookmaker ?? r.source,
    market: r.market,
    side: r.side,
    line: num(r.line),
    display_line: num(r.display_line),
    decimal_odds: num(r.decimal_odds),
    p_win: num(r.p_win),
    p_push: num(r.p_push),
    p_loss: num(r.p_loss),
    ev_per_unit: num(r.ev_per_unit),
    edge_vs_fair: num(r.edge_vs_fair),
    full_kelly_fraction: num(r.full_kelly_fraction),
    kelly_multiplier: num(r.kelly_multiplier),
    fractional_kelly_fraction: num(r.fractional_kelly_fraction),
    single_bet_capped_fraction: num(r.single_bet_capped_fraction),
    game_scale_factor: num(r.game_scale_factor),
    daily_scale_factor: num(r.daily_scale_factor),
    final_stake_fraction: num(r.final_stake_fraction),
    qualification_status: r.qualification_status,
    mathematically_eligible: bool(r.mathematically_eligible),
    actionable: bool(r.actionable),
    reasons: json(r.reasons, []),
    warnings: json(r.warnings, []),
    quote_age_seconds: num(r.quote_age_seconds),
    last_seen_age_seconds: num(r.last_seen_age_seconds),
    max_quote_age_seconds: num(r.max_quote_age_seconds),
  }
}

function policyOf(r: any) {
  return {
    risk_policy_version: r.risk_policy_version,
    kelly_multiplier: num(r.kelly_multiplier),
    max_bet_fraction: num(r.max_bet_fraction),
    max_game_fraction: num(r.max_game_fraction),
    max_day_fraction: num(r.max_day_fraction),
  }
}

function exposure(r: any, scope: 'game' | 'daily') {
  return {
    before: num(r[`${scope}_exposure_before`]),
    scale_factor: num(r[`${scope}_scale_factor`]),
    after: num(r[`${scope}_exposure_after`]),
  }
}

/**
 * 單場：把 sizing 列掛到 pricing.markets[].outcomes[].sizing，並回傳 odds.sizing 摘要。
 * 沒有 sizing 列的已定價 outcome → sizing = null（not_sized：sizing 尚未對目前定價計算）。
 */
export function attachSizing(pricing: { markets: any[] }, rows: any[] | null) {
  const base = { risk_policy_version: RISK_POLICY_VERSION, definitions: SIZING_DEFINITIONS }
  const outcomes = pricing.markets.flatMap((m: any) => m.outcomes)
  if (rows == null) {
    for (const o of outcomes) o.sizing = null
    return { ...base, status: 'unavailable', policy: null, game_exposure: null, daily_exposure: null }
  }
  const byPricing = new Map<number, any>()
  for (const r of rows) byPricing.set(Number(r.market_pricing_snapshot_id), r)
  let sized = 0
  for (const o of outcomes) {
    const r = o.pricing_id == null ? undefined : byPricing.get(o.pricing_id)
    o.sizing = r ? shapeSizingRow(r) : null
    if (r) sized++
  }
  // 摘要取最新一次 sizing（同一場的列通常同屬一個組合；以 analysis_as_of 最新者為準）
  const r0 = [...rows].sort((a, b) => String(b.analysis_as_of).localeCompare(String(a.analysis_as_of)))[0]
  return {
    ...base,
    status: !outcomes.length ? 'no_pricing' : sized === 0 ? 'not_sized' : sized < outcomes.length ? 'partial' : 'sized',
    policy: r0 ? policyOf(r0) : null,
    betting_day: r0 ? day(r0.betting_day) : null,
    analysis_as_of: r0?.analysis_as_of ?? null,
    game_exposure: r0 ? exposure(r0, 'game') : null,
    daily_exposure: r0 ? exposure(r0, 'daily') : null,
  }
}

/** 單日（betting day）檢視：該日最新組合的全部列與 exposure（只讀） */
export function shapeDaySizing(bettingDay: string, rows: any[] | null) {
  const base = { betting_day: bettingDay, risk_policy_version: RISK_POLICY_VERSION, definitions: SIZING_DEFINITIONS }
  if (rows == null) return { ...base, status: 'unavailable', policy: null, daily_exposure: null, games: [] as any[] }
  if (!rows.length) return { ...base, status: 'not_sized', policy: null, daily_exposure: null, games: [] as any[] }
  const games = new Map<number, any>()
  for (const r of rows) {
    const g = Number(r.game_id)
    if (!games.has(g)) {
      games.set(g, {
        game_id: g,
        date_utc: r.date_utc ?? null,
        home_abbr: r.home_abbr ?? null,
        away_abbr: r.away_abbr ?? null,
        game_exposure: exposure(r, 'game'),
        outcomes: [] as any[],
      })
    }
    games.get(g)!.outcomes.push(shapeSizingRow(r))
  }
  return {
    ...base,
    status: 'sized',
    analysis_as_of: rows[0].analysis_as_of,
    portfolio_key: rows[0].portfolio_key,
    policy: policyOf(rows[0]),
    daily_exposure: exposure(rows[0], 'daily'),
    games: [...games.values()],
  }
}
