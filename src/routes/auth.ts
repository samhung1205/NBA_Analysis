/**
 * 驗證路由 (規格書 §3.2)
 * 個人使用：email + password 登入，保護 bets / 績效頁。
 */

import { Hono } from 'hono'
import type { AppBindings } from '../db'
import { getDb } from '../db'
import {
  authenticate,
  registerUser,
  createSessionToken,
  setSessionCookie,
  clearSessionCookie,
  getCurrentUser,
  resolveSessionSecret,
  registrationAllowed,
  MIN_REGISTER_PASSWORD_LENGTH,
} from '../lib/auth'

const auth = new Hono<{ Bindings: AppBindings }>()

function secretOf(env: AppBindings) {
  return resolveSessionSecret(env)
}

auth.post('/register', async (c) => {
  const db = await getDb(c.env)
  // 正式環境（Postgres）是個人使用：只有「還沒有任何使用者」或明確設定 ALLOW_REGISTRATION=true 才開放註冊，
  // 避免陌生人建立帳號並被 decision board 物化流程納入。
  if (db.driver === 'postgres' && (c.env as any).ALLOW_REGISTRATION !== 'true') {
    const row = await db.one<{ n: number | string }>('SELECT COUNT(*) AS n FROM users')
    if (!registrationAllowed(db.driver, (c.env as any).ALLOW_REGISTRATION, Number(row?.n ?? 0)))
      return c.json({ error: 'registration_closed', message: '此網站未開放註冊' }, 403)
  }
  const body = await c.req.json().catch(() => ({}))
  const email = String((body as any).email ?? '').trim()
  const password = String((body as any).password ?? '')
  const displayName = (body as any).display_name ? String((body as any).display_name) : undefined

  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email))
    return c.json({ error: 'email 格式不正確' }, 400)
  if (password.length < MIN_REGISTER_PASSWORD_LENGTH)
    return c.json({ error: `密碼至少 ${MIN_REGISTER_PASSWORD_LENGTH} 個字元` }, 400)

  let user
  try {
    user = await registerUser(db, email, password, displayName)
  } catch (e) {
    // 例如 runtime 的 WebCrypto 限制：只在伺服器記錄，瀏覽器只拿到通用訊息（不洩漏 crypto / 資料庫細節）
    console.error('[auth] register failed:', e)
    return c.json({ error: 'internal_error', message: '無法建立帳號，請稍後再試' }, 500)
  }
  if (!user) return c.json({ error: '此 email 已註冊' }, 409)

  const token = await createSessionToken(secretOf(c.env), user.id, user.email)
  setSessionCookie(c, token)
  return c.json({ ok: true, user: { id: user.id, email: user.email } }, 201)
})

auth.post('/login', async (c) => {
  const db = await getDb(c.env)
  const body = await c.req.json().catch(() => ({}))
  const email = String((body as any).email ?? '').trim()
  const password = String((body as any).password ?? '')

  const user = await authenticate(db, email, password)
  if (!user) return c.json({ error: 'email 或密碼錯誤' }, 401)

  const token = await createSessionToken(secretOf(c.env), user.id, user.email)
  setSessionCookie(c, token)
  return c.json({ ok: true, user: { id: user.id, email: user.email } })
})

auth.post('/logout', (c) => {
  clearSessionCookie(c)
  return c.json({ ok: true })
})

auth.get('/me', async (c) => {
  const user = await getCurrentUser(c)
  if (!user) return c.json({ authenticated: false }, 200)
  return c.json({ authenticated: true, user })
})

export default auth
