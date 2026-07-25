#!/usr/bin/env node
/**
 * 對正式 Postgres (Supabase / Railway) 套用 schema
 * ------------------------------------------------------------
 * 用法：
 *   DATABASE_URL="postgresql://..." npm run db:migrate:pg
 *
 * 這支腳本會依序執行 migrations/postgres/*.sql，並以
 * schema_migrations 表記錄已套用的檔案，可重複執行。
 * 階段二的 Python 服務連同一個 DATABASE_URL 即可直接使用這些表。
 */

import { readdir, readFile } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import postgres from 'postgres'

const __dirname = dirname(fileURLToPath(import.meta.url))
const MIG_DIR = join(__dirname, '..', 'migrations', 'postgres')

const url = process.env.DATABASE_URL
if (!url) {
  console.error('✗ 缺少環境變數 DATABASE_URL')
  console.error('  例：DATABASE_URL="postgresql://user:pass@host:5432/postgres" npm run db:migrate:pg')
  process.exit(1)
}

const sql = postgres(url, { max: 1, prepare: false, onnotice: () => {} })

try {
  await sql`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      filename   TEXT PRIMARY KEY,
      applied_at TIMESTAMPTZ DEFAULT NOW()
    )`
  const applied = new Set(
    (await sql`SELECT filename FROM schema_migrations`).map((r) => r.filename)
  )

  const files = (await readdir(MIG_DIR)).filter((f) => f.endsWith('.sql')).sort()
  let count = 0
  for (const f of files) {
    if (applied.has(f)) {
      console.log(`· 已套用，略過 ${f}`)
      continue
    }
    const text = await readFile(join(MIG_DIR, f), 'utf8')
    console.log(`→ 套用 ${f} …`)
    await sql.unsafe(text)
    await sql`INSERT INTO schema_migrations (filename) VALUES (${f})`
    count++
  }
  console.log(count ? `✓ 完成，套用 ${count} 個 migration` : '✓ 資料庫已是最新狀態')
} catch (e) {
  console.error('✗ Migration 失敗：', e.message)
  process.exitCode = 1
} finally {
  await sql.end({ timeout: 5 })
}
