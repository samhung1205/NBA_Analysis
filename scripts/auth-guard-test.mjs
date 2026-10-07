#!/usr/bin/env node
/**
 * 正式環境登入防護單元測試（不需要資料庫 / wrangler）
 * 用法：npm run test:auth   （Node ≥ 22.6，使用 --experimental-strip-types 直接載入 TS）
 */
import assert from 'node:assert/strict'

const {
  resolveSessionSecret, registrationAllowed, createSessionToken, readSessionToken,
  hashPassword, verifyPassword, PBKDF2_ITERATIONS, MAX_SUPPORTED_ITERATIONS, MIN_REGISTER_PASSWORD_LENGTH,
} = await import('../src/lib/auth.ts')
const DEV = 'dev-insecure-secret-change-me'
const GOOD = 'x'.repeat(44)
let n = 0
const t = async (name, fn) => (await fn(), console.log(`  ✓ ${name}`), n++)

await t('sandbox (D1, no DATABASE_URL) keeps dev fallback', () => {
  assert.equal(resolveSessionSecret({}), DEV)
  assert.equal(resolveSessionSecret({ SESSION_SECRET: 'abc' }), 'abc')
})
await t('production (DATABASE_URL set) requires a strong SESSION_SECRET', () => {
  assert.throws(() => resolveSessionSecret({ DATABASE_URL: 'postgres://x' }))
  assert.throws(() => resolveSessionSecret({ DATABASE_URL: 'postgres://x', SESSION_SECRET: '' }))
  assert.throws(() => resolveSessionSecret({ DATABASE_URL: 'postgres://x', SESSION_SECRET: 'short' }))
  assert.throws(() => resolveSessionSecret({ DATABASE_URL: 'postgres://x', SESSION_SECRET: DEV }))
  assert.equal(resolveSessionSecret({ DATABASE_URL: 'postgres://x', SESSION_SECRET: GOOD }), GOOD)
})
await t('registration: open in sandbox; production only bootstrap or explicit flag', () => {
  assert.equal(registrationAllowed('d1', undefined, 5), true)
  assert.equal(registrationAllowed('postgres', undefined, 0), true)
  assert.equal(registrationAllowed('postgres', undefined, 2), false)
  assert.equal(registrationAllowed('postgres', 'false', 2), false)
  assert.equal(registrationAllowed('postgres', 'true', 2), true)
})
await t('a token signed with the public dev secret is rejected by a production secret', async () => {
  const forged = await createSessionToken(DEV, 1, 'a@b.c')
  assert.equal(await readSessionToken(GOOD, forged), null)
  assert.ok(await readSessionToken(GOOD, await createSessionToken(GOOD, 1, 'a@b.c')))
})

/* ---------------- PBKDF2：Cloudflare WebCrypto 上限 100,000 ---------------- */
const PW = 'correct horse battery'

await t('new hashes use exactly 100000 iterations in the stored format', async () => {
  assert.equal(PBKDF2_ITERATIONS, 100_000)
  const h = await hashPassword(PW)
  const [scheme, iter, salt, hash] = h.split('$')
  assert.equal(scheme, 'pbkdf2')
  assert.equal(iter, '100000')
  assert.equal(Buffer.from(salt, 'base64').length, 16)
  assert.equal(Buffer.from(hash, 'base64').length, 32)           // 256-bit derived key
  assert.notEqual(h, await hashPassword(PW))                       // 隨機 salt
})
await t('correct password verifies, wrong password fails', async () => {
  const h = await hashPassword(PW)
  assert.equal(await verifyPassword(PW, h), true)
  assert.equal(await verifyPassword(PW + 'x', h), false)
})
await t('verification reads iterations from the stored hash (not hard-coded)', async () => {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(PW), 'PBKDF2', false, ['deriveBits'])
  const salt = crypto.getRandomValues(new Uint8Array(16))
  const bits = new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', salt, iterations: 50_000, hash: 'SHA-256' }, key, 256))
  const stored = `pbkdf2$50000$${Buffer.from(salt).toString('base64')}$${Buffer.from(bits).toString('base64')}`
  assert.equal(await verifyPassword(PW, stored), true)
  assert.equal(await verifyPassword(PW, stored.replace('$50000$', '$100000$')), false)
})
await t('malformed stored hashes fail closed without throwing', async () => {
  const warn0 = console.warn
  console.warn = () => {}
  try {
  for (const bad of ['', 'x', 'pbkdf2', 'pbkdf2$100000$only', 'bcrypt$10$a$b', 'pbkdf2$abc$AAAA$AAAA',
    'pbkdf2$0$AAAA$AAAA', 'pbkdf2$-5$AAAA$AAAA', 'pbkdf2$100000$!!!!$!!!!', 'pbkdf2$100000$AAAA$AAAA$extra', null, undefined]) {
    assert.equal(await verifyPassword(PW, bad), false, String(bad))
  }
  } finally { console.warn = warn0 }
})
await t('stored hash above the Cloudflare cap (e.g. legacy 210000) fails safely', async () => {
  const warn = console.warn
  const warned = []
  console.warn = (...a) => warned.push(a.join(' '))
  try {
    assert.equal(MAX_SUPPORTED_ITERATIONS, 100_000)
    const legacy = 'pbkdf2$210000$aM/7vl/sgW2Sllr61lDirQ==$mhclR2wcId1f3yv27DPMpO4/NwcRiRBN5kQr/Y5k8z4='
    assert.equal(await verifyPassword('nba12345678', legacy), false)     // 密碼正確也不驗證（本 runtime 無法執行）
    assert.equal(await verifyPassword(PW, `pbkdf2$100001$AAAA$AAAA`), false)
  } finally { console.warn = warn }
  assert.ok(warned.length === 2 && !warned.join(' ').includes('nba12345678') && !warned.join(' ').includes('mhclR2wc'))
})
await t('a runtime WebCrypto exception during verification is swallowed (fail closed)', async () => {
  const h = await hashPassword(PW)
  const orig = crypto.subtle.deriveBits.bind(crypto.subtle)
  const warn = console.warn
  console.warn = () => {}
  crypto.subtle.deriveBits = async () => { throw new DOMException('Pbkdf2 failed', 'NotSupportedError') }
  try { assert.equal(await verifyPassword(PW, h), false) } finally { crypto.subtle.deriveBits = orig; console.warn = warn }
})

/* ---------------- 註冊路由：密碼長度 / 錯誤處理 / 正式環境閘門 ---------------- */
const { default: authRoutes } = await import('../src/routes/auth.ts')

function fakeD1() {
  const users = []
  const stmt = (sql) => ({
    args: [],
    bind(...a) { this.args = a; return this },
    async all() {
      if (/FROM users WHERE email/.test(sql)) return { results: users.filter((u) => u.email === this.args[0]) }
      return { results: [] }
    },
    async run() {
      if (/INSERT INTO users/.test(sql)) { users.push({ id: users.length + 1, email: this.args[0], hash: this.args[1] }); return { meta: { last_row_id: users.length } } }
      return { meta: {} }
    },
  })
  return { users, prepare: stmt }
}
const post = (env, body) =>
  authRoutes.request('/register', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }, env)

await t('registration rejects passwords shorter than 12 characters (400)', async () => {
  assert.equal(MIN_REGISTER_PASSWORD_LENGTH, 12)
  const d1 = fakeD1()
  for (const pw of ['', 'short', '12345678', 'elevenchars']) {
    const r = await post({ DB: d1 }, { email: 'a@b.co', password: pw })
    assert.equal(r.status, 400, pw)
    assert.match((await r.json()).error, /12/)
  }
  assert.equal(d1.users.length, 0)
})
await t('registration accepts a 12+ character password, stores a 100000-iteration hash, sets the session cookie', async () => {
  const d1 = fakeD1()
  const r = await post({ DB: d1 }, { email: 'Me@Example.com', password: 'exactly12chr' })   // 剛好 12
  assert.equal(r.status, 201)
  assert.equal(d1.users.length, 1)
  assert.match(d1.users[0].hash, /^pbkdf2\$100000\$/)
  const cookie = r.headers.get('set-cookie')
  assert.match(cookie, /nba_session=/); assert.match(cookie, /HttpOnly/i); assert.match(cookie, /SameSite=Lax/i)
  assert.match(cookie, /Max-Age=1209600/)                                                   // 14 天不變
})
await t('a crypto/runtime failure during registration returns a generic 500 (no raw exception text)', async () => {
  const orig = crypto.subtle.deriveBits.bind(crypto.subtle)
  const err = console.error
  console.error = () => {}
  crypto.subtle.deriveBits = async () => { throw new DOMException('Pbkdf2 failed: iteration counts above 100000 are not supported', 'NotSupportedError') }
  try {
    const r = await post({ DB: fakeD1() }, { email: 'a@b.co', password: 'a-long-enough-password' })
    assert.equal(r.status, 500)
    const txt = JSON.stringify(await r.json())
    assert.ok(!/pbkdf2|iteration|NotSupported/i.test(txt), txt)
  } finally { crypto.subtle.deriveBits = orig; console.error = err }
})
await t('session token signature / expiry behavior is unchanged', async () => {
  const tok = await createSessionToken(GOOD, 7, 'x@y.z')
  const p = await readSessionToken(GOOD, tok)
  assert.equal(p.uid, 7)
  assert.ok(Math.abs((p.exp - Math.floor(Date.now() / 1000)) - 14 * 86400) < 5)
  assert.equal(await readSessionToken('y'.repeat(44), tok), null)
  const [body, sig] = tok.split('.')
  assert.equal(await readSessionToken(GOOD, body + '.' + sig.slice(0, -2) + 'AA'), null)
})

console.log(`\n${n} passed`)
