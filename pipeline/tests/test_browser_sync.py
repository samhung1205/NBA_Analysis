"""Browser import contracts, privacy, time ordering, receipts and isolated Postgres worker flow."""
import copy
import json
import uuid
import base64
import gzip
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.odds import browser_sync as bs
from core.odds import ingest

NOW = datetime.now(timezone.utc)
TIP = NOW + timedelta(hours=12)
FX = Path(__file__).parent / 'fixtures/odds/twsport_event_detail_h1_20261004.json'


def scrub(value, kind):
    out = {}
    for key, v in value.items():
        if key not in bs.SCHEMA[kind]:
            continue
        if key == 'markets':
            v = [scrub(m, 'market') for m in v if m['externalreference'].split('/')[0] in ('219', '223', '225', '60', '66', '68')]
        elif key == 'selections':
            v = [scrub(s, 'selection') for s in v]
        out[key] = v
    return out


def fixture():
    event = scrub(json.loads(FX.read_text())['data'], 'event')
    event.update(idfoevent='browser-fixture', participantname_home='波士頓塞爾提克', participantname_away='紐約尼克', tsstart=TIP.isoformat())
    payload = {'version': bs.VERSION, 'events': [{'captured_at': (NOW - timedelta(seconds=5)).isoformat(),
                                                'tournament': '美國職籃', 'event': event}]}
    job = dict(id='browser-fixture-job', user_id=1, created_at=NOW-timedelta(seconds=20), received_at=NOW,
               expires_at=NOW+timedelta(minutes=5), betting_day=bs.tpe_date(TIP), payload=payload)
    return payload, job


def test_valid_capture_preserves_individual_times_and_all_six_markets():
    p, j = fixture()
    p['events'].append(copy.deepcopy(p['events'][0]))
    p['events'][1]['captured_at'] = (NOW-timedelta(seconds=2)).isoformat()
    p['events'][1]['event']['idfoevent'] = 'second'
    events = bs.validate_capture(p, j, NOW)
    assert events[0][0] != events[1][0]
    assert len(bs.twsport.parse_event(events[0][1], 'regular', '美國職籃').snapshots) == 6


def compressed_inbox(payload):
    return json.dumps({'version':bs.INBOX_VERSION,'data':base64.b64encode(gzip.compress(json.dumps(payload,ensure_ascii=False).encode())).decode()})


def test_compressed_inbox_preserves_unicode_fields_and_each_capture_time():
    p,j=fixture()
    assert bs.decode_inbox(compressed_inbox(p))==p
    assert bs.decode_inbox(json.dumps(p))==p
    assert bs.decode_inbox(p)==p
    assert bs.validate_capture(bs.decode_inbox(compressed_inbox(p)),j,NOW)[0][0]==NOW-timedelta(seconds=5)


@pytest.mark.parametrize('data', ['not-valid-base64!', base64.b64encode(b'not-gzip').decode(), base64.b64encode(gzip.compress(b'{}')[:-2]).decode(),
                                  base64.b64encode(gzip.compress(b'{}')+b'garbage').decode()])
def test_corrupt_compressed_inbox_is_rejected(data):
    with pytest.raises(bs.CaptureError,match='invalid_inbox_payload'):
        bs.decode_inbox(json.dumps({'version':bs.INBOX_VERSION,'data':data}))


def test_compressed_inbox_expansion_is_bounded():
    envelope={'version':bs.INBOX_VERSION,'data':base64.b64encode(gzip.compress(b'x'*(bs.MAX_CAPTURE_BYTES+1))).decode()}
    with pytest.raises(bs.CaptureError,match='capture_too_large'):
        bs.decode_inbox(json.dumps(envelope))


@pytest.mark.parametrize('where', ['envelope', 'event', 'market', 'selection'])
def test_unknown_fields_are_rejected_without_exposing_credentials(where):
    p, j = fixture()
    target = {'envelope': p, 'event': p['events'][0]['event'], 'market': p['events'][0]['event']['markets'][0],
              'selection': p['events'][0]['event']['markets'][0]['selections'][0]}[where]
    target['Authorization'] = 'SECRET_CREDENTIAL'
    with pytest.raises(bs.CaptureError) as err:
        bs.validate_capture(p, j, NOW)
    assert 'SECRET_CREDENTIAL' not in str(err.value)


@pytest.mark.parametrize('mutate', [
    lambda p: p['events'][0].update(captured_at='2000-01-01T00:00:00Z'),
    lambda p: p['events'][0].update(captured_at=(NOW+timedelta(minutes=1)).isoformat()),
    lambda p: p['events'][0].update(captured_at='2026-10-28T01:00:00'),
    lambda p: p['events'][0].update(tournament='美國職籃熱身賽'),
    lambda p: p['events'][0]['event'].update(tsstart=(NOW-timedelta(minutes=1)).isoformat()),
    lambda p: p['events'][0]['event'].update(tsstart='2099-01-01T01:00:00Z'),
    lambda p: p['events'][0]['event'].update(markets=[]),
    lambda p: p['events'].append(copy.deepcopy(p['events'][0])),
])
def test_invalid_capture_fails_before_any_import(mutate):
    p, j = fixture()
    mutate(p)
    with pytest.raises((bs.CaptureError, ValueError)):
        bs.validate_capture(p, j, NOW)


def test_import_uses_each_capture_time_and_safe_provenance_and_does_not_fetch():
    p, j = fixture()
    p['events'].append(copy.deepcopy(p['events'][0]))
    p['events'][1]['captured_at'] = (NOW-timedelta(seconds=2)).isoformat()
    p['events'][1]['event']['idfoevent'] = 'second'
    calls = []
    def run(source, *, fetch, parser, fetched_at, db):
        raw = fetch(None)
        parsed = parser(raw)
        assert parsed.events[0].snapshots[0].raw['browser_sync_job_id'] == j['id']
        assert parsed.events[0].snapshots[0].raw['browser_captured_at'] == fetched_at.isoformat()
        calls.append(fetched_at)
        return SimpleNamespace(n_matched=1, diagnostics={'would_write': 6}, n_snapshots_new=6, n_snapshots_unchanged=0,
                               n_snapshots_stale=0, n_unmatched=0, n_ambiguous=0, n_rejected=0, outcome='success', run_id=len(calls))
    state, summary, err = bs.import_capture(j, now=NOW, clock=lambda:NOW, run=run, refresh=lambda:True)
    assert state == 'succeeded' and summary['new'] == 12 and summary['decision_refreshed'] and err is None
    assert calls == [NOW-timedelta(seconds=5), NOW-timedelta(seconds=2)]


def test_refresh_failure_is_partial_and_no_matches_is_not_success():
    _, j = fixture()
    def run(*a, **kw):
        return SimpleNamespace(n_matched=1, diagnostics={'would_write': 6}, n_snapshots_new=6, n_snapshots_unchanged=0,
                               n_snapshots_stale=0, n_unmatched=0, n_ambiguous=0, n_rejected=0, outcome='success', run_id=1)
    assert bs.import_capture(j, now=NOW, clock=lambda:NOW, run=run, refresh=lambda:False)[0] == 'partial'
    def unmatched(*a, **kw):
        r=run();r.n_matched=0;r.n_snapshots_new=0;r.n_unmatched=1;r.diagnostics['would_write']=0
        return r
    assert bs.import_capture(j, now=NOW, clock=lambda:NOW, run=unmatched, refresh=lambda:True)[0] == 'failed'


def test_disabled_consumer_never_opens_database(monkeypatch):
    monkeypatch.delenv('TWSPORT_SYNC_ENABLED', raising=False)
    class NoDB:
        def cursor(self):
            raise AssertionError('must not access DB')
    bs.browser_sync_job(db=NoDB())


def test_browser_mode_does_not_launch_automation_or_overwrite_quote_heartbeat(monkeypatch):
    monkeypatch.setenv('TWSPORT_SYNC_ENABLED', 'true')
    monkeypatch.setattr(ingest, 'run_odds_job', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no automatic fetch')))
    assert ingest.twsport_odds_job().skipped == 'browser_capture_only'


def setup_db_job(db, seeded):
    p, j = fixture()
    with db.cursor() as cur:
        db.upsert_game(cur, nba_game_id='0022600999', season='2026-27', season_stage='regular', date_utc=TIP,
                       home_team_id=seeded['BOS'], away_team_id=seeded['NYK'], status='scheduled')
        cur.execute("INSERT INTO users(email,password_hash) VALUES(%s,'unused') RETURNING id", (f'sync-{uuid.uuid4().hex}@example.test',))
        j['user_id'] = cur.fetchone()['id']
        cur.execute("INSERT INTO browser_sync_jobs(id,user_id,client_request_id,betting_day,state,created_at,expires_at,received_at,payload) "
                    "VALUES(%s,%s,'fixture-request',%s,'queued',%s,%s,%s,%s)",
                    (j['id'],j['user_id'],j['betting_day'],j['created_at'],j['expires_at'],j['received_at'],json.dumps(p)))
    return j


@pytest.mark.parametrize('compressed',[False,True])
def test_real_worker_imports_six_markets_refreshes_and_clears_inbox(db, seeded, monkeypatch, compressed):
    j=setup_db_job(db, seeded)
    if compressed:
        with db.cursor() as cur:
            cur.execute('UPDATE browser_sync_jobs SET payload=%s WHERE id=%s',(compressed_inbox(j['payload']),j['id']))
    monkeypatch.setenv('TWSPORT_SYNC_ENABLED','true')
    bs.browser_sync_job(db=db)
    with db.cursor() as cur:
        cur.execute('SELECT * FROM browser_sync_jobs WHERE id=%s',(j['id'],));r=cur.fetchone()
        assert r['state'] == 'succeeded', r
        assert r['payload'] is None and json.loads(r['summary'])['decision_refreshed']
        cur.execute("SELECT * FROM odds_snapshots WHERE source='twsport'");rows=cur.fetchall()
        assert len(rows)==6 and all(r['fetched_at']==NOW-timedelta(seconds=5) for r in rows)
        assert all(r['raw_json']['ingestion_method']==bs.VERSION for r in rows)
        cur.execute('SELECT COUNT(*) n FROM decision_snapshots WHERE user_id=%s',(j['user_id'],));assert cur.fetchone()['n']>0
    bs.browser_sync_job(db=db)
    with db.cursor() as cur:
        cur.execute('SELECT COUNT(*) n FROM odds_snapshots');assert cur.fetchone()['n']==6


def test_invalid_jobs_clear_payload_without_odds_writes(db, seeded, monkeypatch):
    j=setup_db_job(db, seeded)
    monkeypatch.setenv('TWSPORT_SYNC_ENABLED','true')
    with db.cursor() as cur:
        cur.execute("UPDATE browser_sync_jobs SET payload=%s WHERE id=%s",(json.dumps({'password':'SECRET'}),j['id']))
    bs.browser_sync_job(db=db)
    with db.cursor() as cur:
        cur.execute('SELECT * FROM browser_sync_jobs WHERE id=%s',(j['id'],));r=cur.fetchone()
        assert r['state']=='failed' and r['payload'] is None
        assert r['error_code']=='unexpected_capture_field'
        cur.execute('SELECT COUNT(*) n FROM odds_snapshots');assert cur.fetchone()['n']==0


def test_expired_jobs_are_not_imported(db, seeded, monkeypatch):
    j=setup_db_job(db, seeded)
    monkeypatch.setenv('TWSPORT_SYNC_ENABLED','true')
    with db.cursor() as cur:
        cur.execute('UPDATE browser_sync_jobs SET expires_at=%s WHERE id=%s',(NOW-timedelta(minutes=1),j['id']))
    bs.browser_sync_job(db=db)
    with db.cursor() as cur:
        cur.execute('SELECT * FROM browser_sync_jobs WHERE id=%s',(j['id'],));r=cur.fetchone()
        assert r['state']=='failed' and r['error_code']=='expired' and r['payload'] is None
        cur.execute('SELECT COUNT(*) n FROM odds_snapshots');assert cur.fetchone()['n']==0


def test_concurrent_workers_claim_a_job_once(db, seeded, monkeypatch):
    j=setup_db_job(db, seeded)
    monkeypatch.setenv('TWSPORT_SYNC_ENABLED','true')
    calls=[]
    gate, mutex=Barrier(2), Lock()
    def import_once(job, **kw):
        with mutex:
            calls.append(job['id'])
        return 'succeeded', {'decision_refreshed': True}, None
    monkeypatch.setattr(bs,'import_capture',import_once)
    def worker():
        gate.wait(timeout=10)
        bs.browser_sync_job(db=db)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _:worker(), range(2)))
    assert calls==[j['id']]
    with db.cursor() as cur:
        cur.execute('SELECT state,payload FROM browser_sync_jobs WHERE id=%s',(j['id'],));r=cur.fetchone()
        assert r['state']=='succeeded' and r['payload'] is None


def test_claimed_job_has_processing_lease_without_relaxing_capture_freshness(db, seeded, monkeypatch):
    j=setup_db_job(db, seeded)
    original_expiry=bs.now_utc()+timedelta(seconds=30)
    with db.cursor() as cur:
        cur.execute('UPDATE browser_sync_jobs SET expires_at=%s WHERE id=%s',(original_expiry,j['id']))
    monkeypatch.setenv('TWSPORT_SYNC_ENABLED','true')
    def process(job, *, db, progress):
        assert job['expires_at']>original_expiry+timedelta(minutes=9)
        progress({'stage':'pricing','games':12,'markets':72,'new':72})
        with db.cursor() as cur:
            # Equivalent to a website poll just after the original upload deadline.
            cur.execute("UPDATE browser_sync_jobs SET state='failed',error_code='expired' WHERE expires_at<%s",(original_expiry+timedelta(seconds=1),))
            cur.execute('SELECT state,summary FROM browser_sync_jobs WHERE id=%s',(job['id'],));r=cur.fetchone()
            assert r['state']=='running' and json.loads(r['summary'])['stage']=='pricing'
        p=bs.decode_inbox(job['payload']);p['events'][0]['captured_at']=(bs.now_utc()-timedelta(minutes=6)).isoformat()
        with pytest.raises(bs.CaptureError,match='capture_time_invalid'):
            bs.validate_capture(p,job,bs.now_utc())
        return 'succeeded',{'decision_refreshed':True},None
    monkeypatch.setattr(bs,'import_capture',process)
    bs.browser_sync_job(db=db)
