/**
 * 驗證機制 (規格書 §3.2)
 * ------------------------------------------------------------
 * 個人登入 email/password，保護 bets / 績效等私人資料。
 * 全部使用 Web Crypto API（Cloudflare Workers 可用，無 Node.js 依賴）：
 *   - 密碼：PBKDF2-SHA256 (100,000 iterations) + 16-byte random salt
 *     ⚠️ Cloudflare WebCrypto 目前最多只支援 100,000 次（超過會丟 NotSupportedError），所以新雜湊使用 runtime 上限；
 *        這「不等同」210,000 次的強度，由註冊密碼最短 12 字元補強（私人單人站台）。驗證時迭代次數仍讀自儲存的雜湊。
 *   - Session：HMAC-SHA256 簽章的 cookie，無需額外儲存
 */

import type { Context } from 'hono'
import { getCookie, setCookie, deleteCookie } from 'hono/cookie'
import type { AppBindings, Db } from '../db'

export const PBKDF2_ITERATIONS = 100_000
/** Cloudflare WebCrypto PBKDF2 的迭代上限；儲存的雜湊要求更多次 → 本 runtime 無法驗證 → 安全地視為驗證失敗 */
export const MAX_SUPPORTED_ITERATIONS = 100_000
export const MIN_REGISTER_PASSWORD_LENGTH = 12
const SESSION_COOKIE = 'nba_session'
const SESSION_TTL_SEC = 60 * 60 * 24 * 14 // 14 天

const enc = new TextEncoder()

const DEV_SECRET = 'dev-insecure-secret-change-me'
const MIN_SECRET_LEN = 32

/**
 * Session 簽章金鑰。
 * 正式環境（已設 DATABASE_URL = 使用 Postgres）必須有真正的隨機 SESSION_SECRET：
 * 缺少、長度不足或等於開發預設值 → 丟錯（fail closed），避免任何人用公開的預設值偽造登入 cookie。
 * 沙盒（D1）仍沿用開發預設值，維持本機開發與 smoke test 的行為。
 */
export function resolveSessionSecret(env: Pick<AppBindings, 'SESSION_SECRET' | 'DATABASE_URL'> | undefined): string {
  const secret = env?.SESSION_SECRET
  if (env?.DATABASE_URL) {
    if (!secret || secret === DEV_SECRET || secret.length < MIN_SECRET_LEN) {
      throw new Error(
        `SESSION_SECRET 未設定或過弱：正式環境需要至少 ${MIN_SECRET_LEN} 字元的隨機字串（openssl rand -base64 32）`
      )
    }
    return secret
  }
  return secret || DEV_SECRET
}

function b64encode(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf)
  let s = ''
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s)
}

function b64decode(str: string): Uint8Array {
  const bin = atob(str)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

/**
 * 是否允許註冊：沙盒（D1）一律允許；正式環境（Postgres）只有「還沒有任何使用者」或明確 ALLOW_REGISTRATION=true 才允許。
 */
export function registrationAllowed(driver: 'postgres' | 'd1', allowFlag: string | undefined, userCount: number): boolean {
  if (driver !== 'postgres') return true
  return allowFlag === 'true' || userCount === 0
}

/* ----------------------------- 密碼雜湊 ----------------------------- */

export async function hashPassword(password: string): Promise<string> {
  const salt = crypto.getRandomValues(new Uint8Array(16))
  const bits = await deriveBits(password, salt, PBKDF2_ITERATIONS)
  return `pbkdf2$${PBKDF2_ITERATIONS}$${b64encode(salt)}$${b64encode(bits)}`
}

/**
 * 驗證密碼。任何不支援 / 格式錯誤的儲存雜湊都 fail closed（回 false），絕不丟出 WebCrypto 例外。
 * 迭代次數讀自儲存的雜湊（不寫死 100,000）；超過 runtime 上限或不是合理的正整數 → false。
 * 只在伺服器 log 記「有一筆雜湊不被支援」，不記 email / 雜湊內容 / 密碼。
 */
export async function verifyPassword(password: string, stored: string): Promise<boolean> {
  try {
    const parts = typeof stored === 'string' ? stored.split('$') : []
    if (parts.length !== 4 || parts[0] !== 'pbkdf2') return false
    const [, iterStr, saltB64, hashB64] = parts
    if (!/^\d{1,9}$/.test(iterStr)) return false
    const iterations = parseInt(iterStr, 10)
    if (iterations < 1) return false
    if (iterations > MAX_SUPPORTED_ITERATIONS) {
      console.warn(`[auth] stored password hash requests ${iterations} PBKDF2 iterations; runtime max is ${MAX_SUPPORTED_ITERATIONS} → login refused`)
      return false
    }
    const bits = await deriveBits(password, b64decode(saltB64), iterations)
    return timingSafeEqual(new Uint8Array(bits), b64decode(hashB64))
  } catch (e) {
    console.warn('[auth] password verification failed closed:', e instanceof Error ? e.name : 'error')
    return false
  }
}

async function deriveBits(password: string, salt: Uint8Array, iterations: number) {
  const key = await crypto.subtle.importKey('raw', enc.encode(password), 'PBKDF2', false, [
    'deriveBits',
  ])
  return crypto.subtle.deriveBits(
    { name: 'PBKDF2', salt: salt as unknown as BufferSource, iterations, hash: 'SHA-256' },
    key,
    256
  )
}

function timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false
  let diff = 0
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i]
  return diff === 0
}

/* ----------------------------- Session ----------------------------- */

type SessionPayload = { uid: number; email: string; exp: number }

async function hmac(secret: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    'raw',
    enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign']
  )
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(data))
  return b64encode(sig).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export async function createSessionToken(
  secret: string,
  uid: number,
  email: string
): Promise<string> {
  const payload: SessionPayload = {
    uid,
    email,
    exp: Math.floor(Date.now() / 1000) + SESSION_TTL_SEC,
  }
  const body = b64encode(enc.encode(JSON.stringify(payload)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  return `${body}.${await hmac(secret, body)}`
}

export async function readSessionToken(
  secret: string,
  token: string
): Promise<SessionPayload | null> {
  const [body, sig] = token.split('.')
  if (!body || !sig) return null
  if (!timingSafeEqual(enc.encode(await hmac(secret, body)), enc.encode(sig))) return null
  try {
    const json = new TextDecoder().decode(
      b64decode(body.replace(/-/g, '+').replace(/_/g, '/'))
    )
    const payload = JSON.parse(json) as SessionPayload
    if (payload.exp < Math.floor(Date.now() / 1000)) return null
    return payload
  } catch {
    return null
  }
}

export function setSessionCookie(c: Context, token: string) {
  setCookie(c, SESSION_COOKIE, token, {
    httpOnly: true,
    secure: new URL(c.req.url).protocol === 'https:',
    sameSite: 'Lax',
    path: '/',
    maxAge: SESSION_TTL_SEC,
  })
}

export function clearSessionCookie(c: Context) {
  deleteCookie(c, SESSION_COOKIE, { path: '/' })
}

export async function getCurrentUser(
  c: Context
): Promise<{ uid: number; email: string } | null> {
  const token = getCookie(c, SESSION_COOKIE)
  if (!token) return null
  const secret = resolveSessionSecret(c.env as any)
  const payload = await readSessionToken(secret, token)
  return payload ? { uid: payload.uid, email: payload.email } : null
}

/** 註冊使用者（重複 email 回傳 null） */
export async function registerUser(
  db: Db,
  email: string,
  password: string,
  displayName?: string
): Promise<{ id: number; email: string } | null> {
  const existing = await db.one<{ id: number }>('SELECT id FROM users WHERE email = ?', [
    email.toLowerCase(),
  ])
  if (existing) return null
  const hash = await hashPassword(password)
  const isPg = db.driver === 'postgres'
  const sql = isPg
    ? 'INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?) RETURNING id'
    : 'INSERT INTO users (email, password_hash, display_name) VALUES (?, ?, ?)'
  const res = await db.run(sql, [email.toLowerCase(), hash, displayName ?? null])
  const id = Number(res.lastInsertId)
  return { id, email: email.toLowerCase() }
}

/** 驗證登入 */
export async function authenticate(
  db: Db,
  email: string,
  password: string
): Promise<{ id: number; email: string } | null> {
  const user = await db.one<{ id: number; email: string; password_hash: string }>(
    'SELECT id, email, password_hash FROM users WHERE email = ?',
    [email.toLowerCase()]
  )
  if (!user) return null
  const ok = await verifyPassword(password, user.password_hash)
  return ok ? { id: user.id, email: user.email } : null
}
