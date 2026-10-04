/**
 * 資料查詢層
 * ------------------------------------------------------------
 * 所有 SQL 集中於此，routes 只組裝回應格式。
 * SQL 一律使用 `?` 佔位符與標準語法，D1(SQLite) 與 Postgres 皆可執行。
 */

import type { Db } from './index'
import { tpeDayRangeUtc } from '../lib/time'

export type GameRow = {
  id: number
  nba_game_id: string | null
  season: string
  date_utc: string
  status: string
  arena: string | null
  home_pts: number | null
  away_pts: number | null
  home_h1: number | null
  away_h1: number | null
  home_h2: number | null
  away_h2: number | null
  home_team_id: number
  away_team_id: number
  home_abbr: string
  home_name: string
  home_name_zh: string | null
  away_abbr: string
  away_name: string
  away_name_zh: string | null
}

const GAME_SELECT = `
  SELECT g.id, g.nba_game_id, g.season, g.date_utc, g.status, g.arena,
         g.home_pts, g.away_pts, g.home_h1, g.away_h1, g.home_h2, g.away_h2,
         g.home_q1, g.home_q2, g.home_q3, g.home_q4, g.home_ot,
         g.away_q1, g.away_q2, g.away_q3, g.away_q4, g.away_ot,
         g.home_team_id, g.away_team_id,
         ht.abbr AS home_abbr, ht.name AS home_name, ht.name_zh AS home_name_zh,
         at.abbr AS away_abbr, at.name AS away_name, at.name_zh AS away_name_zh
  FROM games g
  JOIN teams ht ON ht.id = g.home_team_id
  JOIN teams at ON at.id = g.away_team_id
`

/** 指定台灣日期的賽事（含球隊資訊） */
export async function getGamesByTpeDate(db: Db, tpeDate: string): Promise<GameRow[]> {
  const { startUtc, endUtc } = tpeDayRangeUtc(tpeDate)
  return db.all<GameRow>(
    `${GAME_SELECT} WHERE g.date_utc >= ? AND g.date_utc < ? ORDER BY g.date_utc ASC`,
    [startUtc, endUtc]
  )
}

export async function getGameById(db: Db, id: number): Promise<GameRow | null> {
  return db.one<GameRow>(`${GAME_SELECT} WHERE g.id = ?`, [id])
}

/** 每場最新一筆預測（批次） */
export async function getLatestPredictions(db: Db, gameIds: number[]) {
  if (!gameIds.length) return new Map<number, any>()
  const ph = gameIds.map(() => '?').join(',')
  const rows = await db.all<any>(
    `SELECT p.* FROM predictions p
      WHERE p.game_id IN (${ph})
        AND p.created_at = (
          SELECT MAX(p2.created_at) FROM predictions p2 WHERE p2.game_id = p.game_id
        )`,
    gameIds
  )
  const map = new Map<number, any>()
  for (const r of rows) if (!map.has(r.game_id)) map.set(r.game_id, r)
  return map
}

export async function getLatestPredictionForGame(db: Db, gameId: number) {
  return db.one<any>(
    `SELECT * FROM predictions WHERE game_id = ? ORDER BY created_at DESC LIMIT 1`,
    [gameId]
  )
}

/**
 * 每場 / 每來源 / 每 bookmaker / 每玩法的最新盤口（批次）
 * - D.1 起同一來源可有多家 bookmaker（The Odds API）：latest 以 bookmaker 分開取，不混在一起。
 *   舊資料 bookmaker 為 NULL → 視為來源本身（台彩只有自己一家）。
 * - 最新一筆若為 suspended / closed，代表「目前沒有可下注的報價」→ 不回傳（不退回較舊的 open 報價）。
 *   舊資料（market_status 為 NULL）視為 open。歷史仍完整保留在 odds_snapshots（見 getOddsHistory）。
 */
export async function getLatestOdds(db: Db, gameIds: number[]) {
  if (!gameIds.length) return new Map<number, any[]>()
  const ph = gameIds.map(() => '?').join(',')
  const rows = await db.all<any>(
    `SELECT o.* FROM odds_snapshots o
      WHERE o.game_id IN (${ph})
        AND o.fetched_at = (
          SELECT MAX(o2.fetched_at) FROM odds_snapshots o2
           WHERE o2.game_id = o.game_id AND o2.source = o.source AND o2.market = o.market
             AND COALESCE(o2.bookmaker, o2.source) = COALESCE(o.bookmaker, o.source)
        )
      ORDER BY o.source, o.market, o.bookmaker`,
    gameIds
  )
  const map = new Map<number, any[]>()
  for (const r of rows) {
    if (r.market_status != null && r.market_status !== 'open') continue
    if (!map.has(r.game_id)) map.set(r.game_id, [])
    map.get(r.game_id)!.push(r)
  }
  return map
}

/** 單場完整盤口歷史（供折線圖） */
export async function getOddsHistory(db: Db, gameId: number) {
  return db.all<any>(
    `SELECT * FROM odds_snapshots WHERE game_id = ? ORDER BY fetched_at ASC, id ASC`,
    [gameId]
  )
}

/**
 * 指定台灣日期的傷病報告（每位球員取最新一筆）
 *
 * 收錄範圍刻意涵蓋兩種情況，避免漏掉申報：
 *   (a) 在該台灣日期當天申報的報告
 *   (b) 掛在該台灣日期比賽上的報告
 * 原因：NBA 官方於「賽前一日當地 17:00 前」申報，換算成台灣時間常落在
 * 比賽日的前一天，若只用 (a) 嚴格過濾，隔日賽事的傷病會查不到。
 */
export async function getInjuriesByTpeDate(db: Db, tpeDate: string) {
  const { startUtc, endUtc } = tpeDayRangeUtc(tpeDate)
  return db.all<any>(
    `SELECT i.id, i.report_time_utc, i.status, i.reason, i.source,
            i.game_id, i.player_id,
            p.name AS player_name, p.position, p.is_starter,
            t.id AS team_id, t.abbr AS team_abbr, t.name AS team_name, t.name_zh AS team_name_zh
       FROM injuries i
       LEFT JOIN players p ON p.id = i.player_id
       LEFT JOIN teams   t ON t.id = i.team_id
      WHERE (
              (i.report_time_utc >= ? AND i.report_time_utc < ?)
              OR i.game_id IN (
                SELECT g.id FROM games g WHERE g.date_utc >= ? AND g.date_utc < ?
              )
            )
        AND i.report_time_utc = (
          SELECT MAX(i2.report_time_utc) FROM injuries i2 WHERE i2.player_id = i.player_id
        )
      ORDER BY t.abbr ASC, i.status ASC`,
    [startUtc, endUtc, startUtc, endUtc]
  )
}

/** 最近 N 場已完成比賽（球隊近況 / H2H 用） */
export async function getTeamRecentGames(db: Db, teamId: number, limit = 10) {
  return db.all<GameRow>(
    `${GAME_SELECT} WHERE (g.home_team_id = ? OR g.away_team_id = ?) AND g.status = 'final'
      ORDER BY g.date_utc DESC LIMIT ${Number(limit)}`,
    [teamId, teamId]
  )
}

/** 歷史對戰 H2H */
export async function getHeadToHead(db: Db, teamA: number, teamB: number, limit = 10) {
  return db.all<GameRow>(
    `${GAME_SELECT}
      WHERE g.status = 'final'
        AND ((g.home_team_id = ? AND g.away_team_id = ?) OR (g.home_team_id = ? AND g.away_team_id = ?))
      ORDER BY g.date_utc DESC LIMIT ${Number(limit)}`,
    [teamA, teamB, teamB, teamA]
  )
}

/** 單場雙方球隊數據 */
export async function getTeamGameStats(db: Db, gameId: number) {
  return db.all<any>(
    `SELECT s.*, t.abbr, t.name, t.name_zh
       FROM team_game_stats s JOIN teams t ON t.id = s.team_id
      WHERE s.game_id = ?`,
    [gameId]
  )
}

/** 單場球員 box score */
export async function getPlayerGameStats(db: Db, gameId: number) {
  return db.all<any>(
    `SELECT s.*, p.name AS player_name, p.position, t.abbr AS team_abbr
       FROM player_game_stats s
       JOIN players p ON p.id = s.player_id
       LEFT JOIN teams t ON t.id = s.team_id
      WHERE s.game_id = ?
      ORDER BY s.started DESC, s.min DESC`,
    [gameId]
  )
}

/** 個人投注紀錄 */
export async function getBets(db: Db, userId: number) {
  return db.all<any>(
    `SELECT b.*, g.date_utc, g.status AS game_status,
            g.home_pts, g.away_pts,
            ht.abbr AS home_abbr, at.abbr AS away_abbr,
            ht.name_zh AS home_name_zh, at.name_zh AS away_name_zh
       FROM bets b
       JOIN games g ON g.id = b.game_id
       JOIN teams ht ON ht.id = g.home_team_id
       JOIN teams at ON at.id = g.away_team_id
      WHERE b.user_id = ?
      ORDER BY b.placed_at DESC`,
    [userId]
  )
}

export async function getDataSources(db: Db) {
  return db.all<any>(`SELECT * FROM data_sources ORDER BY category, source_key`)
}

export async function getModelMetrics(db: Db) {
  return db.all<any>(
    `SELECT * FROM model_metrics ORDER BY evaluated_at DESC, model_version DESC`
  )
}

export async function getAllTeams(db: Db) {
  return db.all<any>(`SELECT * FROM teams ORDER BY conference, division, abbr`)
}
