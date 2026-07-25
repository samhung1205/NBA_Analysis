#!/usr/bin/env node
/**
 * 將開發期測試資料灌入正式 Postgres
 * ------------------------------------------------------------
 * 用法：
 *   DATABASE_URL="postgresql://..." npm run db:seed:pg
 *
 * 資料來源為 seed/seed.template.sql，經 scripts/render-seed.mjs 展開時間 token
 * 後即為純標準 SQL（不含方言特有的日期函式），可同時用於 D1 與 Postgres，
 * 因此只需維護單一份 seed 樣板。
 *
 * ⚠️ 此腳本會清空所有資料表再重新灌入，請勿對已有真實資料的資料庫執行。
 */

import { readFile } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import postgres from 'postgres'
import { renderSeed } from './render-seed.mjs'

const __dirname = dirname(fileURLToPath(import.meta.url))
const TEMPLATE = join(__dirname, '..', 'seed', 'seed.template.sql')

const url = process.env.DATABASE_URL
if (!url) {
  console.error('✗ 缺少環境變數 DATABASE_URL')
  console.error('  例：DATABASE_URL="postgresql://user:pass@host:6543/postgres" npm run db:seed:pg')
  process.exit(1)
}

const sql = postgres(url, { max: 1, prepare: false, onnotice: () => {} })
try {
  const rendered = renderSeed(await readFile(TEMPLATE, 'utf8'))
  console.log('→ 灌入測試資料（會先清空所有表）…')
  await sql.unsafe(rendered)

  // Postgres 的 SERIAL 序列不會因 DELETE 而重置，需同步到目前最大 id，
  // 否則後續 INSERT（例如新增投注紀錄）會撞到主鍵衝突。
  const tables = ['teams', 'players', 'games', 'team_game_stats', 'player_game_stats',
    'injuries', 'odds_snapshots', 'predictions', 'bets', 'users', 'data_sources', 'model_metrics']
  for (const t of tables) {
    await sql.unsafe(
      `SELECT setval(pg_get_serial_sequence('${t}', 'id'),
                     COALESCE((SELECT MAX(id) FROM ${t}), 1))`
    )
  }

  const counts = await sql`
    SELECT 'teams' AS t, COUNT(*) AS n FROM teams
    UNION ALL SELECT 'games', COUNT(*) FROM games
    UNION ALL SELECT 'predictions', COUNT(*) FROM predictions
    UNION ALL SELECT 'odds_snapshots', COUNT(*) FROM odds_snapshots
    UNION ALL SELECT 'injuries', COUNT(*) FROM injuries
    UNION ALL SELECT 'data_sources', COUNT(*) FROM data_sources
    UNION ALL SELECT 'model_metrics', COUNT(*) FROM model_metrics
    UNION ALL SELECT 'users', COUNT(*) FROM users`
  console.log('✓ 完成：')
  for (const r of counts) console.log(`   ${r.t.padEnd(18)} ${r.n}`)
} catch (e) {
  console.error('✗ Seed 失敗：', e.message)
  process.exitCode = 1
} finally {
  await sql.end({ timeout: 5 })
}
