import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync, readdirSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { gunzipSync } from 'node:zlib';
import { randomBytes } from 'node:crypto';
import { Hono } from 'hono';
import { createSessionToken } from '../src/lib/auth.ts';
import routes from '../src/routes/twsport-sync.ts';
import api from '../src/routes/api.ts';
import { boundedJson, validateCapture, encodeInboxCapture, INBOX_VERSION, MAX_INBOX_BYTES } from '../src/lib/twsport-sync.ts';
import { collectTwsport } from '../extensions/twsport-sync/collector.js';
const schema = JSON.parse(readFileSync('pipeline/core/odds/browser_schema.json'));
const original = JSON.parse(readFileSync('pipeline/tests/fixtures/odds/twsport_event_detail_h1_20261004.json')).data;
function scrub(value, kind) {
  return Object.fromEntries(Object.entries(value).filter(([k]) => schema[kind].includes(k)).map(([k, v]) =>
    [k, k === 'markets' ? v.filter(m => ['219','223','225','60','66','68'].includes(m.externalreference.split('/')[0])).map(x => scrub(x,'market')) : k === 'selections' ? v.map(x => scrub(x,'selection')) : v]));
}
const day = new Date(Date.now() + 8 * 3600000 + 86400000).toISOString().slice(0,10);
const event = () => ({ ...scrub(original, 'event'), idfoevent: 'sync-fixture', tsstart: `${day}T08:00:00+08:00` });
const payload = () => ({version:'twsport-browser-v1', events:[{captured_at:new Date().toISOString(), tournament:'美國職籃', event:event()}]});
const decodeStored = text => { const envelope=JSON.parse(text); return JSON.parse(gunzipSync(Buffer.from(envelope.data,'base64')).toString('utf8')); };
function fixture() {
  const sql = new DatabaseSync(':memory:');
  sql.exec('PRAGMA foreign_keys=ON');
  for (const name of readdirSync('migrations/d1').filter(n=>n.endsWith('.sql')).sort()) sql.exec(readFileSync(`migrations/d1/${name}`,'utf8'));
  sql.exec("INSERT INTO users(id,email,password_hash) VALUES(1,'sync@example.test','unused'),(2,'other@example.test','unused')");
  const hb = new Date().toISOString();
  sql.prepare("INSERT INTO data_sources(source_key,display_name,last_status,last_attempt_at) VALUES('browser_sync','fixture','ok',?)").run(hb);
  function stmt(query, params=[]) {return {bind(...p){return stmt(query,p)},async all(){return {results:sql.prepare(query).all(...params)}},async run(){const r=sql.prepare(query).run(...params);return {meta:{last_row_id:Number(r.lastInsertRowid),changes:Number(r.changes)}}}}}
  const DB={prepare:stmt};
  const env={DB,SESSION_SECRET:'local-sync-test-secret',TWSPORT_SYNC_ENABLED:'true',TWSPORT_EXTENSION_ID:'a'.repeat(32)};
  const mounted=new Hono().route('/api',routes).route('/api',api);
  async function request(path, body, {uid=1, origin='http://localhost', method=body===undefined?'GET':'POST', target=routes, extraEnv={}}={}) {
    const token=uid ? await createSessionToken(env.SESSION_SECRET,uid,'test@example.test') : null;
    const r=await target.request(path,{method,headers:{'Content-Type':'application/json',Origin:origin,...(token?{Cookie:`nba_session=${token}`}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})},{...env,...extraEnv});
    return {status:r.status,body:await r.json()};
  }
  return {sql,request,mounted,env,create:()=>request('/twsport-sync',{client_request_id:'sync-request-001',betting_day:day})};
}
let passed=0;
async function test(name, fn){const f=fixture();try{await fn(f);passed++;console.log(`  ✓ ${name}`)}finally{f.sql.close()}}
await test('anonymous and cross-site requests are rejected before writes',async f=>{
 assert.equal((await f.request('/twsport-sync',undefined,{uid:0})).status,401);
 assert.equal((await f.request('/twsport-sync',{},{origin:'https://evil.test'})).status,403);
 assert.equal(f.sql.prepare('SELECT COUNT(*) n FROM browser_sync_jobs').get().n,0);
});
await test('disabled feature and missing worker fail closed',async f=>{
 assert.equal((await f.request('/twsport-sync',undefined,{extraEnv:{TWSPORT_SYNC_ENABLED:'false'}})).body.enabled,false);
 f.sql.exec("UPDATE data_sources SET last_attempt_at='2000-01-01T00:00:00Z'");
 assert.equal((await f.create()).body.error,'worker_unavailable');
});
await test('prepare is idempotent; one active job across users',async f=>{
 const a=await f.create();assert.equal(a.status,201);
 assert.equal((await f.create()).body.job.id,a.body.job.id);
 const b=await f.request('/twsport-sync',{client_request_id:'other-request-001',betting_day:day},{uid:2});
 assert.ok([409,429].includes(b.status));
 assert.equal(f.sql.prepare('SELECT COUNT(*) n FROM browser_sync_jobs').get().n,1);
});
await test('valid capture queues once and private payload never appears in receipt',async f=>{
 const id=(await f.create()).body.job.id;
 const r=await f.request(`/twsport-sync/${id}/capture`,payload());
 assert.equal(r.status,202);assert.equal(r.body.job.state,'queued');assert.equal(r.body.job.payload,undefined);
 assert.equal((await f.request(`/twsport-sync/${id}/capture`,payload())).body.replay,true);
 const db=f.sql.prepare('SELECT * FROM browser_sync_jobs WHERE id=?').get(id);
 assert.equal(decodeStored(db.payload).events[0].event.idfoevent,'sync-fixture');
});
await test('twelve-game Unicode capture stores as a small lossless inbox envelope',async f=>{
 const p=payload();p.events=Array.from({length:12},(_,i)=>({...p.events[0],event:{...event(),idfoevent:'large-fixture-'+i}}));
 assert.ok(Buffer.byteLength(JSON.stringify(p))>64*1024);
 const encoded=await encodeInboxCapture(p);
 assert.equal(JSON.parse(encoded).version,INBOX_VERSION);assert.ok(encoded.length<16*1024);assert.deepEqual(decodeStored(encoded),p);
 const id=(await f.create()).body.job.id;
 assert.equal((await f.request(`/twsport-sync/${id}/capture`,p)).status,202);
 const saved=f.sql.prepare('SELECT payload FROM browser_sync_jobs').get().payload;
 assert.ok(saved.length<=MAX_INBOX_BYTES);assert.deepEqual(decodeStored(saved),p);
});
await test('incompressible oversized inbox is rejected rather than sent as a large database bind',async()=>{
 await assert.rejects(()=>encodeInboxCapture({data:randomBytes(100000).toString('base64')}),/capture_too_large/);
});
function disconnectCapture(f, mode) {
 const prepare=f.env.DB.prepare;let faults=0,writes=0;
 f.env.DB.prepare=query=>{
  const store=query.includes('SET payload = ?');
  const receipt=query.includes('FROM browser_sync_jobs WHERE id = ?')&&!query.includes('AND user_id = ?');
  const closed=()=>Object.assign(new Error('write CONNECTION_CLOSED secret-host-must-not-leak'),{code:'CONNECTION_CLOSED'});
  const wrap=stmt=>({bind(...args){return wrap(stmt.bind(...args))},async all(){
   if(mode==='receipt'&&receipt&&faults++===0)throw closed();
   return stmt.all();
  },async run(){
   if(store){writes++;
    if(mode==='always'||mode==='before'&&faults++===0)throw closed();
    const result=await stmt.run();
    if(mode==='after'&&faults++===0)throw closed();
    return result;
   }
   return stmt.run();
  }});
  return wrap(prepare(query));
 };
 return ()=>writes;
}
await test('capture reconnects after a failed write and queues only once',async f=>{
 const id=(await f.create()).body.job.id;const writes=disconnectCapture(f,'before');
 const r=await f.request(`/twsport-sync/${id}/capture`,payload());
 assert.equal(r.status,202);assert.equal(r.body.job.state,'queued');assert.equal(writes(),2);
 assert.equal(f.sql.prepare('SELECT COUNT(*) n FROM browser_sync_jobs').get().n,1);
});
for(const phase of ['after','receipt'])await test(`lost ${phase} response recovers the committed receipt without another write`,async f=>{
 const id=(await f.create()).body.job.id;const writes=disconnectCapture(f,phase);
 const r=await f.request(`/twsport-sync/${id}/capture`,payload());
 assert.equal(r.status,200);assert.equal(r.body.job.state,'queued');assert.equal(r.body.replay,true);assert.equal(writes(),1);
});
await test('persistent capture disconnect stops after one retry without leaking driver details',async f=>{
 const id=(await f.create()).body.job.id;const writes=disconnectCapture(f,'always');
 const r=await f.request(`/twsport-sync/${id}/capture`,payload());
 assert.equal(r.status,503);assert.equal(r.body.error,'capture_store_unavailable');assert.equal(writes(),2);
 assert.ok(!JSON.stringify(r).includes('secret-host'));
 assert.equal(f.sql.prepare('SELECT state,payload FROM browser_sync_jobs').get().payload,null);
});
await test('other users cannot read or upload a capture',async f=>{
 const id=(await f.create()).body.job.id;
 assert.equal((await f.request(`/twsport-sync?id=${id}`,undefined,{uid:2})).status,404);
 assert.equal((await f.request(`/twsport-sync/${id}/capture`,payload(),{uid:2})).status,404);
});
await test('credentials or arbitrary fields are rejected instead of stored',async f=>{
 const id=(await f.create()).body.job.id;
 for(const mutate of [p=>p.headers={Cookie:'SECRET_COOKIE'},p=>p.events[0].event.authorization='SECRET_KEY',p=>p.events[0].event.markets[0].selections[0].token='SECRET_TOKEN']){
  const p=payload();mutate(p);const r=await f.request(`/twsport-sync/${id}/capture`,p);assert.equal(r.status,400);
 }
 assert.equal(f.sql.prepare('SELECT payload FROM browser_sync_jobs').get().payload,null);
});
await test('invalid, duplicate, stale, future, started and wrong-day captures fail closed',async f=>{
 const id=(await f.create()).body.job.id;
 for(const mutate of [p=>p.events.push(p.events[0]),p=>p.events[0].captured_at='2000-01-01T00:00:00Z',p=>p.events[0].captured_at=new Date(Date.now()+60000).toISOString(),p=>p.events[0].event.tsstart='2000-01-01T00:00:00Z',p=>p.events[0].event.tsstart='2099-01-01T00:00:00Z']){
  const p=payload();mutate(p);assert.equal((await f.request(`/twsport-sync/${id}/capture`,p)).status,400);
 }
});
await test('chunked oversized body is bounded without trusting Content-Length',async()=>{
 const request=new Request('http://localhost',{method:'POST',body:new ReadableStream({start(c){c.enqueue(new Uint8Array(2*1024*1024+1));c.close()}}),duplex:'half'});
 await assert.rejects(()=>boundedJson(request),/capture_too_large/);
});
await test('expiry clears payload and cancellation cannot stop a queued job',async f=>{
 const id=(await f.create()).body.job.id;
 await f.request(`/twsport-sync/${id}/capture`,payload());
 await f.request(`/twsport-sync/${id}/cancel`,{});
 assert.equal(f.sql.prepare('SELECT state FROM browser_sync_jobs').get().state,'queued');
 f.sql.exec("UPDATE browser_sync_jobs SET expires_at='2000-01-01T00:00:00Z'");
 const r=await f.request(`/twsport-sync?id=${id}`);
 assert.equal(r.body.job.state,'failed');assert.equal(f.sql.prepare('SELECT payload FROM browser_sync_jobs').get().payload,null);
});
await test('enabling browser sync protects public odds-bearing routes',async f=>{
 for(const path of ['/games','/games/today','/games/1','/odds/1','/sizing'])
  assert.equal((await f.request(path,undefined,{uid:0,target:api})).status,401,path);
});
await test('mounted routes protect sync and odds while preserving other public APIs',async f=>{
 assert.equal((await f.request('/api/twsport-sync',undefined,{uid:0,target:f.mounted})).status,401);
 assert.equal((await f.request('/api/games',undefined,{uid:0,target:f.mounted})).status,401);
 assert.equal((await f.request('/api/teams',undefined,{uid:0,target:f.mounted})).status,200);
 assert.equal((await f.request('/api/games',undefined,{uid:0,target:f.mounted,extraEnv:{TWSPORT_SYNC_ENABLED:'false'}})).status,401);
 f.sql.exec("DELETE FROM data_sources WHERE source_key='browser_sync'");
 assert.equal((await f.request('/api/games',undefined,{uid:0,target:f.mounted,extraEnv:{TWSPORT_SYNC_ENABLED:'false'}})).status,200);
});
await test('concurrent prepares and uploads leave one immutable queued capture',async f=>{
 const prepared=await Promise.all([f.create(),f.create()]);
 assert.equal(f.sql.prepare('SELECT COUNT(*) n FROM browser_sync_jobs').get().n,1);
 const id=prepared.find(r=>r.body.job)?.body.job.id;
 const a=payload(),b=payload();b.events[0].event.idfoevent='second-upload';
 const result=await Promise.all([f.request(`/twsport-sync/${id}/capture`,a),f.request(`/twsport-sync/${id}/capture`,b)]);
 assert.ok(result.every(r=>[200,202].includes(r.status)));
 assert.equal(f.sql.prepare('SELECT state FROM browser_sync_jobs').get().state,'queued');
 const saved=f.sql.prepare('SELECT payload FROM browser_sync_jobs').get().payload;
 await f.request(`/twsport-sync/${id}/capture`,b);
 assert.equal(f.sql.prepare('SELECT payload FROM browser_sync_jobs').get().payload,saved);
});
await test('missing migration and null request fail without an unhandled error',async f=>{
 assert.equal((await f.request('/twsport-sync',null)).status,400);
 f.sql.exec('DROP TABLE browser_sync_jobs');
 assert.equal((await f.request('/twsport-sync')).body.reason,'migration_required');
 assert.equal((await f.create()).status,503);
});
await test('per-game capture times remain separate and scalar types are bounded',async()=>{
 const p=payload();p.events.push({...p.events[0],captured_at:new Date(Date.now()-1000).toISOString(),event:{...event(),idfoevent:'second'}});
 const r=validateCapture(p,{created_at:new Date().toISOString(),betting_day:day},Date.now());
 assert.notEqual(r.events[0].captured_at,r.events[1].captured_at);
 p.events[0].event.markets[0].name={cookie:'no'};
 assert.throws(()=>validateCapture(p,{created_at:new Date().toISOString(),betting_day:day},Date.now()));
});
async function collector(get){let calls=0;const ctx={location:{origin:'https://www-talo-ssb-pr.sportslottery.com.tw'},document:{title:'台彩'},Date,Set,Map,TextEncoder,TextDecoder,AbortSignal,setTimeout:fn=>{fn();return 1},fetch:async(url,init)=>{calls++;assert.equal(url,'/services/content/get');return get(JSON.parse(init.body).contentId,calls)},input:{date:day,schema}};const result=await runInNewContext(`(${collectTwsport.toString()})(input)`,ctx);return {result:JSON.parse(JSON.stringify(result)),calls}}
const json=x=>new Response(JSON.stringify(x),{headers:{'Content-Type':'application/json'}});
await test('real collector traverses navigation, returns allowlisted data and strips unrelated secrets',async()=>{
 const detail={...event(),account:'SECRET_ACCOUNT',markets:event().markets.map(m=>({...m,cookie:'SECRET_COOKIE'}))};
 const {result,calls}=await collector(({type,id})=>{
  if(id==='1355/34765.1')return json({data:{bonavigationnodes:[{name:'美國',idfwbonavigation:'country'}]}});
  if(id==='1355/country')return json({data:{bonavigationnodes:[{name:'美國職籃',idfwbonavigation:'nba',numevents:1}]}});
  if(id==='1355/nba')return json({data:{marketgroups:[{idfwmarketgrouptype:'MATCHESCOUPON',idfwmarketgroup:'group'}]}});
  if(type==='eventGroup')return json({data:{events:[event()]}});
  return json({data:detail});
 });
 assert.equal(result.ok,true);assert.equal(calls,5);assert.equal(result.capture.events.length,1);
 assert.ok(!JSON.stringify(result).includes('SECRET_'));assert.equal(result.capture.events[0].event.markets.length,event().markets.length);
});
await test('collector stops on blocked/rate-limited/changed responses without retry or leakage',async()=>{
 for(const [response,error] of [[new Response('SECRET',{status:403}),'blocked'],[new Response('SECRET',{status:429}),'rate_limited'],[new Response('SECRET',{status:200}),'parser_changed']]){
  const r=await collector(()=>response);assert.equal(r.result.error,error);assert.equal(r.calls,1);assert.ok(!JSON.stringify(r).includes('SECRET'));
 }
});
function background({frames=[],execution={ok:true,capture:{}},hold=false}={}) {
 let listener,execute,opened=0,queries=0,release;
 const pending=hold?new Promise(r=>{release=r}):Promise.resolve();
 const chrome={runtime:{onMessageExternal:{addListener:fn=>{listener=fn}},getPlatformInfo:async()=>({})},
  tabs:{query:async()=>{queries++;await pending;return [{id:5}]},create:async()=>{opened++}},
  webNavigation:{getAllFrames:async()=>frames},scripting:{executeScript:async input=>{execute=input;return [{result:execution}]}}};
 const source=readFileSync('extensions/twsport-sync/background.js','utf8').replace(/^import .*;\n/gm,'');
 runInNewContext(source,{chrome,URL,setInterval:()=>1,clearInterval:()=>{},SITE_ORIGIN:'https://nba.example',SCHEMA:schema,collectTwsport});
 const sender={url:'https://nba.example/decision',frameId:0};
 const send=(data,from=sender)=>new Promise(resolve=>{if(listener(data,from,resolve)===false)resolve(undefined)});
 return {send,release,get execute(){return execute},get opened(){return opened},get queries(){return queries}};
}
await test('extension rejects foreign origins, paths and child frames before reading tabs',async()=>{
 const b=background();
 for(const sender of [{url:'https://evil.test/decision',frameId:0},{url:'https://nba.example/elsewhere',frameId:0},{url:'https://nba.example/decision',frameId:1}])
  assert.equal(await b.send({type:'NBA_TWSPORT_COLLECT',date:day},sender),undefined);
 assert.equal(b.queries,0);
 assert.equal((await b.send({type:'NBA_TWSPORT_PING'})).ok,true);
});
await test('extension uses only the exact sportsbook frame and does not double start',async()=>{
 const b=background({frames:[{frameId:9,url:'https://www-talo-ssb-pr.sportslottery.com.tw/x'}],hold:true});
 const first=b.send({type:'NBA_TWSPORT_COLLECT',date:day});
 assert.equal((await b.send({type:'NBA_TWSPORT_COLLECT',date:day})).error,'sync_busy');
 b.release();assert.equal((await first).ok,true);
 assert.equal(b.execute.world,'MAIN');assert.deepEqual(Array.from(b.execute.target.frameIds),[9]);assert.equal(b.opened,0);
 const absent=background({frames:[{frameId:0,url:'https://evil.test/'}]});
 assert.equal((await absent.send({type:'NBA_TWSPORT_COLLECT',date:day})).error,'open_sportsbook');assert.equal(absent.opened,1);
});
async function frontend({captureError=null,uploadDisconnect=false}={}) {
 let click,reads=0,events=0;
 const posts=[],button={disabled:true,textContent:'',addEventListener:(name,fn)=>{if(name==='click')click=fn}},status={textContent:''};
 const job={id:'ui-fixture',betting_day:day,state:'capturing'};
 const NBA={tpe:t=>t,get:async path=>{
  if(!path.includes('?id='))return {enabled:true,extension_id:'a'.repeat(32),job:null};
  reads++;return {job:reads===1?{...job,state:'queued'}:{...job,state:'succeeded',summary:{markets:6,games:1,capture_times:['fixture-time']}}};
 },post:async(path,body)=>{
  posts.push({path,body});
  if(path.endsWith('/capture')){if(uploadDisconnect)throw new Error('network');return {job:{...job,state:'queued'}}}
  return {job};
 }};
 const chrome={runtime:{sendMessage:(id,msg,callback)=>callback(msg.type==='NBA_TWSPORT_PING'?{ok:true}:captureError?{ok:false,error:captureError}:{ok:true,capture:payload()})}};
 const ctx={document:{getElementById:id=>({'twsport-sync-button':button,'twsport-sync-status':status,'decision-date':{value:day}}[id])},NBA,chrome,crypto,Event,
  window:{dispatchEvent:()=>{events++}},setTimeout:fn=>{fn();return 1}};
 runInNewContext(readFileSync('public/static/js/twsport-sync.js','utf8'),ctx);
 for(let n=0;n<4;n++)await new Promise(setImmediate);
 assert.equal(button.disabled,false);
 await click();
 return {posts,status,button,events};
}
await test('frontend completes prepare, one upload, receipt polling and decision refresh',async()=>{
 const r=await frontend();assert.equal(r.posts.length,2);assert.ok(r.status.textContent.includes('6 個市場'));
 assert.equal(r.events,1);assert.equal(r.button.disabled,false);
});
await test('frontend cancels blocked capture without uploading or announcing success',async()=>{
 const r=await frontend({captureError:'blocked'});assert.ok(r.posts.at(-1).path.endsWith('/cancel'));
 assert.ok(!r.posts.some(p=>p.path.endsWith('/capture')));assert.ok(r.status.textContent.includes('阻擋'));assert.equal(r.events,0);
});
await test('frontend recovers an upload disconnect through receipt without resending',async()=>{
 const r=await frontend({uploadDisconnect:true});assert.equal(r.posts.filter(p=>p.path.endsWith('/capture')).length,1);
 assert.equal(r.posts.filter(p=>p.path.endsWith('/cancel')).length,0);assert.equal(r.events,1);assert.ok(r.status.textContent.includes('決策已更新'));
});
console.log(`\n${passed} passed`);
