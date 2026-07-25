/**
 * 台灣時間 (UTC+8) 工具
 * ------------------------------------------------------------
 * 規格書要求「隔日(台灣時間)賽事列表」。DB 一律存 UTC ISO8601，
 * 這裡負責 台灣日期 ↔ UTC 區間 的換算，避免時區錯位。
 */

export const TPE_OFFSET_MS = 8 * 60 * 60 * 1000

/** 取得某個 UTC 時間點所對應的台灣日期字串 YYYY-MM-DD */
export function tpeDateString(d: Date = new Date()): string {
  return new Date(d.getTime() + TPE_OFFSET_MS).toISOString().slice(0, 10)
}

/** 台灣日期加減天數 */
export function addDays(dateStr: string, days: number): string {
  const d = new Date(`${dateStr}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

/**
 * 台灣日期 (YYYY-MM-DD) → 該日 00:00~24:00 (台灣時間) 對應的 UTC ISO 區間
 * 例：2026-07-26 (TPE) → 2026-07-25T16:00:00Z ~ 2026-07-26T16:00:00Z
 */
export function tpeDayRangeUtc(dateStr: string): { startUtc: string; endUtc: string } {
  const start = new Date(`${dateStr}T00:00:00Z`).getTime() - TPE_OFFSET_MS
  const end = start + 24 * 60 * 60 * 1000
  return {
    startUtc: new Date(start).toISOString(),
    endUtc: new Date(end).toISOString(),
  }
}

/** 今天（台灣） */
export function todayTpe(): string {
  return tpeDateString()
}

/** 明天（台灣） */
export function tomorrowTpe(): string {
  return addDays(todayTpe(), 1)
}

/** 格式化為台灣時間顯示字串：07/26 (日) 08:30 */
export function formatTpe(utcIso: string): string {
  const d = new Date(utcIso)
  const t = new Date(d.getTime() + TPE_OFFSET_MS)
  const week = ['日', '一', '二', '三', '四', '五', '六'][t.getUTCDay()]
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(t.getUTCMonth() + 1)}/${pad(t.getUTCDate())} (${week}) ${pad(
    t.getUTCHours()
  )}:${pad(t.getUTCMinutes())}`
}
