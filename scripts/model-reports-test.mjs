/** Real in-memory SQLite + Hono read API; no environment files, network or production data. */
import assert from 'node:assert/strict'
import { DatabaseSync } from 'node:sqlite'
import { readFileSync, readdirSync } from 'node:fs'
import api from '../src/routes/api.ts'

const sql = new DatabaseSync(':memory:')
sql.exec('PRAGMA foreign_keys=ON')
const dir = new URL('../migrations/d1/', import.meta.url)
for (const file of readdirSync(dir).filter(x => x.endsWith('.sql')).sort()) sql.exec(readFileSync(new URL(file, dir), 'utf8'))
function statement(query, args = []) {
  return { bind(...values) { return statement(query, values) },
    async all() { return { results: sql.prepare(query).all(...args) } } }
}
const env = { DB: { prepare: statement } }
let passed = 0
async function test(name, fn) { await fn(); passed++; console.log(`  ✓ ${name}`) }
const request = () => api.request('/metrics/prospective', {}, env)

await test('unmaterialized data is waiting rather than a zero-performance claim', async () => {
  const res = await request()
  assert.equal(res.status, 200)
  assert.equal(res.headers.get('Cache-Control'), 'no-store')
  assert.deepEqual(await res.json(), { status: 'waiting', reports: [] })
})
await test('Python aggregates pass through without Node recalculation', async () => {
  const report = { n_final_games: 68, evaluations: { final_t60: { n_games: 68, win_logistic: { accuracy: .602941 } } } }
  sql.prepare('INSERT INTO model_evaluation_reports VALUES (?,?,?)').run('prospective-model-v1', new Date().toISOString(), JSON.stringify(report))
  const body = await (await request()).json()
  assert.equal(body.reports[0].stale, false)
  assert.deepEqual(body.reports[0].report, report)
})
await test('old and invalid report timestamps cannot claim freshness', async () => {
  for (const ts of ['2000-01-01T00:00:00Z', 'invalid', new Date(Date.now()+86400000).toISOString()]) {
    sql.prepare('UPDATE model_evaluation_reports SET generated_at=?').run(ts)
    assert.equal((await (await request()).json()).reports[0].stale, true)
  }
})
await test('malformed materialized payload is unavailable and stale', async () => {
  sql.prepare('UPDATE model_evaluation_reports SET payload=?').run('invalid-json')
  const report = (await (await request()).json()).reports[0]
  assert.equal(report.report, null); assert.equal(report.stale, true)
})
await test('only approved aggregate report keys are public', async () => {
  sql.prepare('INSERT INTO model_evaluation_reports VALUES (?,?,?)').run('private-fixture', new Date().toISOString(), '{"private":true}')
  const body = await (await request()).json()
  assert.equal(body.reports.length, 1)
  assert.equal(JSON.stringify(body).includes('private'), false)
})
await test('D1 shadow ledger rejects mutation', () => {
  sql.prepare('INSERT INTO model_shadow_experiments VALUES (?,?,?)').run('fixture', new Date().toISOString(), '{}')
  assert.throws(() => sql.exec("UPDATE model_shadow_experiments SET spec_json='changed'"), /append-only/)
  assert.throws(() => sql.exec('DELETE FROM model_shadow_experiments'), /append-only/)
})
await test('deployment before report migration remains a waiting state', async () => {
  sql.exec('DROP TABLE model_evaluation_reports')
  assert.equal((await (await request()).json()).status, 'waiting')
})
sql.close()
console.log(`${passed} passed`)
