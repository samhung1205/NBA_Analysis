import { Hono } from 'hono'
import type { AppBindings, Db } from '../db'
import { getDb } from '../db'
import { getCurrentUser } from '../lib/auth'
import { addDays, todayTpe } from '../lib/time'
import { ACTIVE_SYNC, boundedJson, shapeSyncJob, validateCapture } from '../lib/twsport-sync'

const sync = new Hono<{ Bindings: AppBindings }>()
const iso = () => new Date().toISOString()
const requestId = (v: any) => typeof v === 'string' && /^[\w:-]{8,100}$/.test(v)
async function schemaReady(db: Db) {
  try { await db.all('SELECT id FROM browser_sync_jobs WHERE 1 = 0'); return true } catch { return false }
}
async function expire(db: Db) {
  await db.run(`UPDATE browser_sync_jobs SET state = 'failed', payload = NULL, error_code = 'expired', finished_at = ?
    WHERE state IN ${ACTIVE_SYNC} AND expires_at < ?`, [iso(), iso()])
}
async function workerReady(db: Db) {
  const row = await db.one<any>("SELECT * FROM data_sources WHERE source_key = 'browser_sync'")
  return row?.last_status === 'ok' && Date.now() - new Date(row.last_attempt_at).getTime() < 120000
}

sync.use('*', async (c, next) => {
  if (!/^\/twsport-sync(?:\/|$)/.test(c.req.path.replace(/^\/api/, ''))) return next()
  c.header('Cache-Control', 'no-store')
  if (!(await getCurrentUser(c))) return c.json({ error: 'unauthorized', message: '請先登入再同步台彩盤口' }, 401)
  if (c.req.method === 'POST') {
    if (c.req.header('origin') !== new URL(c.req.url).origin)
      return c.json({ error: 'origin_rejected' }, 403)
    if (!c.req.header('content-type')?.toLowerCase().startsWith('application/json'))
      return c.json({ error: 'json_required' }, 415)
  }
  await next()
})

sync.get('/twsport-sync', async (c) => {
  if (c.env.TWSPORT_SYNC_ENABLED !== 'true') return c.json({ enabled: false, reason: 'not_enabled' })
  const user = (await getCurrentUser(c))!
  const db = await getDb(c.env)
  if (!(await schemaReady(db))) return c.json({ enabled: false, reason: 'migration_required' })
  await expire(db)
  const id = c.req.query('id')
  const row = id
    ? await db.one<any>('SELECT * FROM browser_sync_jobs WHERE id = ? AND user_id = ?', [id, user.uid])
    : await db.one<any>('SELECT * FROM browser_sync_jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1', [user.uid])
  if (id && !row) return c.json({ error: 'not_found' }, 404)
  const extensionId = /^[a-p]{32}$/.test(c.env.TWSPORT_EXTENSION_ID || '') ? c.env.TWSPORT_EXTENSION_ID : null
  const ready = await workerReady(db)
  return c.json({ enabled: !!extensionId && ready,
    reason: !extensionId ? 'extension_not_configured' : ready ? null : 'worker_unavailable',
    extension_id: extensionId, job: shapeSyncJob(row) })
})

sync.post('/twsport-sync', async (c) => {
  if (c.env.TWSPORT_SYNC_ENABLED !== 'true') return c.json({ error: 'not_enabled' }, 503)
  const user = (await getCurrentUser(c))!
  const db = await getDb(c.env)
  if (!(await schemaReady(db))) return c.json({ error: 'migration_required' }, 503)
  let body: any
  try { body = await boundedJson(c.req.raw) } catch { return c.json({ error: 'invalid_request' }, 400) }
  if (!body || typeof body !== 'object') return c.json({ error: 'invalid_request' }, 400)
  if (!requestId(body.client_request_id)) return c.json({ error: 'invalid_request_id' }, 400)
  const replay = await db.one<any>('SELECT * FROM browser_sync_jobs WHERE user_id = ? AND client_request_id = ?', [user.uid, body.client_request_id])
  if (replay) return c.json({ job: shapeSyncJob(replay), replay: true })
  const day = String(body.betting_day ?? '')
  if (![todayTpe(), addDays(todayTpe(), 1), addDays(todayTpe(), 2)].includes(day)) return c.json({ error: 'date_outside_window' }, 400)
  if (!(await workerReady(db))) return c.json({ error: 'worker_unavailable' }, 503)
  await expire(db)
  const recent = await db.one<any>('SELECT created_at FROM browser_sync_jobs ORDER BY created_at DESC LIMIT 1')
  if (recent && Date.now() - new Date(recent.created_at).getTime() < 30000)
    return c.json({ error: 'cooldown', message: '請等待 30 秒再同步' }, 429)
  const id = crypto.randomUUID(), now = iso()
  try {
    await db.run(`INSERT INTO browser_sync_jobs (id, user_id, client_request_id, betting_day, state, created_at, expires_at)
      VALUES (?, ?, ?, ?, 'capturing', ?, ?)`, [id, user.uid, body.client_request_id, day, now, new Date(Date.now() + 5 * 60000).toISOString()])
  } catch { return c.json({ error: 'sync_busy', message: '已有同步工作正在進行，請稍後再試' }, 409) }
  return c.json({ job: shapeSyncJob(await db.one('SELECT * FROM browser_sync_jobs WHERE id = ?', [id])) }, 201)
})

sync.post('/twsport-sync/:id/capture', async (c) => {
  if (c.env.TWSPORT_SYNC_ENABLED !== 'true') return c.json({ error: 'not_enabled' }, 503)
  const user = (await getCurrentUser(c))!, db = await getDb(c.env)
  if (!(await schemaReady(db))) return c.json({ error: 'migration_required' }, 503)
  await expire(db)
  const job = await db.one<any>('SELECT * FROM browser_sync_jobs WHERE id = ? AND user_id = ?', [c.req.param('id'), user.uid])
  if (!job) return c.json({ error: 'not_found' }, 404)
  if (job.state !== 'capturing') return c.json({ job: shapeSyncJob(job), replay: true })
  let payload: any
  try { payload = validateCapture(await boundedJson(c.req.raw), job, Date.now()) }
  catch (e) { const code = (e as Error).message; return c.json({ error: code }, code === 'capture_too_large' ? 413 : 400) }
  // Conditional transition also makes concurrent/retried uploads idempotent.
  await db.run(`UPDATE browser_sync_jobs SET payload = ?, received_at = ?, state = 'queued'
    WHERE id = ? AND user_id = ? AND state = 'capturing'`, [JSON.stringify(payload), iso(), job.id, user.uid])
  return c.json({ job: shapeSyncJob(await db.one('SELECT * FROM browser_sync_jobs WHERE id = ?', [job.id])) }, 202)
})

sync.post('/twsport-sync/:id/cancel', async (c) => {
  if (c.env.TWSPORT_SYNC_ENABLED !== 'true') return c.json({ error: 'not_enabled' }, 503)
  const user = (await getCurrentUser(c))!, db = await getDb(c.env)
  if (!(await schemaReady(db))) return c.json({ error: 'migration_required' }, 503)
  // No arbitrary exception strings supplied by the extension are stored or echoed.
  await db.run(`UPDATE browser_sync_jobs SET state = 'failed', payload = NULL, error_code = 'capture_cancelled', finished_at = ?
    WHERE id = ? AND user_id = ? AND state = 'capturing'`, [iso(), c.req.param('id'), user.uid])
  return c.json({ ok: true })
})
export default sync
