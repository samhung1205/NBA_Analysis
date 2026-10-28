"""browser-sync-v1 inbox consumer. No external HTTP, browser automation, credentials or risk mathematics.

The browser sends allowlisted public NBA event fields. Each game's response time is preserved separately.
Queue receipts retain counts/errors; raw inbox payload is cleared on completion/expiry. Disabled by default.
"""
from __future__ import annotations

import json
import os
import math
import base64
import zlib
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

from ..timeutil import ensure_utc, now_utc, parse_utc, tpe_date
from . import twsport

VERSION = 'twsport-browser-v1'
INBOX_VERSION = 'twsport-inbox-gzip-v1'
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_INBOX_BYTES = 48 * 1024
PROCESSING_LEASE = timedelta(minutes=10)
SCHEMA = json.loads(Path(__file__).with_name('browser_schema.json').read_text())
HEARTBEAT = dict(source_key='browser_sync', display_name='Chrome 盤口匯入處理器', category='odds', expected_interval_min=1)


class CaptureError(ValueError):
    pass


class _ConnectionDb:
    """Keep this serialized consumer on its already reserved connection, including nested jobs."""
    def __init__(self, source, connection):
        self._source, self._connection = source, connection

    def __getattr__(self, name):
        return getattr(self._source, name)

    @contextmanager
    def cursor(self):
        with self._connection.cursor() as cur:
            yield cur

    @contextmanager
    def transaction(self):
        with self._connection.transaction():
            with self._connection.cursor() as cur:
                yield cur


def decode_inbox(raw):
    """Read both legacy JSON and a bounded gzip envelope; no unbounded decompression."""
    try:
        if isinstance(raw, str):
            if len(raw.encode('utf-8')) > MAX_CAPTURE_BYTES:
                raise CaptureError('capture_too_large')
            raw = json.loads(raw)
        if not isinstance(raw, dict) or raw.get('version') != INBOX_VERSION:
            return raw
        _only(raw, ['version', 'data'])
        data = raw.get('data')
        if not isinstance(data, str) or len(data) > MAX_INBOX_BYTES:
            raise CaptureError('invalid_inbox_payload')
        compressed = base64.b64decode(data, validate=True)
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(compressed, MAX_CAPTURE_BYTES + 1)
        if len(decoded) > MAX_CAPTURE_BYTES:
            raise CaptureError('capture_too_large')
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise CaptureError('invalid_inbox_payload')
        return json.loads(decoded.decode('utf-8'))
    except CaptureError:
        raise
    except (ValueError, TypeError, UnicodeError, zlib.error):
        raise CaptureError('invalid_inbox_payload') from None


def _only(value, fields):
    if not isinstance(value, dict) or any(key not in fields for key in value):
        raise CaptureError('unexpected_capture_field')


def _event(value, kind='event'):
    _only(value, SCHEMA[kind])
    for key, v in value.items():
        if key in ('markets', 'selections'):
            limit = 80 if key == 'markets' else 3
            if not isinstance(v, list) or len(v) > limit:
                raise CaptureError('invalid_capture')
            for item in v:
                _event(item, 'market' if key == 'markets' else 'selection')
        elif v is not None and (not isinstance(v, (str, int, float, bool)) or isinstance(v, str) and len(v) > 300):
            raise CaptureError('invalid_capture')
        elif isinstance(v, float) and not math.isfinite(v):
            raise CaptureError('invalid_capture')


def validate_capture(payload: dict, job: dict, now: datetime) -> list[tuple[datetime, dict]]:
    """Defense in depth; no DB/env imports, and no arbitrary raw fields pass through."""
    _only(payload, ['version', 'events'])
    events = payload.get('events')
    if payload.get('version') != VERSION or not isinstance(events, list) or not 1 <= len(events) <= 20:
        raise CaptureError('invalid_capture')
    seen, result = set(), []
    now = ensure_utc(now)
    for item in events:
        _only(item, ['captured_at', 'tournament', 'event'])
        if item.get('tournament') != '美國職籃':
            raise CaptureError('invalid_tournament')
        try:
            at = datetime.fromisoformat(item['captured_at'].replace('Z', '+00:00'))
            event = item['event']
            tip = datetime.fromisoformat(event['tsstart'].replace('Z', '+00:00'))
            if at.tzinfo is None or tip.tzinfo is None:
                raise ValueError()
            at, tip = ensure_utc(at), ensure_utc(tip)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CaptureError('capture_time_invalid') from None
        if at < ensure_utc(parse_utc(job['created_at'])) - timedelta(seconds=30) or now - at > timedelta(minutes=5) or at > now + timedelta(seconds=5):
            raise CaptureError('capture_time_invalid')
        if tip <= now or tpe_date(tip).isoformat() != str(job['betting_day'])[:10]:
            raise CaptureError('game_outside_window')
        _event(event)
        eid = str(event.get('idfoevent') or '')
        if not eid or len(eid) > 100 or eid in seen or not event.get('markets'):
            raise CaptureError('invalid_capture')
        seen.add(eid)
        # Validate every event before the first database write, including canonical prices/lines.
        parsed = twsport.parse_event(event, 'regular', '美國職籃')
        if not parsed.snapshots:
            raise CaptureError('no_valid_markets')
        result.append((at, event))
    return result


def import_capture(job, *, now=None, db=None, run=None, refresh=None, clock=now_utc, progress=None):
    from .ingest import run_odds_job
    now = ensure_utc(now or clock())
    payload = decode_inbox(job['payload'])
    events = validate_capture(payload, job, now)
    run = run or run_odds_job
    summary = dict(games=0, markets=0, new=0, unchanged=0, stale=0, rejected=0, capture_times=[], run_ids=[],
                   ingestion_method=VERSION, received_at=str(job.get('received_at')), decision_refreshed=False, stage='importing')
    problem = False
    for captured, event in events:
        if ensure_utc(clock()) > ensure_utc(parse_utc(job['expires_at'])):
            raise CaptureError('expired')
        raw = {'tournaments': [{'name': '美國職籃', 'stage': 'regular', 'numevents': 1,
                                'event_groups': [{'data': {'events': [event]}}]}], 'event_details': {}}
        def parser(raw, captured=captured):
            parsed = twsport.parse_raw(raw)
            for ev in parsed.events:
                ev.snapshots = [replace(s, raw={**s.raw, 'ingestion_method': VERSION, 'browser_sync_job_id': job['id'],
                                                'browser_captured_at': captured.isoformat(),
                                                'server_received_at': str(job.get('received_at'))}) for s in ev.snapshots]
            return parsed
        rep = run('twsport', fetch=lambda _: raw, parser=parser, fetched_at=captured, db=db)
        summary['games'] += rep.n_matched
        summary['markets'] += rep.diagnostics.get('would_write', 0)
        for key, attr in [('new', 'n_snapshots_new'), ('unchanged', 'n_snapshots_unchanged'), ('stale', 'n_snapshots_stale')]:
            summary[key] += getattr(rep, attr)
        summary['rejected'] += rep.n_unmatched + rep.n_ambiguous + rep.n_rejected
        summary['capture_times'].append(captured.isoformat())
        if rep.run_id is not None:
            summary['run_ids'].append(rep.run_id)
        problem |= rep.outcome != 'success' or rep.n_matched == 0 or rep.n_snapshots_stale > 0 or rep.diagnostics.get('would_write', 0) == 0
        if progress:
            progress(summary)
    if summary['new'] + summary['unchanged'] == 0:
        return 'failed', summary, 'no_snapshots_imported'
    # Existing Python jobs remain the only authority for all pricing/sizing/capacity.
    try:
        if refresh is None:
            from ..pricing.job import pricing_job
            from ..sizing.job import sizing_job
            from ..decision.job import decision_board_job
            def refresh():
                as_of = now_utc()
                summary['stage'] = 'pricing'
                if progress: progress(summary)
                pricing = pricing_job(as_of, db=db)
                summary['stage'] = 'sizing'
                if progress: progress(summary)
                sizing = sizing_job(as_of, db=db)
                summary['stage'] = 'decision'
                if progress: progress(summary)
                decision = decision_board_job(user_ids=[job['user_id']], db=db)
                return not pricing.errors and not sizing.pricing_missing and not sizing.note and not decision.note and decision.users == 1
        summary['decision_refreshed'] = bool(refresh())
        problem |= not summary['decision_refreshed']
    except Exception:
        problem = True
    return ('partial' if problem else 'succeeded'), summary, None if not problem else 'partial_import_or_refresh'


def browser_sync_job(*, db=None):
    if os.environ.get('TWSPORT_SYNC_ENABLED') != 'true':
        return
    if db is None:
        from .. import db
    with db.cursor() as lock:
        lock.execute("SELECT to_regclass('browser_sync_jobs') IS NOT NULL AS ok")
        if not lock.fetchone()['ok']:
            return
        lock.execute("SELECT pg_try_advisory_lock(hashtext('browser_sync:' || current_schema())) AS ok")
        if not lock.fetchone()['ok']:
            return
        worker_db = _ConnectionDb(db, lock.connection)
        try:
            with worker_db.cursor() as cur:
                cur.execute("UPDATE browser_sync_jobs SET state='failed', payload=NULL, error_code='expired', finished_at=%s "
                            "WHERE state IN ('capturing','queued','running') AND expires_at < %s", (now_utc(), now_utc()))
                db.heartbeat(cur, status='ok', records_updated=0, **HEARTBEAT)
            with worker_db.transaction() as cur:
                cur.execute("SELECT * FROM browser_sync_jobs WHERE state='queued' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED")
                job = cur.fetchone()
                if not job:
                    return
                started = now_utc()
                # The upload deadline stays five minutes; a claimed job gets its own bounded processing lease.
                # validate_capture still rejects observations older than five minutes at processing start.
                job['expires_at'] = started + PROCESSING_LEASE
                cur.execute("UPDATE browser_sync_jobs SET state='running', started_at=%s, expires_at=%s WHERE id=%s",
                            (started, job['expires_at'], job['id']))
            try:
                def progress(summary):
                    # Reuse the lock connection; do not occupy another slot in the shared small pool.
                    lock.execute("UPDATE browser_sync_jobs SET summary=%s WHERE id=%s AND state='running'",
                                 (json.dumps(summary), job['id']))
                    db.heartbeat(lock, status='ok', records_updated=summary.get('new', 0), **HEARTBEAT)
                state, summary, error = import_capture(job, db=worker_db, progress=progress)
            except Exception as exc:
                # Do not log or persist arbitrary payload/exception text.
                state, summary = 'failed', {'decision_refreshed': False}
                error = str(exc) if isinstance(exc, CaptureError) else 'processing_failed'
            with worker_db.transaction() as cur:
                cur.execute("UPDATE browser_sync_jobs SET state=%s, summary=%s, error_code=%s, payload=NULL, finished_at=%s "
                            "WHERE id=%s AND state='running'", (state, json.dumps(summary), error, now_utc(), job['id']))
                db.heartbeat(cur, status='ok', records_updated=summary.get('new', 0), **HEARTBEAT)
        finally:
            lock.execute("SELECT pg_advisory_unlock(hashtext('browser_sync:' || current_schema()))")
