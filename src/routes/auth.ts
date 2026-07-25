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
} from '../lib/auth'

const auth = new Hono<{ Bindings: AppBindings }>()

function secretOf(env: AppBindings) {
  return env.SESSION_SECRET || 'dev-insecure-secret-change-me'
}

auth.post('/register', async (c) => {
  const db = await getDb(c.env)
  const body = await c.req.json().catch(() => ({}))
  const email = String((body as any).email ?? '').trim()
  const password = String((body as any).password ?? '')
  const displayName = (body as any).display_name ? String((body as any).display_name) : undefined

  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email))
    return c.json({ error: 'email 格式不正確' }, 400)
  if (password.length < 8) return c.json({ error: '密碼至少 8 個字元' }, 400)

  const user = await registerUser(db, email, password, displayName)
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
