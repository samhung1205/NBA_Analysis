#!/usr/bin/env node
/**
 * 正式環境登入防護單元測試（不需要資料庫 / wrangler）
 * 用法：npm run test:auth   （Node ≥ 22.6，使用 --experimental-strip-types 直接載入 TS）
 */
import assert from 'node:assert/strict'

const { resolveSessionSecret, registrationAllowed, createSessionToken, readSessionToken } = await import(
  '../src/lib/auth.ts'
)
const DEV = 'dev-insecure-secret-change-me'
const GOOD = 'x'.repeat(44)
let n = 0
const t = (name, fn) => (fn(), console.log(`  ✓ ${name}`), n++)

t('sandbox (D1, no DATABASE_URL) keeps dev fallback', () => {
  assert.equal(resolveSessionSecret({}), DEV)
  assert.equal(resolveSessionSecret({ SESSION_SECRET: 'abc' }), 'abc')
})
t('production (DATABASE_URL set) requires a strong SESSION_SECRET', () => {
  assert.throws(() => resolveSessionSecret({ DATABASE_URL: 'postgres://x' }))
  assert.throws(() => resolveSessionSecret({ DATABASE_URL: 'postgres://x', SESSION_SECRET: '' }))
  assert.throws(() => resolveSessionSecret({ DATABASE_URL: 'postgres://x', SESSION_SECRET: 'short' }))
  assert.throws(() => resolveSessionSecret({ DATABASE_URL: 'postgres://x', SESSION_SECRET: DEV }))
  assert.equal(resolveSessionSecret({ DATABASE_URL: 'postgres://x', SESSION_SECRET: GOOD }), GOOD)
})
t('registration: open in sandbox; production only bootstrap or explicit flag', () => {
  assert.equal(registrationAllowed('d1', undefined, 5), true)
  assert.equal(registrationAllowed('postgres', undefined, 0), true)
  assert.equal(registrationAllowed('postgres', undefined, 2), false)
  assert.equal(registrationAllowed('postgres', 'false', 2), false)
  assert.equal(registrationAllowed('postgres', 'true', 2), true)
})
t('a token signed with the public dev secret is rejected by a production secret', async () => {
  const forged = await createSessionToken(DEV, 1, 'a@b.c')
  assert.equal(await readSessionToken(GOOD, forged), null)
  assert.ok(await readSessionToken(GOOD, await createSessionToken(GOOD, 1, 'a@b.c')))
})
// 非同步 t 不會 await；再補一次同步等待確認
const forged = await createSessionToken(DEV, 1, 'a@b.c')
assert.equal(await readSessionToken(GOOD, forged), null)
console.log(`\n${n} passed`)
