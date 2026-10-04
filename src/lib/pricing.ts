/**
 * Phase D.2 定價結果的序列化（只讀、不計算）
 * ------------------------------------------------------------
 * 去水 / 模型機率 / edge / EV 的數學**只**在 Python（pipeline/core/pricing/），
 * 結果寫入 market_pricing_snapshots；這裡只做分組、排序與欄位命名（serialization contract）。
 * 取代階段一的 src/lib/edge.ts（TS 版去水 / Kelly / line_gap 已移除，避免兩套數學並存）。
 *
 * 欄位定義（與 Python engine 相同，不得混用）：
 *   raw_implied_prob = 1 / decimal_odds（含水）
 *   market_overround = Σ raw_implied_prob − 1
 *   fair_no_vig_prob = raw_implied_prob / Σ raw_implied_prob（proportional-v1）
 *   model_prob       = C.5E 分佈：該 outcome 嚴格勝出的機率
 *   push_prob        = P(結果 = 線)，退還本金（整數讓分 / 大小分線）
 *   edge_vs_fair     = model_prob − fair_no_vig_prob（唯一叫 edge 的量）
 *   ev_per_unit      = model_prob × (decimal_odds − 1) − loss_prob（實際賠率，每投注 1 單位）
 */

export const PRICING_VERSION = 'pricing-v1'
export const NO_VIG_METHOD = 'proportional-v1'

export const PRICING_DEFINITIONS = {
  raw_implied_prob: '1 / decimal_odds（含水）',
  market_overround: 'Σ raw_implied_prob − 1（同一 snapshot 全部 outcome）',
  fair_no_vig_prob: 'raw_implied_prob / Σ raw_implied_prob（proportional-v1；兩向 / 三向全部 outcome 一起去水）',
  model_prob: 'C.5E 預測分佈：該 outcome 嚴格勝出的機率 P(win)',
  push_prob: 'P(結果 = 線)，退還本金（只有整數讓分 / 大小分線）；三向的和局是 outcome 不是 push',
  edge_vs_fair: 'model_prob − fair_no_vig_prob（唯一的 edge；不是線差、不是 model − raw implied、不是 EV）',
  ev_per_unit: 'model_prob × (decimal_odds − 1) − loss_prob（實際提供的賠率；每投注 1 單位）',
  expected_return: '1 + ev_per_unit',
  ev_percent: '100 × ev_per_unit',
}

const MARKET_LABEL: Record<string, string> = {
  ml: '不讓分(獨贏)',
  spread: '讓分',
  total: '大小分',
  h1_ml: '上半場獨贏',
  h1_spread: '上半場讓分',
  h1_total: '上半場大小分',
}
const SIDE_ORDER = ['home', 'draw', 'away', 'over', 'under']
const MARKET_ORDER = ['ml', 'spread', 'total', 'h1_ml', 'h1_spread', 'h1_total']

function num(v: any): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
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

/** Edge 顯示等級（純呈現：依 edge_vs_fair 標色；不是推薦） */
export function edgeTier(edge: number | null): 'none' | 'low' | 'mid' | 'high' {
  if (edge == null || edge <= 0.01) return 'none'
  if (edge < 0.03) return 'low'
  if (edge < 0.06) return 'mid'
  return 'high'
}

function shapeOutcome(r: any) {
  return {
    pricing_id: r.id == null ? null : Number(r.id), // D.3：對照 odds.sizing / bet_sizing_snapshots.market_pricing_snapshot_id
    side: r.side,
    display_line: num(r.display_line),
    model_target: r.model_target ?? null,
    model_threshold: num(r.model_threshold),
    comparator: r.comparator ?? null,
    decimal_odds: num(r.decimal_odds),
    raw_implied_prob: num(r.raw_implied_prob),
    fair_no_vig_prob: num(r.fair_no_vig_prob),
    model_prob: num(r.model_prob),
    push_prob: num(r.push_prob),
    loss_prob: num(r.loss_prob),
    edge_vs_fair: num(r.edge_vs_fair),
    ev_per_unit: num(r.ev_per_unit),
    expected_return: num(r.expected_return),
    ev_percent: num(r.ev_percent),
  }
}

/**
 * market_pricing_snapshots 的列（每 outcome 一列）→ 每個 snapshot 一個 market。
 * latestOpenSnapshotIds：目前顯示中的最新 open 盤口；沒有定價列者列入 unpriced_odds_snapshot_ids。
 */
export function shapePricing(rows: any[] | null, latestOpenSnapshotIds: number[], latestPredictionId: number | null) {
  const base = {
    pricing_version: PRICING_VERSION,
    no_vig_method: NO_VIG_METHOD,
    definitions: PRICING_DEFINITIONS,
  }
  if (rows == null) {
    return { ...base, status: 'unavailable', markets: [] as any[], unpriced_odds_snapshot_ids: latestOpenSnapshotIds }
  }
  const bySnap = new Map<number, any[]>()
  for (const r of rows) {
    const k = Number(r.odds_snapshot_id)
    if (!bySnap.has(k)) bySnap.set(k, [])
    bySnap.get(k)!.push(r)
  }
  const markets: any[] = []
  for (const [snapId, rs] of bySnap) {
    // 同一 analysis_as_of 有多組預測（極少見）→ 取 prediction_id 最大的一組（與 Python 選擇規則一致）
    const pid = Math.max(...rs.map((r) => (r.prediction_id == null ? 0 : Number(r.prediction_id))))
    const set = rs.filter((r) => (r.prediction_id == null ? 0 : Number(r.prediction_id)) === pid)
    const r0 = set[0]
    markets.push({
      odds_snapshot_id: snapId,
      source: r0.source,
      bookmaker: r0.bookmaker ?? r0.source,
      market: r0.market,
      market_label: MARKET_LABEL[r0.market] ?? r0.market,
      market_type: r0.market_type ?? null,
      period: r0.period ?? null,
      outcome_set: r0.outcome_set ?? null,
      line: num(r0.line),
      away_line: num(r0.away_line),
      status: r0.status,
      status_reason: r0.status_reason ?? null,
      settlement_rule: r0.settlement_rule ?? null,
      total_raw_implied: num(r0.total_raw_implied),
      market_overround: num(r0.market_overround),
      fair_prob_sum: num(r0.fair_prob_sum),
      odds_fetched_at: r0.odds_fetched_at,
      analysis_as_of: r0.analysis_as_of,
      prediction_id: r0.prediction_id == null ? null : Number(r0.prediction_id),
      prediction_is_latest: r0.prediction_id == null ? null : Number(r0.prediction_id) === latestPredictionId,
      prediction_kind: r0.prediction_kind ?? null,
      prediction_profile: r0.prediction_profile ?? null,
      model_version: r0.model_version ?? null,
      artifact_version: r0.artifact_version ?? null,
      distribution_version: r0.distribution_version ?? null,
      warnings: json(r0.warnings, []),
      diagnostics: json(r0.diagnostics, {}),
      outcomes: set
        .map(shapeOutcome)
        .sort((a, b) => SIDE_ORDER.indexOf(a.side) - SIDE_ORDER.indexOf(b.side)),
    })
  }
  markets.sort(
    (a, b) =>
      (a.source === 'twsport' ? 0 : 1) - (b.source === 'twsport' ? 0 : 1) ||
      String(a.bookmaker).localeCompare(String(b.bookmaker)) ||
      MARKET_ORDER.indexOf(a.market) - MARKET_ORDER.indexOf(b.market)
  )
  const unpriced = latestOpenSnapshotIds.filter((id) => !bySnap.has(id))
  const status = !latestOpenSnapshotIds.length
    ? 'no_odds'
    : unpriced.length === latestOpenSnapshotIds.length
      ? 'not_priced'
      : unpriced.length
        ? 'partial'
        : 'priced'
  return { ...base, status, markets, unpriced_odds_snapshot_ids: unpriced }
}

/**
 * 【deprecated】階段一的 edges[]（前端卡片 / 詳情頁沿用的形狀）。
 * D.2 起只由 Python 定價結果導出（台彩、已定價市場），不再在 TS 計算：
 *   - edge = edge_vs_fair（模型 − 去水公允機率）；讓分 / 大小分不再用 line_gap
 *   - selection = edge_vs_fair 較大的一邊（顯示用，不是投注推薦）
 *   - kelly_quarter / line_gap / model_value 保留欄位但為 null（線差不是 edge；D.3 起 Kelly / 理論注碼改讀
 *     odds.sizing 與 pricing.markets[].outcomes[].sizing，由 Python 計算，這裡不導出、不重算）
 * 新程式請改讀 odds.pricing.markets。
 */
export function legacyEdges(pricing: { markets: any[] }) {
  const out: any[] = []
  for (const m of pricing.markets) {
    if (m.source !== 'twsport' || m.status !== 'priced' || m.market === 'h1_ml') continue
    const priced = m.outcomes.filter((o: any) => o.edge_vs_fair != null)
    if (!priced.length) continue
    const best = priced.reduce((a: any, b: any) => (b.edge_vs_fair > a.edge_vs_fair ? b : a))
    out.push({
      market: m.market,
      market_label: m.market_label,
      selection: best.side,
      line: m.line,
      odds: best.decimal_odds,
      model_prob: best.model_prob,
      market_fair_prob: best.fair_no_vig_prob,
      market_implied_prob: best.raw_implied_prob,
      vig: m.market_overround,
      push_prob: best.push_prob,
      edge: best.edge_vs_fair,
      ev_per_unit: best.ev_per_unit,
      tier: edgeTier(best.edge_vs_fair),
      odds_snapshot_id: m.odds_snapshot_id,
      kelly_quarter: null,
      line_gap: null,
      model_value: null,
      deprecated: true,
    })
  }
  return out
}
