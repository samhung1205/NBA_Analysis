/**
 * 盤口隱含機率與 Edge 計算 (階段一：呈現層計算；階段二會由 Python 寫入更精確版本)
 * ------------------------------------------------------------
 * 規格書 §6「抽水現實」：台彩返還率低於國際盤，所有 edge 與 ROI
 * 必須以台彩實際賠率計算，因此這裡：
 *   - 以「去除抽水後的正規化機率」(devig / no-vig) 作為市場公允機率
 *   - edge = 模型機率 − 市場公允機率
 *   - Kelly 建議一律以實際賠率計算，並套 1/4 Kelly 上限
 */

/** 台彩十進位賠率 → 隱含機率（含抽水） */
export function impliedProb(decimalOdds: number): number {
  if (!decimalOdds || decimalOdds <= 1) return 0
  return 1 / decimalOdds
}

/** 兩邊賠率去抽水（比例法），回傳公允機率 */
export function devig(homeOdds?: number | null, awayOdds?: number | null) {
  const ph = impliedProb(homeOdds ?? 0)
  const pa = impliedProb(awayOdds ?? 0)
  const sum = ph + pa
  if (sum <= 0) return { home: null, away: null, vig: null }
  return {
    home: ph / sum,
    away: pa / sum,
    vig: sum - 1, // 市場抽水（>0 表示莊家優勢）
  }
}

/** Edge：模型機率 − 市場公允機率 */
export function calcEdge(modelProb?: number | null, fairProb?: number | null): number | null {
  if (modelProb == null || fairProb == null) return null
  return modelProb - fairProb
}

/**
 * Kelly 下注比例（含分數 Kelly 上限）
 * f* = (p * (b+1) - 1) / b ，b = decimalOdds - 1
 */
export function kellyFraction(
  modelProb: number | null | undefined,
  decimalOdds: number | null | undefined,
  fraction = 0.25
): number | null {
  if (modelProb == null || !decimalOdds || decimalOdds <= 1) return null
  const b = decimalOdds - 1
  const f = (modelProb * (b + 1) - 1) / b
  if (f <= 0) return 0
  return Math.min(f * fraction, fraction)
}

/** Edge 等級（前端標色用） */
export function edgeTier(edge: number | null): 'none' | 'low' | 'mid' | 'high' {
  if (edge == null || edge <= 0.01) return 'none'
  if (edge < 0.03) return 'low'
  if (edge < 0.06) return 'mid'
  return 'high'
}
