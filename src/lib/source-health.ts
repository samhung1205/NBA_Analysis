/** 狀態頁的來源角色與排程判讀；不變更資料新鮮度、模型或風控規則。 */
import { TPE_OFFSET_MS } from './time'

type Source = Record<string, any>
type Health = 'ok' | 'warn' | 'error' | 'unknown'
type State = 'ok' | 'stale' | 'warning' | 'error' | 'unknown' | 'standby' | 'inactive' | 'historical' | 'waiting' | 'blocked'

function timestamp(value: any): number | null {
  if (value == null || value === '') return null
  const ms = new Date(value).getTime()
  return Number.isFinite(ms) ? ms : null
}

function staleAt(row: Source, now: number): boolean | null {
  const last = timestamp(row.last_success_at)
  const expected = row.expected_interval_min == null ? null : Number(row.expected_interval_min)
  return last != null && expected != null && expected > 0 ? now > last + expected * 120000 : null
}

/** 與 core/scheduling.py 的 referee_assignments 相同：台灣 00–10、20–23 時的 :07/:37。
 * 只數應執行的時段；休息期間不要求每 30 分鐘抓取。超過兩個輪詢間隔仍須告警。
 */
export function refereeCadence(now: number) {
  const midnight = Math.floor((now + TPE_OFFSET_MS) / 86400000) * 86400000 - TPE_OFFSET_MS
  const ticks: number[] = []
  for (let d = -2; d <= 1; d++) {
    for (let h = 0; h < 24; h++) {
      if (h > 10 && h < 20) continue
      for (const m of [7, 37]) ticks.push(midnight + d * 86400000 + h * 3600000 + m * 60000)
    }
  }
  const last = ticks.filter((t) => t <= now).at(-1)!
  const next = ticks.find((t) => t > now)!
  return { required: ticks.filter((t) => t < now - 60 * 60000).at(-1)!, next,
    paused: next - last > 30 * 60000 }
}

export function classifySource(row: Source, rows: Source[], now: number) {
  const stale = staleAt(row, now)
  const result = (health: Health, state: State, detail: string, action: string | null = null,
    operational = true, nextPoll: number | null = null) => ({ health, state, detail, action, operational, stale,
    next_poll_at: nextPoll == null ? null : new Date(nextPoll).toISOString() })

  if (['balldontlie', 'bbref'].includes(row.source_key))
    return result('unknown', 'inactive', '舊來源紀錄；現行排程沒有使用此來源，不要求持續更新。', null, false)
  if (row.source_key === 'nbainjuries_backfill')
    return result(row.last_status === 'error' ? 'error' : row.last_status === 'warn' ? 'warn' : 'unknown',
      'historical', '歷史傷病回填紀錄，與即時傷病輪詢分開；上次警告不代表今日傷病資料過期。',
      row.last_status === 'warn' || row.last_status === 'error' ? '研究或重新回填時再檢查失敗報告；即時狀態請看 NBA 官方傷病報告。' : null, false)

  // 備援未被呼叫不等於故障；只有更新且仍新鮮的其他來源成功，才可判讀為待命。
  const attempt = timestamp(row.last_attempt_at)
  if (['nba_api', 'espn'].includes(row.source_key) && (stale || row.last_status !== 'ok')) {
    const alternative = rows.find((s) => s.source_key !== row.source_key
      && ['nba_cdn', 'nba_api', 'espn'].includes(s.source_key) && s.last_status === 'ok'
      && staleAt(s, now) === false && (timestamp(s.last_success_at) ?? 0) > (attempt ?? 0))
    if (alternative) return result('unknown', 'standby',
      `備援僅在需要時呼叫；${alternative.display_name || alternative.source_key} 最近成功。上次備援執行紀錄仍保留，尚未重新驗證備援健康。`, null, false)
  }
  if (row.last_status === 'error')
    return result('error', 'error', '來源最近一次執行失敗。', '檢查下方錯誤與排程日誌；排除後再確認新資料。')
  if (row.source_key === 'twsport' && ['blocked', 'not_configured'].includes(row.last_outcome))
    return result('warn', 'blocked', '台彩自動擷取受阻或未啟用；來源心跳不代表已有新盤口。',
      '需取得並匯入有效台彩盤口，決策中心才可能產生可操作機會。')
  if (row.last_status === 'warn')
    return result('warn', 'warning', '來源回報警告；請依執行紀錄判斷原因，不能一概視為資料過期。', '檢查下方紀錄與來源設定。')

  if (row.source_key === 'nba_referees' && row.last_status === 'ok') {
    const cadence = refereeCadence(now)
    const last = timestamp(row.last_success_at)
    if (last == null || last < cadence.required)
      return result('warn', 'stale', '裁判來源已錯過應執行的輪詢時段。此資料目前只供研究收集，未接入模型。',
        '檢查 referee_assignments 排程與來源日誌。', true, cadence.next)
    if (cadence.paused) return result('ok', 'waiting',
      '目前是裁判來源排程休息時段，不要求持續更新；此資料尚未接入模型。', null, true, cadence.next)
  }
  if (stale) return result('warn', 'stale', '最後成功時間已超過預期間隔兩倍。', '檢查來源與排程，勿將舊資料當作最新資料。')
  if (row.last_status === 'ok' && timestamp(row.last_success_at) != null)
    return result('ok', 'ok', '來源最近一次執行正常。')
  return result('unknown', 'unknown', '尚無足夠的成功執行紀錄。', '確認來源是否已設定並開始執行。')
}
