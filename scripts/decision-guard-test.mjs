/** D.5 regression tests: real SQLite schema/transactions + Hono routes, no network or production access.
 * Cloudflare runtime compatibility is covered separately by the isolated D1 API smoke suite.
 */
import assert from 'node:assert/strict'
import { DatabaseSync } from 'node:sqlite'
import { readFileSync, readdirSync } from 'node:fs'
import { createSessionToken } from '../src/lib/auth.ts'
import routes from '../src/routes/decision.ts'
import { renderSeed } from './render-seed.mjs'

let passed = 0
const root = new URL('../', import.meta.url)

function fixture() {
  const sql = new DatabaseSync(':memory:')
  sql.exec('PRAGMA foreign_keys = ON')
  const dir = new URL('migrations/d1/', root)
  for (const name of readdirSync(dir).filter(n => n.endsWith('.sql')).sort())
    sql.exec(readFileSync(new URL(name, dir), 'utf8'))
  sql.exec(renderSeed(readFileSync(new URL('seed/seed.template.sql', root), 'utf8')))
  const statement = (query, params = []) => ({
    bind(...args) { return statement(query, args) },
    async all() { return { results: sql.prepare(query).all(...params) } },
    execute() {
      const result = sql.prepare(query).run(...params)
      return { meta: { changes: Number(result.changes), last_row_id: Number(result.lastInsertRowid) } }
    },
    async run() { return this.execute() },
  })
  const binding = {
    prepare: statement,
    async batch(steps) {
      sql.exec('BEGIN IMMEDIATE')
      try {
        const results = steps.map(step => step.execute())
        sql.exec('COMMIT')
        return results
      } catch (error) { sql.exec('ROLLBACK'); throw error }
    },
  }
  const one = (query, ...args) => sql.prepare(query).get(...args)
  const user = one("SELECT * FROM users WHERE email = 'demo@example.com'")
  const account = one('SELECT * FROM bankroll_accounts WHERE user_id = ?', user.id)
  let uid = user.id
  const request = async (path, body) => {
    const token = await createSessionToken('decision-test-secret', uid, 'test@example.com')
    const response = await routes.request(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { cookie: `nba_session=${token}`, 'content-type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }, { DB: binding, SESSION_SECRET: 'decision-test-secret' })
    return { status: response.status, body: await response.json() }
  }
  function bet() {
    const game = one("SELECT * FROM games WHERE status = 'scheduled' ORDER BY date_utc LIMIT 1")
    const row = sql.prepare(`INSERT INTO bets
      (user_id, game_id, market, selection, odds, stake, account_id, origin, strategy_compliance,
       reference_context, override_reason, override_confirmed_at, model_target, model_threshold, comparator, settlement_rule)
      VALUES (?, ?, 'ml', 'home', 1.9, 392, ?, 'platform_opportunity', 'compliant', ?, 'old reason', ?,
              'margin', 0, 'gt', 'moneyline_ot_included')`)
      .run(user.id, game.id, account.id, JSON.stringify({ original: true }), new Date().toISOString())
    return Number(row.lastInsertRowid)
  }
  function bankroll({ age = 0, mismatch = null, materialized = true } = {}) {
    sql.prepare("INSERT INTO users (email, password_hash) VALUES ('boundary@example.com', 'unused')").run()
    uid = one("SELECT id FROM users WHERE email = 'boundary@example.com'").id
    sql.prepare('INSERT INTO bankroll_accounts (user_id) VALUES (?)').run(uid)
    const acc = one('SELECT * FROM bankroll_accounts WHERE user_id = ?', uid)
    sql.prepare("INSERT INTO bankroll_ledger (account_id, entry_type, amount, recorded_by, recorded_at) VALUES (?, 'initial_funding', 10000, 'user', ?)")
      .run(acc.id, new Date().toISOString())
    if (!materialized) return acc
    const snapshot = { ...one('SELECT * FROM decision_snapshots LIMIT 1') }
    delete snapshot.id
    const confirmed = new Date(Date.now() - age).toISOString()
    Object.assign(snapshot, {
      user_id: uid, account_id: acc.id, as_of: confirmed, last_confirmed_at: confirmed,
      risk_state_version: acc.risk_state_version,
      ledger_watermark: one('SELECT MAX(id) AS n FROM bankroll_ledger WHERE account_id = ?', acc.id).n,
      bet_event_watermark: 0, bankroll_day_snapshot_id: null,
      bankroll: JSON.stringify({ current_bankroll: 10000, available_bankroll: 10000 }),
    })
    if (mismatch) snapshot[mismatch] = 9999
    sql.prepare(`INSERT INTO decision_snapshots (${Object.keys(snapshot).join(',')})
      VALUES (${Object.keys(snapshot).map(() => '?').join(',')})`).run(...Object.values(snapshot))
    return acc
  }
  return { sql, one, account, request, bet, bankroll, close: () => sql.close() }
}

async function test(name, run) {
  const f = fixture()
  try { await run(f); passed++; console.log(`  ✓ ${name}`) } finally { f.close() }
}
const correction = { stake: 50000, reason: 'correct actual stake' }

await test('unconfirmed correction above the cap returns 409 without writes', async f => {
  const id = f.bet()
  const before = f.one('SELECT COUNT(*) AS n FROM bets').n
  const r = await f.request(`/bets/${id}/correction`, correction)
  assert.equal(r.status, 409)
  assert.equal(r.body.error, 'confirmation_required')
  assert.equal(r.body.compliance_if_confirmed, 'missing_context')
  assert.equal(f.one('SELECT COUNT(*) AS n FROM bets').n, before)
  assert.equal(f.one('SELECT record_status FROM bets WHERE id = ?', id).record_status, 'active')
  assert.equal(f.one('SELECT risk_state_version FROM bankroll_accounts WHERE id = ?', f.account.id).risk_state_version, f.account.risk_state_version)
})

await test('confirmed correction never inherits compliant or the previous confirmation', async f => {
  const id = f.bet()
  const r = await f.request(`/bets/${id}/correction`, { ...correction, confirm_override: true })
  assert.equal(r.status, 201)
  assert.equal(r.body.bet.stake, 50000)
  assert.equal(r.body.bet.strategy_compliance, 'missing_context')
  assert.equal(r.body.bet.override_reason, correction.reason)
  assert.equal(r.body.bet.risk_check.confirmed, true)
  assert.ok(r.body.bet.risk_check.checks.includes('correction_not_revalidated'))
  assert.equal(r.body.bet.risk_check.skipped, undefined)
  const old = f.one('SELECT * FROM bets WHERE id = ?', id)
  assert.equal(old.stake, 392)
  assert.equal(old.strategy_compliance, 'compliant')
  assert.equal(old.record_status, 'superseded')
  assert.equal(old.superseded_by_bet_id, r.body.id)
  assert.equal(f.one('SELECT COUNT(*) AS n FROM bet_events WHERE bet_id IN (?, ?)', id, r.body.id).n, 2)
})

await test('reducing stake also uses explicit unverified correction semantics', async f => {
  const id = f.bet()
  assert.equal((await f.request(`/bets/${id}/correction`, { stake: 100, reason: 'reduce' })).status, 409)
  const r = await f.request(`/bets/${id}/correction`, { stake: 100, reason: 'reduce', confirm_override: true })
  assert.equal(r.status, 201)
  assert.equal(r.body.bet.strategy_compliance, 'missing_context')
})

await test('changing side or line clears automatic settlement identity', async f => {
  const id = f.bet()
  const r = await f.request(`/bets/${id}/correction`, { ...correction, selection: 'away', line: 2.5, confirm_override: true })
  assert.equal(r.status, 201)
  assert.equal(r.body.bet.selection, 'away')
  for (const field of ['model_target', 'model_threshold', 'comparator', 'settlement_rule']) assert.equal(r.body.bet[field], null)
  assert.equal(r.body.bet.strategy_compliance, 'missing_context')
})

for (const [label, body] of [
  ['invalid stake', { stake: 0 }], ['invalid line', { line: 'NaN' }],
  ['draw on a full-game market', { selection: 'draw' }],
  ['future placement', { placed_at: '2100-01-01T00:00:00Z' }], ['missing reason', { reason: '' }],
]) await test(`correction rejects ${label} before changing the original`, async f => {
  const id = f.bet()
  assert.equal((await f.request(`/bets/${id}/correction`, { ...correction, confirm_override: true, ...body })).status, 400)
  assert.equal(f.one('SELECT record_status FROM bets WHERE id = ?', id).record_status, 'active')
})

await test('retrying a confirmed correction returns its existing replacement', async f => {
  const id = f.bet()
  const body = { ...correction, confirm_override: true, client_request_id: 'correction-retry-001' }
  const first = await f.request(`/bets/${id}/correction`, body)
  const count = f.one('SELECT COUNT(*) AS n FROM bets').n
  const retry = await f.request(`/bets/${id}/correction`, body)
  assert.equal(first.status, 201)
  assert.equal(retry.status, 200)
  assert.equal(retry.body.replay, true)
  assert.equal(retry.body.id, first.body.id)
  assert.equal(f.one('SELECT COUNT(*) AS n FROM bets').n, count)
  const other = f.bet()
  assert.equal((await f.request(`/bets/${other}/correction`, body)).status, 409)
})

await test('simultaneous corrections create only one active replacement', async f => {
  const id = f.bet()
  const responses = await Promise.all(['correction-tab-a', 'correction-tab-b'].map(client_request_id =>
    f.request(`/bets/${id}/correction`, { ...correction, confirm_override: true, client_request_id })))
  assert.deepEqual(responses.map(r => r.status).sort(), [201, 409])
  assert.equal(f.one('SELECT COUNT(*) AS n FROM bets WHERE supersedes_bet_id = ?', id).n, 1)
})

await test('stale balance with matching versions rejects withdrawal without a ledger write', async f => {
  const acc = f.bankroll({ age: 20 * 60 * 1000 })
  const state = await f.request('/bankroll')
  assert.equal(state.body.summary_state.status, 'stale')
  const before = f.one('SELECT COUNT(*) AS n FROM bankroll_ledger').n
  const r = await f.request('/bankroll/entries', { entry_type: 'withdrawal', amount: 1000 })
  assert.equal(r.status, 409)
  assert.equal(r.body.error, 'recalculation_pending')
  assert.equal(f.one('SELECT COUNT(*) AS n FROM bankroll_ledger').n, before)
  assert.equal(f.one('SELECT risk_state_version FROM bankroll_accounts WHERE id = ?', acc.id).risk_state_version, acc.risk_state_version)
})

for (const mismatch of ['risk_state_version', 'ledger_watermark', 'bet_event_watermark'])
  await test(`withdrawal rejects ${mismatch} mismatch`, async f => {
    f.bankroll({ mismatch })
    assert.equal((await f.request('/bankroll/entries', { entry_type: 'withdrawal', amount: 1000 })).status, 409)
  })

await test('withdrawal without any snapshot remains blocked', async f => {
  f.bankroll({ materialized: false })
  assert.equal((await f.request('/bankroll/entries', { entry_type: 'withdrawal', amount: 1000 })).status, 409)
})

await test('current balance permits withdrawal and idempotent replay', async f => {
  const acc = f.bankroll()
  const body = { entry_type: 'withdrawal', amount: 1000, client_request_id: 'withdraw-retry-001' }
  assert.equal((await f.request('/bankroll')).body.summary_state.status, 'current')
  const first = await f.request('/bankroll/entries', body)
  const retry = await f.request('/bankroll/entries', body)
  assert.equal(first.status, 201)
  assert.equal(retry.status, 200)
  assert.equal(retry.body.replay, true)
  assert.equal(f.one("SELECT COUNT(*) AS n FROM bankroll_ledger WHERE account_id = ? AND entry_type = 'withdrawal'", acc.id).n, 1)
})

await test('current balance still rejects an excessive withdrawal', async f => {
  f.bankroll()
  const r = await f.request('/bankroll/entries', { entry_type: 'withdrawal', amount: 10001 })
  assert.equal(r.status, 409)
  assert.equal(r.body.error, 'exceeds_available_bankroll')
})

await test('simultaneous withdrawals cannot spend the same materialized balance twice', async f => {
  const acc = f.bankroll()
  const results = await Promise.all(['withdraw-tab-a', 'withdraw-tab-b'].map(client_request_id =>
    f.request('/bankroll/entries', { entry_type: 'withdrawal', amount: 6000, client_request_id })))
  assert.deepEqual(results.map(r => r.status).sort(), [201, 409])
  assert.equal(f.one("SELECT COUNT(*) AS n FROM bankroll_ledger WHERE account_id = ? AND entry_type = 'withdrawal'", acc.id).n, 1)
})

console.log(`\n${passed} passed`)
