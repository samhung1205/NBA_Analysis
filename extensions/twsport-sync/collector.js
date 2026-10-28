/** Executes only in the normal sportsbook page, after an explicit user request. No anti-bot workaround. */
export async function collectTwsport({ date, schema }) {
  const origin = 'https://www-talo-ssb-pr.sportslottery.com.tw';
  if (location.origin !== origin) return { ok: false, error: 'wrong_frame' };
  const clean = x => String(x ?? '').replace(/[\r\n]/g, '').trim();
  if (/just a moment|attention required|請稍候|请稍候/i.test(document.title)) return { ok: false, error: 'blocked' };
  const started = Date.now();
  let requests = 0;
  const pause = () => new Promise(resolve => setTimeout(resolve, 1250));
  const safe = (obj, kind) => {
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw new Error('parser_changed');
    const out = {};
    for (const key of schema[kind]) {
      if (!(key in obj)) continue;
      if (key === 'markets') {
        if (!Array.isArray(obj[key])) throw new Error('parser_changed');
        const supported = new Set(['219', '223', '225', '60', '66', '68']);
        out[key] = obj[key].filter(m => supported.has(clean(m.externalreference).split('/')[0])).map(m => safe(m, 'market'));
        if (out[key].length > 80) throw new Error('capture_too_large');
      } else if (key === 'selections') {
        if (!Array.isArray(obj[key]) || obj[key].length > 3) throw new Error('parser_changed');
        out[key] = obj[key].map(s => safe(s, 'selection'));
      } else {
        const v = obj[key];
        if (v !== null && !['string', 'number', 'boolean'].includes(typeof v)) throw new Error('parser_changed');
        if (typeof v === 'string' && v.length > 300) throw new Error('parser_changed');
        out[key] = v;
      }
    }
    return out;
  };
  const get = async (type, id) => {
    if (++requests > 50 || Date.now() - started > 120000) throw new Error('capture_timeout');
    if (requests > 1) await pause();
    const response = await fetch('/services/content/get', {
      method: 'POST', cache: 'no-store', credentials: 'same-origin',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ contentId: { type, id: String(id) }, clientContext: { language: 'ZH', ipAddress: '0.0.0.0' } }),
      signal: AbortSignal.timeout(15000),
    });
    if (response.status === 403) throw new Error('blocked');
    if (response.status === 429) throw new Error('rate_limited');
    if (!response.ok) throw new Error('source_unavailable');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let text = '', size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 4 * 1024 * 1024) { await reader.cancel(); throw new Error('capture_too_large'); }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    const captured_at = new Date().toISOString();
    let body;
    try { body = JSON.parse(text); } catch { throw new Error('parser_changed'); }
    return { body, captured_at };
  };
  const nodes = obj => {
    const data = Array.isArray(obj?.data) ? obj.data.at(-1) : obj?.data;
    if (!Array.isArray(data?.bonavigationnodes)) throw new Error('parser_changed');
    return data.bonavigationnodes;
  };
  try {
    const sport = await get('boNavigationList', '1355/34765.1');
    const country = nodes(sport.body).find(n => clean(n.name) === '美國');
    if (!country?.idfwbonavigation) throw new Error('parser_changed');
    const countries = await get('boNavigationList', `1355/${country.idfwbonavigation}`);
    const tour = nodes(countries.body).find(n => clean(n.name) === '美國職籃');
    if (!tour?.idfwbonavigation) throw new Error('parser_changed');
    if (Number(tour.numevents) === 0) return { ok: false, error: 'no_games' };
    const nav = await get('boNavigationList', `1355/${tour.idfwbonavigation}`);
    const groups = nav.body?.data?.marketgroups?.filter(g => g.idfwmarketgrouptype === 'MATCHESCOUPON');
    if (!groups?.length) throw new Error('parser_changed');
    const listed = new Map();
    for (const group of groups) {
      const coupon = await get('eventGroup', group.idfwmarketgroup);
      if (!Array.isArray(coupon.body?.data?.events)) throw new Error('parser_changed');
      for (const event of coupon.body.data.events) {
        const tip = Date.parse(event.tsstart);
        const day = Number.isFinite(tip) ? new Date(tip + 8 * 3600000).toISOString().slice(0, 10) : '';
        if (tip > Date.now() && day === date) listed.set(clean(event.idfoevent), event);
      }
    }
    if (!listed.size) return { ok: false, error: 'no_games' };
    if (listed.size > 20) throw new Error('capture_too_large');
    const events = [];
    for (const [id] of listed) {
      const detail = await get('event', id);
      if (clean(detail.body?.data?.idfoevent) !== id) throw new Error('parser_changed');
      events.push({ tournament: '美國職籃', captured_at: detail.captured_at, event: safe(detail.body.data, 'event') });
    }
    const capture = { version: 'twsport-browser-v1', events };
    if (new TextEncoder().encode(JSON.stringify(capture)).length > 2 * 1024 * 1024) throw new Error('capture_too_large');
    return { ok: true, capture };
  } catch (error) {
    const allowed = ['blocked', 'rate_limited', 'parser_changed', 'capture_timeout', 'capture_too_large', 'source_unavailable'];
    return { ok: false, error: allowed.includes(error.message) ? error.message : 'source_unavailable' };
  }
}
