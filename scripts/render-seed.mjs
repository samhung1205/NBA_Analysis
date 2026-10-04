#!/usr/bin/env node
/**
 * Seed 產生器
 * ------------------------------------------------------------
 * 將 seed/seed.template.sql 的時間 token 展開為固定的 ISO8601 UTC 字串，
 * 輸出 seed/seed.generated.sql。
 *
 * 好處：產生出的 SQL 不含任何資料庫方言特有的日期函式，
 *       同一份檔案可直接餵給 D1(SQLite) 與 Postgres，
 *       避免「兩份 seed 不同步」或「跨方言翻譯」的維護風險。
 *
 * 支援的 token：
 *   {{NOW:-30m}} / {{NOW:-2h}} / {{NOW:-3d}}   相對現在的時間
 *   {{TPE:+1 08:00}}                           台灣時間「明天 08:00」→ UTC
 *   {{TPE:+0 10:00}}                           台灣時間「今天 10:00」
 *   {{TPE:-3 09:00}}                           台灣時間「3 天前 09:00」
 *   {{TPEDATE:+1}}                             台灣日期「明天」→ 'YYYY-MM-DD'（D.3 betting_day）
 *
 * 用法：node scripts/render-seed.mjs [輸出路徑]
 */

import { readFile, writeFile } from 'node:fs/promises'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const TEMPLATE = join(__dirname, '..', 'seed', 'seed.template.sql')
const DEFAULT_OUT = join(__dirname, '..', 'seed', 'seed.generated.sql')

const TPE_OFFSET_MS = 8 * 60 * 60 * 1000
const UNIT_MS = { m: 60_000, h: 3_600_000, d: 86_400_000 }

const iso = (ms) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')

/** 展開所有時間 token */
export function renderSeed(template, now = new Date()) {
  const nowMs = now.getTime()

  let out = template.replace(/\{\{NOW:([+-])(\d+)([mhd])\}\}/g, (_m, sign, n, unit) => {
    const delta = Number(n) * UNIT_MS[unit] * (sign === '-' ? -1 : 1)
    return iso(nowMs + delta)
  })

  out = out.replace(/\{\{TPE:([+-]\d+)\s+(\d{2}):(\d{2})\}\}/g, (_m, dayOffset, hh, mm) => {
    // 先換算出「現在」對應的台灣日期，再套上指定的台灣時刻，最後轉回 UTC
    const tpeNow = new Date(nowMs + TPE_OFFSET_MS)
    const tpeMidnightUtcMs = Date.UTC(
      tpeNow.getUTCFullYear(),
      tpeNow.getUTCMonth(),
      tpeNow.getUTCDate() + Number(dayOffset),
      Number(hh),
      Number(mm),
      0
    )
    // tpeMidnightUtcMs 目前是「把台灣時刻當成 UTC」，減去 8 小時得到真正的 UTC
    return iso(tpeMidnightUtcMs - TPE_OFFSET_MS)
  })

  out = out.replace(/\{\{TPEDATE:([+-]\d+)\}\}/g, (_m, dayOffset) => {
    const tpeNow = new Date(nowMs + TPE_OFFSET_MS)
    const d = new Date(Date.UTC(tpeNow.getUTCFullYear(), tpeNow.getUTCMonth(), tpeNow.getUTCDate() + Number(dayOffset)))
    return d.toISOString().slice(0, 10)
  })

  const leftover = out.match(/\{\{[^}]+\}\}/g)
  if (leftover) {
    throw new Error(`樣板中有無法辨識的 token：${[...new Set(leftover)].join(', ')}`)
  }
  return out
}

// 直接執行時：讀樣板 → 渲染 → 寫檔
if (import.meta.url === `file://${process.argv[1]}`) {
  const outPath = process.argv[2] || DEFAULT_OUT
  const template = await readFile(TEMPLATE, 'utf8')
  const rendered = renderSeed(template)
  const banner =
    `-- ⚠️ 此檔由 scripts/render-seed.mjs 自動產生，請勿手動編輯。\n` +
    `-- 來源樣板：seed/seed.template.sql\n` +
    `-- 產生時間：${new Date().toISOString()}\n\n`
  await writeFile(outPath, banner + rendered, 'utf8')
  console.log(`✓ 已產生 ${outPath}`)
}
