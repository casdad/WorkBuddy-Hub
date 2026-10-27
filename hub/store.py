import asyncio
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from .concurrency import complete

SCHEMA = """
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions(token TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE accounts(
 id INTEGER PRIMARY KEY, provider TEXT NOT NULL, uid TEXT NOT NULL, name TEXT NOT NULL,
 credential TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 enabled INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'ready',
 cooldown REAL NOT NULL DEFAULT 0, max_concurrency INTEGER NOT NULL DEFAULT 1,
 balance_micro INTEGER, balance_at REAL, balance_source TEXT,
 auth_expires REAL, last_error TEXT, created REAL NOT NULL,
 UNIQUE(provider,uid));
CREATE TABLE pools(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, strategy TEXT NOT NULL DEFAULT 'balanced');
CREATE TABLE pool_members(pool_id INTEGER REFERENCES pools(id) ON DELETE CASCADE,
 account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE, PRIMARY KEY(pool_id,account_id));
CREATE TABLE models(account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
 model TEXT NOT NULL, capabilities TEXT NOT NULL, verified REAL, PRIMARY KEY(account_id,model));
CREATE TABLE aliases(alias TEXT PRIMARY KEY, model TEXT NOT NULL);
CREATE TABLE api_keys(id INTEGER PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
 pool_id INTEGER NOT NULL REFERENCES pools(id), models TEXT NOT NULL DEFAULT '[]', enabled INTEGER NOT NULL DEFAULT 1,
 expires REAL, request_limit INTEGER, used INTEGER NOT NULL DEFAULT 0, reserved INTEGER NOT NULL DEFAULT 0,
 rpm INTEGER NOT NULL DEFAULT 60, max_concurrency INTEGER NOT NULL DEFAULT 2, created REAL NOT NULL);
CREATE TABLE packages(id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
 package_id TEXT NOT NULL, name TEXT NOT NULL, remaining_micro INTEGER, expires REAL, observed REAL NOT NULL,
 source TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE snapshots(id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,
 remaining_micro INTEGER, observed REAL NOT NULL, source TEXT NOT NULL);
CREATE TABLE requests(id TEXT PRIMARY KEY, created REAL NOT NULL, key_id INTEGER REFERENCES api_keys(id),
 account_id INTEGER REFERENCES accounts(id), model TEXT NOT NULL, upstream_model TEXT NOT NULL,
 state TEXT NOT NULL, status INTEGER, input_tokens INTEGER, output_tokens INTEGER, credits_micro INTEGER,
 usage_source TEXT NOT NULL DEFAULT 'unknown', latency_ms INTEGER, ttft_ms INTEGER, error TEXT,
 settled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE attempts(id INTEGER PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
 account_id INTEGER REFERENCES accounts(id), created REAL NOT NULL, status INTEGER, error TEXT);
CREATE TABLE ledger(request_id TEXT PRIMARY KEY REFERENCES requests(id) ON DELETE CASCADE,
 key_id INTEGER REFERENCES api_keys(id), units INTEGER NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE jobs(id INTEGER PRIMARY KEY, created REAL NOT NULL, account_id INTEGER,
 kind TEXT NOT NULL, state TEXT NOT NULL, message TEXT);
CREATE TABLE audit(id INTEGER PRIMARY KEY, created REAL NOT NULL, action TEXT NOT NULL, target TEXT);
CREATE INDEX requests_created ON requests(created);
CREATE INDEX requests_key_time ON requests(key_id,created);
INSERT INTO pools(id,name) VALUES(1,'默认账号池');
PRAGMA user_version=1;
"""


MIGRATIONS = {
    # 1 -> 2: explicit fallback pool chain.
    1: 'ALTER TABLE pools ADD COLUMN fallback_pool_id INTEGER REFERENCES pools(id); PRAGMA user_version=2;',
    # 2 -> 3: revealable API keys, plus per-request endpoint and cache accounting.
    2: ('ALTER TABLE api_keys ADD COLUMN secret TEXT;'
        'ALTER TABLE requests ADD COLUMN endpoint TEXT;'
        'ALTER TABLE requests ADD COLUMN cache_tokens INTEGER;'
        'PRAGMA user_version=3;'),
}


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "hub.db"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
                version = 1
            if version not in (1, 2, 3):
                raise RuntimeError("不支持此数据库版本")
            # Each step is its own transaction: a failure leaves the old version.
            while version in MIGRATIONS:
                conn.executescript("BEGIN IMMEDIATE;" + MIGRATIONS[version] + "COMMIT;")
                version += 1
        # A crashed request might already have consumed upstream credits. Charge
        # the local request unit conservatively; do not invent token/credit facts.
        with self.tx() as conn:
            conn.execute("INSERT OR IGNORE INTO ledger SELECT id,key_id,1,'process_restart',? FROM requests WHERE settled=0",(time.time(),))
            conn.execute("""WITH pending AS (SELECT key_id,COUNT(*) AS n FROM requests WHERE settled=0 GROUP BY key_id)
                UPDATE api_keys SET reserved=MAX(0,reserved-(SELECT n FROM pending WHERE key_id=api_keys.id)),
                used=used+(SELECT n FROM pending WHERE key_id=api_keys.id) WHERE id IN (SELECT key_id FROM pending)""")
            conn.execute("UPDATE requests SET state='interrupted',settled=1,error='process_restart' WHERE settled=0")
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='hub-sqlite')

    async def run(self, function, *args):
        # Await every commit; this is not fire-and-forget/write-behind storage.
        future = asyncio.get_running_loop().run_in_executor(self.worker, partial(function,*args))
        return await complete(future)

    async def aclose(self):
        await complete(asyncio.to_thread(self.worker.shutdown, wait=True))

    async def aall(self, sql, params=()):
        return await self.run(self.all,sql,params)

    async def aone(self, sql, params=()):
        return await self.run(self.one,sql,params)

    async def aexecute(self, sql, params=()):
        return await self.run(self.execute,sql,params)

    async def aget(self, key, default=None):
        return await self.run(self.get,key,default)

    async def aset(self, key, value):
        return await self.run(self.set,key,value)

    async def aaudit(self, action, target=''):
        return await self.run(self.audit,action,target)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @contextmanager
    def tx(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def all(self, sql, params=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def execute(self, sql, params=()):
        with self.tx() as conn:
            return conn.execute(sql, params).lastrowid

    def get(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?",(key,))
        return json.loads(row['value']) if row else default

    def set(self, key, value):
        self.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,json.dumps(value)))

    def audit(self, action, target=''):
        self.execute("INSERT INTO audit(created,action,target) VALUES(?,?,?)",(time.time(),action,str(target)))
