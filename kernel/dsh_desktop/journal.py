"""Durable request identities and write-ahead dispatch boundaries."""
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .targets import RuntimeFault


FINAL = {'completed', 'failed', 'cancelled', 'unknown'}


class OperationJournal:
    def __init__(self, path, epoch, *, result_ttl=86400, identity_ttl=30 * 86400):
        self.epoch = epoch
        self.result_ttl = result_ttl
        self.identity_ttl = identity_ttl
        self._lock = threading.RLock()
        self._closed = False
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.execute('''CREATE TABLE IF NOT EXISTS operations (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_key TEXT NOT NULL,
            digest TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            data TEXT NOT NULL, UNIQUE(owner, request_key))''')
        with self._db:
            rows = self._db.execute('SELECT id, data FROM operations').fetchall()
            for operation_id, raw in rows:
                data = json.loads(raw)
                if data['status'] not in FINAL:
                    uncertain = data.get('in_flight_step') is not None
                    data.update(status='unknown' if uncertain else 'cancelled',
                                outcome='unknown' if uncertain else 'interrupted', worker_running=False,
                                message='Runtime restarted during this operation. Observe business state before issuing remaining steps',
                                next_action='observe')
                    if data.get('in_flight_step') is not None:
                        data['uncertain_step'] = data['in_flight_step']
                        data['dispatch'] = 'unknown'
                    self._write(operation_id, data)
        self.prune()

    def _write(self, operation_id, data):
        self._db.execute('UPDATE operations SET updated=?, data=? WHERE id=?',
                         (time.time(), json.dumps(data, ensure_ascii=False), operation_id))

    def prune(self):
        now = time.time()
        with self._lock, self._db:
            rows = self._db.execute('SELECT id, created, data FROM operations').fetchall()
            for operation_id, created, raw in rows:
                data = json.loads(raw)
                if data['status'] not in FINAL:
                    continue
                if created < now - self.identity_ttl:
                    self._db.execute('DELETE FROM operations WHERE id=?', (operation_id,))
                elif created < now - self.result_ttl and 'result' in data:
                    data.pop('result')
                    data['result_expired'] = True
                    self._write(operation_id, data)

    def register(self, owner, key, digest, kind, target_id=None):
        if not isinstance(key, str) or not key or len(key) > 256:
            raise ValueError('A stable request key of 1 to 256 characters is required')
        with self._lock, self._db:
            self._db.execute('BEGIN IMMEDIATE')
            row = self._db.execute('SELECT digest, data FROM operations WHERE owner=? AND request_key=?', (owner, key)).fetchone()
            if row:
                if row[0] != digest:
                    raise RuntimeFault('IDEMPOTENCY_CONFLICT', 'Request key already identifies different arguments')
                return json.loads(row[1]), False
            if self._db.execute('SELECT count(*) FROM operations').fetchone()[0] >= 10000:
                raise RuntimeFault('OPERATION_LIMIT', 'Operation journal is full. Retained request identities require reconciliation')
            operation_id = 'op_' + uuid.uuid4().hex
            now = time.time()
            data = {'success': True, 'operation_id': operation_id, 'epoch': self.epoch, 'kind': kind,
                    'target_id': target_id, 'status': 'accepted', 'dispatch': 'none', 'outcome': 'dispatched',
                    'completed_steps': [], 'steps': [], 'in_flight_step': None,
                    'expectation': {'status': 'not_requested'}, 'worker_running': False, 'revision': 0}
            self._db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                             (operation_id, owner, key, digest, now, now, json.dumps(data)))
            return data, True

    def get(self, owner, operation_id=None, *, request_key=None):
        with self._lock:
            if request_key is not None:
                row = self._db.execute('SELECT data FROM operations WHERE owner=? AND request_key=?', (owner, request_key)).fetchone()
            else:
                row = self._db.execute('SELECT data FROM operations WHERE owner=? AND id=?', (owner, operation_id)).fetchone()
            if row is None:
                raise RuntimeFault('OPERATION_NOT_FOUND', 'Operation is unavailable in this session')
            return json.loads(row[0])

    def change(self, owner, operation_id, **fields):
        with self._lock, self._db:
            data = self.get(owner, operation_id)
            data.update(fields)
            data['revision'] += 1
            self._write(operation_id, data)
            return data

    def active(self, owner):
        with self._lock:
            rows = self._db.execute('SELECT data FROM operations WHERE owner=?', (owner,)).fetchall()
            return [data for raw, in rows if (data := json.loads(raw))['status'] not in FINAL]

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
