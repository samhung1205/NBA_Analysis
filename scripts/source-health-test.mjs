/** 來源狀態回歸：固定時鐘與公開 fixture，完全不連網或正式 DB。 */
import assert from 'node:assert/strict'
import { classifySource, refereeCadence } from '../src/lib/source-health.ts'
import api from '../src/routes/api.ts'

const NOW = Date.parse('2026-10-28T18:40:00+08:00')
const ago = minutes => new Date(NOW - minutes * 60000).toISOString()
const row = (source_key, options = {}) => ({ source_key, display_name: source_key, last_status: 'ok',
  last_success_at: ago(5), last_attempt_at: ago(5), expected_interval_min: 30, ...options })
const classify = (r, rows = [r], now = NOW) => classifySource(r, rows, now)
let passed = 0
const test = async (name, fn) => { await fn(); passed++; console.log(`  ✓ ${name}`) }

await test('recent critical heartbeat is healthy', () => {
  assert.equal(classify(row('decision_board')).state, 'ok')
})
await test('stale critical success remains stale even with a recent attempt', () => {
  const s = classify(row('decision_board', { last_success_at: ago(61), last_attempt_at: ago(1) }))
  assert.equal(s.state, 'stale'); assert.equal(s.operational, true)
})
await test('a warning is not labelled stale; an error is never labelled healthy', () => {
  assert.equal(classify(row('nbainjuries', { last_status: 'warn' })).state, 'warning')
  assert.equal(classify(row('nbainjuries', { last_status: 'error' })).state, 'error')
})
await test('two-interval boundary uses exact timestamps rather than rounded minutes', () => {
  assert.equal(classify(row('decision_board', { last_success_at: ago(60) })).state, 'ok')
  assert.equal(classify(row('decision_board', { last_success_at: ago(60) }), undefined, NOW + 1).state, 'stale')
})
await test('legacy sources are inactive while retaining their original stale flag', () => {
  for (const key of ['bbref', 'balldontlie']) {
    const s = classify(row(key, { last_success_at: ago(200000) }))
    assert.equal(s.state, 'inactive'); assert.equal(s.operational, false); assert.equal(s.stale, true)
  }
})
await test('historical backfill warnings and errors remain visible separately', () => {
  for (const last_status of ['warn', 'error']) {
    const s = classify(row('nbainjuries_backfill', { last_status, expected_interval_min: null }))
    assert.equal(s.state, 'historical'); assert.equal(s.health, last_status); assert.equal(s.operational, false)
  }
})
await test('old fallback warning becomes standby only with fresher healthy alternative', () => {
  const backup = row('espn', { last_status: 'warn', last_success_at: ago(40000), last_attempt_at: ago(40000) })
  const primary = row('nba_cdn')
  assert.equal(classify(backup, [backup, primary]).state, 'standby')
  assert.equal(classify(backup, [backup]).state, 'warning')
  assert.equal(classify(backup, [backup, { ...primary, last_status: 'error' }]).state, 'warning')
  assert.equal(classify(backup, [backup, { ...primary, last_success_at: ago(50000) }]).state, 'warning')
})
await test('recent backup failure is not hidden by an older primary success', () => {
  const backup = row('espn', { last_status: 'error', last_attempt_at: ago(1) })
  assert.equal(classify(backup, [backup, row('nba_cdn')]).state, 'error')
})
await test('Taiwan odds disabled or blocked requires action rather than stale label', () => {
  for (const last_outcome of ['blocked', 'not_configured']) {
    const s = classify(row('twsport', { last_status: 'warn', last_outcome, last_success_at: ago(200000) }))
    assert.equal(s.state, 'blocked'); assert.equal(s.health, 'warn'); assert.equal(s.operational, true)
  }
})
await test('referees pause at midday and resume at Taipei 20:07', () => {
  const r = row('nba_referees', { last_success_at: '2026-10-28T10:37:02+08:00' })
  const s = classify(r)
  assert.equal(s.state, 'waiting'); assert.equal(s.stale, true)
  assert.equal(s.next_poll_at, '2026-10-28T12:07:00.000Z')
})
await test('referees missing the last active morning session still warn during pause', () => {
  const r = row('nba_referees', { last_success_at: '2026-10-28T09:00:00+08:00' })
  assert.equal(classify(r).state, 'stale')
})
await test('referees become stale if evening polling does not resume', () => {
  const r = row('nba_referees', { last_success_at: '2026-10-28T10:37:02+08:00' })
  assert.equal(classify(r, [r], Date.parse('2026-10-28T21:08:00+08:00')).state, 'stale')
})
await test('referee error remains an error during pause; previous-day heartbeat cannot pass', () => {
  assert.equal(classify(row('nba_referees', { last_status: 'error' })).state, 'error')
  assert.equal(classify(row('nba_referees', { last_success_at: '2026-10-27T10:37:02+08:00' })).state, 'stale')
})
await test('referee cadence spans midnight and Taipei schedule is independent of ET DST', () => {
  for (const date of ['2026-10-28', '2026-11-01', '2026-03-08']) {
    const c = refereeCadence(Date.parse(`${date}T23:50:00+08:00`))
    assert.equal(new Date(c.next + 8 * 3600000).toISOString().slice(11, 16), '00:07')
    assert.equal(c.paused, false)
  }
})
await test('unknown or malformed success time is not invented', () => {
  const r = row('new_source', { last_status: 'unknown', last_success_at: 'invalid' })
  assert.equal(classify(r).state, 'unknown'); assert.equal(classify(r).stale, null)
  assert.equal(classify(row('new_source', { last_success_at: null })).state, 'unknown')
})
await test('status API preserves raw warning, shows local data note and ignores historical issues for operational overall', async () => {
  const sources = [row('nbainjuries_backfill', { last_status: 'warn', last_error: '2 份失敗' }),
    row('bbref', { last_success_at: ago(200000) }), row('decision_board')]
  sources[2].last_success_at = new Date().toISOString()
  const DB = { prepare: () => ({ bind() { return this }, async all() { return { results: sources } } }) }
  const res = await api.request('/system/status', {}, { DB })
  const body = await res.json()
  assert.equal(res.status, 200); assert.equal(body.overall, 'ok')
  assert.match(body.note, /本機 D1/); assert.doesNotMatch(body.note, /目前為 seed/)
  assert.equal(body.sources[0].last_error, '2 份失敗')
  assert.equal(body.sources[0].health, 'warn'); assert.equal(body.sources[0].state, 'historical')
})
await test('status API still reports an operational error overall', async () => {
  const DB = { prepare: () => ({ bind() { return this }, async all() { return { results: [row('decision_board', { last_status: 'error' })] } } }) }
  const body = await (await api.request('/system/status', {}, { DB })).json()
  assert.equal(body.overall, 'error')
})
await test('status API without sources does not claim healthy operation', async () => {
  const DB = { prepare: () => ({ bind() { return this }, async all() { return { results: [] } } }) }
  const body = await (await api.request('/system/status', {}, { DB })).json()
  assert.equal(body.overall, 'unknown')
})

console.log(`\n${passed} passed`)
