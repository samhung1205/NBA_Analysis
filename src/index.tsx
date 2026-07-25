/**
 * NBA 對戰預測平台 — 階段一（網站殼子）
 * ------------------------------------------------------------
 * Hono + Cloudflare Pages
 * - 前端頁面：src/pages
 * - API：    src/routes/api.ts（§3.3）
 * - 驗證：    src/routes/auth.ts（§3.2）
 * - 資料庫：  src/db（Postgres / D1 雙驅動，正式環境用 Postgres 供階段二共用）
 */

import { Hono } from 'hono'
import { cors } from 'hono/cors'
import { logger } from 'hono/logger'
import type { AppBindings } from './db'
import api from './routes/api'
import auth from './routes/auth'
import pages from './pages/index'

const app = new Hono<{ Bindings: AppBindings }>()

app.use('*', logger())
app.use('/api/*', cors({ origin: '*', credentials: true }))

// 健康檢查（部署與階段二排程器可用來確認服務存活）
app.get('/healthz', (c) => c.json({ ok: true, stage: 'stage-1', ts: new Date().toISOString() }))

app.route('/api/auth', auth)
app.route('/api', api)
app.route('/', pages)

// API 錯誤統一格式
app.onError((err, c) => {
  console.error('[error]', err)
  const isApi = new URL(c.req.url).pathname.startsWith('/api/')
  const message = err instanceof Error ? err.message : '伺服器錯誤'
  if (isApi) return c.json({ error: 'internal_error', message }, 500)
  return c.html(
    `<pre style="padding:2rem;font-family:monospace;background:#0f172a;color:#f87171">伺服器錯誤：${message}</pre>`,
    500
  )
})

app.notFound((c) => {
  const isApi = new URL(c.req.url).pathname.startsWith('/api/')
  if (isApi) return c.json({ error: 'not_found' }, 404)
  return c.html(
    `<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">
     <title>找不到頁面</title><script src="https://cdn.tailwindcss.com"></script></head>
     <body class="bg-slate-950 text-slate-300 flex items-center justify-center min-h-screen">
       <div class="text-center"><p class="text-5xl font-bold text-slate-700 mb-3">404</p>
       <p class="mb-4 text-sm">找不到這個頁面</p>
       <a href="/" class="text-orange-400 text-sm hover:underline">返回賽事總覽</a></div>
     </body></html>`,
    404
  )
})

export default app
