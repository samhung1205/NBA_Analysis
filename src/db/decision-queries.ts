/**
 * Phase D.5 查詢（decision board / bankroll / actual bets）
 * ------------------------------------------------------------
 * 與 queries.ts 相同慣例：`?` 佔位符、D1 與 Postgres 共用 SQL。只讀 Python 物化的結果；寫入只記錄使用者輸入的事實
 * （bets / ledger / bet_events / risk_state_claims），不計算任何 exposure / 額度 / bankroll。
 */

import type { Db } from './index'

/** 0008 是否已套用（未套用 → API 退回 D.5 以前的行為，決策中心回 unavailable） */
export async function hasD5Schema(db: Db): Promise<boolean> {
  try {
    await db.all('SELECT id FROM decision_snapshots WHERE 1 = 0')
    await db.all('SELECT record_status FROM bets WHERE 1 = 0')
    return true
  } catch {
    return false
  }
}

export async function getAccount(db: Db, userId: number) {
  return db.one<any>('SELECT * FROM bankroll_accounts WHERE user_id = ?', [userId])
}

export async function getLedgerMax(db: Db, accountId: number | null) {
  if (accountId == null) return 0
  const r = await db.one<any>('SELECT COALESCE(MAX(id), 0) AS m FROM bankroll_ledger WHERE account_id = ?', [accountId])
  return Number(r?.m ?? 0)
}

export async function getUserEventMax(db: Db, userId: number) {
  const r = await db.one<any>(
    "SELECT COALESCE(MAX(id), 0) AS m FROM bet_events WHERE user_id = ? AND actor = 'user'",
    [userId]
  )
  return Number(r?.m ?? 0)
}

export async function getLatestSnapshot(db: Db, userId: number, bettingDay: string) {
  return db.one<any>(
    `SELECT * FROM decision_snapshots WHERE user_id = ? AND betting_day = ?
      ORDER BY as_of DESC, id DESC LIMIT 1`,
    [userId, bettingDay]
  )
}

export async function getLatestSnapshotAnyDay(db: Db, userId: number) {
  return db.one<any>(
    'SELECT * FROM decision_snapshots WHERE user_id = ? ORDER BY last_confirmed_at DESC, id DESC LIMIT 1',
    [userId]
  )
}

export async function getOpportunities(db: Db, snapshotId: number) {
  return db.all<any>('SELECT * FROM decision_opportunities WHERE snapshot_id = ? ORDER BY display_rank, id', [snapshotId])
}

export async function getOpportunityForUser(db: Db, id: number, userId: number) {
  return db.one<any>('SELECT * FROM decision_opportunities WHERE id = ? AND user_id = ?', [id, userId])
}

/** 每場「主力（先發）確定 / 很可能缺陣」人數（每位球員最新一筆；只顯示用） */
export async function getKeyAbsences(db: Db, gameIds: number[]) {
  const out = new Map<number, number>()
  if (!gameIds.length) return out
  const ph = gameIds.map(() => '?').join(',')
  const rows = await db.all<any>(
    `SELECT i.game_id, COUNT(*) AS n FROM injuries i JOIN players p ON p.id = i.player_id
      WHERE i.game_id IN (${ph}) AND p.is_starter = 1 AND i.status IN ('Out', 'Doubtful')
        AND i.report_time_utc = (SELECT MAX(i2.report_time_utc) FROM injuries i2 WHERE i2.player_id = i.player_id)
      GROUP BY i.game_id`,
    gameIds
  )
  for (const r of rows) out.set(Number(r.game_id), Number(r.n))
  return out
}

export async function getUserBets(db: Db, userId: number, opts: { day?: string | null; includeInactive?: boolean } = {}) {
  const where = ['b.user_id = ?']
  const params: unknown[] = [userId]
  if (opts.day) {
    where.push('b.betting_day = ?')
    params.push(opts.day)
  }
  if (!opts.includeInactive) where.push("COALESCE(b.record_status, 'active') = 'active'")
  return db.all<any>(
    `SELECT b.*, g.date_utc, g.status AS game_status, g.home_pts, g.away_pts,
            ht.abbr AS home_abbr, at.abbr AS away_abbr, ht.name_zh AS home_name_zh, at.name_zh AS away_name_zh
       FROM bets b
       JOIN games g ON g.id = b.game_id
       JOIN teams ht ON ht.id = g.home_team_id
       JOIN teams at ON at.id = g.away_team_id
      WHERE ${where.join(' AND ')}
      ORDER BY b.placed_at DESC, b.id DESC`,
    params
  )
}

export async function getBetForUser(db: Db, id: number, userId: number) {
  return db.one<any>('SELECT * FROM bets WHERE id = ? AND user_id = ?', [id, userId])
}

export async function getBetByRequest(db: Db, userId: number, requestId: string) {
  return db.one<any>('SELECT * FROM bets WHERE user_id = ? AND client_request_id = ?', [userId, requestId])
}

export async function getBetEvents(db: Db, betId: number) {
  return db.all<any>('SELECT * FROM bet_events WHERE bet_id = ? ORDER BY id', [betId])
}

export async function getLedger(db: Db, accountId: number) {
  return db.all<any>('SELECT * FROM bankroll_ledger WHERE account_id = ? ORDER BY id DESC', [accountId])
}

export async function getLedgerByRequest(db: Db, accountId: number, requestId: string) {
  return db.one<any>('SELECT * FROM bankroll_ledger WHERE account_id = ? AND client_request_id = ?', [accountId, requestId])
}

export async function getDaySnapshots(db: Db, accountId: number, limit = 14) {
  return db.all<any>(
    `SELECT * FROM bankroll_day_snapshots WHERE account_id = ? ORDER BY betting_day DESC LIMIT ${Number(limit)}`,
    [accountId]
  )
}

/* ------------------------------------------------------------------ */
/* 寫入語句（交給 db.atomic；全部是 INSERT … VALUES / 單列 UPDATE）       */
/* ------------------------------------------------------------------ */

/** 認領下一個版本號：PK (account_id, version) → 兩個請求用同一版本 → 第二個整個交易失敗 */
export function claimSteps(accountId: number, expected: number, next: number, kind: string, requestId: string, now: string) {
  return [
    {
      sql: 'INSERT INTO risk_state_claims (account_id, version, kind, request_id, created_at) VALUES (?, ?, ?, ?, ?)',
      params: [accountId, next, kind, requestId, now],
    },
    {
      sql: `UPDATE bankroll_accounts SET risk_state_version = ?, risk_state_changed_at = ?
             WHERE id = ? AND risk_state_version = ?`,
      params: [next, now, accountId, expected],
    },
  ]
}

export const BET_INSERT_COLUMNS = [
  'user_id', 'game_id', 'market', 'selection', 'line', 'odds', 'stake', 'result', 'note', 'placed_at',
  'account_id', 'betting_day', 'source', 'bookmaker', 'market_type', 'period', 'outcome_set', 'model_target',
  'model_threshold', 'comparator', 'settlement_rule', 'origin', 'strategy_compliance', 'reference_odds_snapshot_id',
  'reference_decimal_odds', 'reference_pricing_snapshot_id', 'reference_sizing_snapshot_id', 'decision_snapshot_id',
  'decision_opportunity_id', 'paper_decision_id', 'reference_context', 'bankroll_day_snapshot_id', 'risk_check',
  'override_reason', 'override_confirmed_at', 'client_request_id', 'recorded_at', 'record_status', 'supersedes_bet_id',
]

export function betInsertStep(row: Record<string, unknown>) {
  const cols = BET_INSERT_COLUMNS
  return {
    sql: `INSERT INTO bets (${cols.join(', ')}) VALUES (${cols.map(() => '?').join(', ')})`,
    params: cols.map((c) => (row[c] === undefined ? null : row[c])),
  }
}

/** bet_events：bet_id 以 client_request_id 在同一交易內找回（D1 / Postgres 共用，不需 RETURNING） */
export function betEventStep(userId: number, requestId: string, eventType: string, payload: unknown, now: string) {
  return {
    sql: `INSERT INTO bet_events (bet_id, user_id, event_type, actor, payload, created_at)
          VALUES ((SELECT id FROM bets WHERE user_id = ? AND client_request_id = ?), ?, ?, 'user', ?, ?)`,
    params: [userId, requestId, userId, eventType, JSON.stringify(payload ?? {}), now],
  }
}

export function betEventByIdStep(betId: number, userId: number, eventType: string, payload: unknown, now: string) {
  return {
    sql: `INSERT INTO bet_events (bet_id, user_id, event_type, actor, payload, created_at) VALUES (?, ?, ?, 'user', ?, ?)`,
    params: [betId, userId, eventType, JSON.stringify(payload ?? {}), now],
  }
}
