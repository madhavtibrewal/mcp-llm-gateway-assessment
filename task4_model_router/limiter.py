"""Per-tenant token rate limiter: a sliding window stored in an on-disk SQLite file.

Every admitted request is a row (tenant, timestamp, tokens). A tenant's usage
is the sum of its rows from the last 60 seconds, so the window slides
smoothly instead of resetting at the top of each minute.
"""

import asyncio
import sqlite3
import time
from contextlib import closing


class RateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited, retry after {retry_after:.1f}s")
        self.retry_after = retry_after


class TokenRateLimiter:
    def __init__(self, db_path: str, max_tokens: int = 50_000, window_seconds: float = 60, clock=time.time):
        self.db_path = db_path
        self.max_tokens = max_tokens
        self.window = window_seconds
        self.clock = clock  # injectable so tests can move time forward
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
            db.execute("CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, ts REAL NOT NULL, tokens INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_by_tenant ON usage (tenant, ts)")

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: we run BEGIN/COMMIT ourselves.
        # timeout: how long to wait if another request holds the write lock.
        return sqlite3.connect(self.db_path, timeout=10, isolation_level=None)

    # SQLite calls block, so the async methods run them in a worker thread.
    async def reserve(self, tenant: str, tokens: int) -> int:
        return await asyncio.to_thread(self._reserve, tenant, tokens)

    async def update(self, row_id: int, tokens: int):
        await asyncio.to_thread(self._execute, "UPDATE usage SET tokens = ? WHERE id = ?", (tokens, row_id))

    async def release(self, row_id: int):
        await asyncio.to_thread(self._execute, "DELETE FROM usage WHERE id = ?", (row_id,))

    def _reserve(self, tenant: str, tokens: int) -> int:
        """Records the tokens and returns the row id, or raises RateLimited."""
        now = self.clock()
        with closing(self._connect()) as db:
            # IMMEDIATE takes the write lock now. Without it, two requests could both
            # read "49,000 used", both decide they fit, and both get in.
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute("DELETE FROM usage WHERE ts <= ?", (now - self.window,))  # drop expired rows
                used = db.execute("SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = ?", (tenant,)).fetchone()[0]
                if used + tokens > self.max_tokens:
                    retry_after = self._retry_after(db, tenant, used, tokens, now)
                    db.execute("COMMIT")
                    raise RateLimited(retry_after)
                row_id = db.execute("INSERT INTO usage (tenant, ts, tokens) VALUES (?, ?, ?)", (tenant, now, tokens)).lastrowid
                db.execute("COMMIT")
                return row_id
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def _retry_after(self, db, tenant, used, tokens, now) -> float:
        # Walk the tenant's rows oldest first until enough of them would have expired.
        for ts, row_tokens in db.execute("SELECT ts, tokens FROM usage WHERE tenant = ? ORDER BY ts", (tenant,)):
            used -= row_tokens
            if used + tokens <= self.max_tokens:
                return ts + self.window - now
        return self.window

    def _execute(self, sql: str, params: tuple):
        with closing(self._connect()) as db:
            db.execute(sql, params)

    def usage(self, tenant: str) -> int:
        with closing(self._connect()) as db:
            query = "SELECT COALESCE(SUM(tokens), 0) FROM usage WHERE tenant = ? AND ts > ?"
            return db.execute(query, (tenant, self.clock() - self.window)).fetchone()[0]
