import schema from '../../pipeline/core/odds/browser_schema.json' with { type: 'json' }
import { tpeDateString } from './time'

export const SYNC_VERSION = 'twsport-browser-v1'
export const MAX_CAPTURE_BYTES = 2 * 1024 * 1024
export const INBOX_VERSION = 'twsport-inbox-gzip-v1'
export const MAX_INBOX_BYTES = 48 * 1024
export const ACTIVE_SYNC = "('capturing','queued','running')"

/** Store a compact ASCII envelope, keeping a bounded database bind and the original capture times. */
export async function encodeInboxCapture(payload: any): Promise<string> {
  const input = new TextEncoder().encode(JSON.stringify(payload))
  if (input.byteLength > MAX_CAPTURE_BYTES) throw new Error('capture_too_large')
  const compressed = new Uint8Array(await new Response(
    new Blob([input]).stream().pipeThrough(new CompressionStream('gzip'))
  ).arrayBuffer())
  let binary = ''
  for (let n = 0; n < compressed.length; n += 8192)
    binary += String.fromCharCode(...compressed.subarray(n, n + 8192))
  const stored = JSON.stringify({ version: INBOX_VERSION, data: btoa(binary) })
  if (stored.length > MAX_INBOX_BYTES) throw new Error('capture_too_large')
  return stored
}

/** Bounded even without Content-Length (chunked requests). Never log the body. */
export async function boundedJson(request: Request) {
  const reader = request.body?.getReader()
  if (!reader) throw new Error('invalid_capture')
  const chunks: Uint8Array[] = []
  let size = 0
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    size += value.byteLength
    if (size > MAX_CAPTURE_BYTES) { await reader.cancel(); throw new Error('capture_too_large') }
    chunks.push(value)
  }
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length }
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) }
  catch { throw new Error('invalid_capture') }
}

function object(value: any) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid_capture')
}
function only(value: any, fields: string[]) {
  object(value)
  if (Object.keys(value).some((k) => !fields.includes(k))) throw new Error('unexpected_capture_field')
}
function clean(value: any, kind: 'event' | 'market' | 'selection'): any {
  only(value, schema[kind])
  const out: Record<string, any> = {}
  for (const [key, v] of Object.entries(value)) {
    if (key === 'markets' || key === 'selections') {
      if (!Array.isArray(v) || v.length > (key === 'markets' ? 80 : 3)) throw new Error('invalid_capture')
      out[key] = v.map((x) => clean(x, key === 'markets' ? 'market' : 'selection'))
    } else {
      if (v !== null && !['string', 'number', 'boolean'].includes(typeof v)) throw new Error('invalid_capture')
      if (typeof v === 'string' && v.length > 300) throw new Error('invalid_capture')
      if (typeof v === 'number' && !Number.isFinite(v)) throw new Error('invalid_capture')
      out[key] = v
    }
  }
  return out
}

export function validateCapture(input: any, job: any, now: number) {
  only(input, ['version', 'events'])
  if (input.version !== SYNC_VERSION || !Array.isArray(input.events) || input.events.length < 1 || input.events.length > 20)
    throw new Error('invalid_capture')
  const seen = new Set<string>()
  const events = input.events.map((item: any) => {
    only(item, ['captured_at', 'tournament', 'event'])
    if (item.tournament !== '美國職籃' || typeof item.captured_at !== 'string'
      || !/(Z|[+-]\d{2}:\d{2})$/.test(item.captured_at)) throw new Error('invalid_capture')
    const at = Date.parse(item.captured_at)
    if (!Number.isFinite(at) || at < Date.parse(job.created_at) - 30000 || at < now - 5 * 60000 || at > now + 5000)
      throw new Error('capture_time_invalid')
    const event = clean(item.event, 'event')
    const eid = String(event.idfoevent || '')
    const tip = typeof event.tsstart === 'string' && /(Z|[+-]\d{2}:\d{2})$/.test(event.tsstart) ? Date.parse(event.tsstart) : NaN
    if (!eid || eid.length > 100 || seen.has(eid) || !Number.isFinite(tip) || tip <= now
      || tpeDateString(new Date(tip)) !== (job.betting_day instanceof Date ? job.betting_day.toISOString() : String(job.betting_day)).slice(0, 10)
      || !Array.isArray(event.markets) || !event.markets.length) throw new Error('invalid_capture')
    seen.add(eid)
    return { captured_at: new Date(at).toISOString(), tournament: item.tournament, event }
  })
  return { version: SYNC_VERSION, events }
}

export function shapeSyncJob(row: any) {
  if (!row) return null
  let summary = null
  try { summary = typeof row.summary === 'string' ? JSON.parse(row.summary) : row.summary } catch { /* receipt unavailable */ }
  return { id: row.id, state: row.state, betting_day: (row.betting_day instanceof Date ? row.betting_day.toISOString() : String(row.betting_day)).slice(0, 10),
    created_at: row.created_at, expires_at: row.expires_at, received_at: row.received_at,
    finished_at: row.finished_at, summary, error_code: row.error_code }
}
