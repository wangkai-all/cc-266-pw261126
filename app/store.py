"""存储层：SQLite 连接管理、版本表、事务化发布、伪标识 UDF、查询缓存、任务与审计。

并发模型（避免共享连接死锁）：
* 每次数据库操作使用独立连接（WAL：多读者 + 单写者），读不加全局锁；
* 写操作经 self._writelock 串行化，配合 busy_timeout 等待写锁而非互锁；
* 发布事务在单连接单事务内完成，外部永远看不到半成品版本。

关键一致性约定：
* 一次发布在单事务内完成「写新版本 -> 建全部用途视图 -> 切换活动版本 -> 作废旧缓存」，
  任何一步失败整体回滚，杜绝升级期间新旧范围混出。
* 查询缓存以 (策略版本, 用途, 查询指纹) 为键，发布即清空，缓存不可能命中旧规则结果。
* 已完成的导出任务在发布时置为 revoked，旧范围数据不能继续被下载。
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time

from .policy import (
    PolicyError,
    build_view_ddl,
    validate_policy,
    view_name,
)

HEX_RE = re.compile(r"^[0-9a-f]{16,}$")
CACHE_MAX = 256
BUSY_TIMEOUT_MS = 5000


def _pse(value: str | None, key: str) -> str | None:
    if value is None:
        return None
    digest = hmac.new(key.encode(), str(value).encode(), hashlib.sha256)
    return "p_" + digest.hexdigest()[:16]


class Store:
    def __init__(self, path: str):
        self.path = path
        # 写锁：串行化所有写事务；读不持此锁（WAL 允许并发读）
        self._writelock = threading.RLock()
        # 进程内结果缓存，自身带锁；键含策略版本，发布即清空
        self._cache: collections.OrderedDict[tuple, dict] = collections.OrderedDict()
        self._cache_lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------- connection
    def connect(self) -> sqlite3.Connection:
        """每次操作独立连接：跨线程安全，避免共享连接上的读写互锁。"""
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.create_function("pse", 2, _pse, deterministic=True)
        return conn

    # 写锁：外部模块通过 store.lock 串行化写事务（如初始化建表）
    @property
    def lock(self) -> threading.RLock:
        return self._writelock

    def txn_connection(self) -> sqlite3.Connection:
        """写事务入口：持写锁并返回已开启 IMMEDIATE 事务的连接。

        调用方负责 commit/rollback 并在 finally 中 close、释放锁。
        典型用法见 publish()。
        """
        self._writelock.acquire()
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        return conn

    def release(self, conn: sqlite3.Connection) -> None:
        try:
            conn.close()
        finally:
            self._writelock.release()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        with self._writelock:
            conn = self.connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS policy_meta(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS policies(
                        version INTEGER PRIMARY KEY,
                        doc TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('draft','active','archived')),
                        note TEXT,
                        created_at REAL NOT NULL,
                        published_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS jobs(
                        id TEXT PRIMARY KEY,
                        purpose TEXT NOT NULL,
                        query_json TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(
                            status IN ('queued','running','done','failed','revoked')),
                        requested_version INTEGER NOT NULL,
                        executed_version INTEGER,
                        cache_hit INTEGER,
                        row_count INTEGER,
                        result_path TEXT,
                        error TEXT,
                        created_at REAL NOT NULL,
                        finished_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS audit_log(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts REAL NOT NULL,
                        purpose TEXT NOT NULL,
                        action TEXT NOT NULL,
                        requested_version INTEGER,
                        executed_version INTEGER,
                        cache_hit INTEGER,
                        row_count INTEGER,
                        query_json TEXT,
                        detail TEXT
                    );
                    """
                )
                conn.commit()
            finally:
                conn.close()

    # -------------------------------------------------------------- policy i/o
    def active_version(self) -> int | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT value FROM policy_meta WHERE key='active_version'"
            ).fetchone()
            return int(row["value"]) if row else None
        finally:
            conn.close()

    def get_policy_doc(self, version: int) -> dict:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT doc FROM policies WHERE version=?", (version,)
            ).fetchone()
            if row is None:
                raise PolicyError(f"版本 {version} 不存在")
            return json.loads(row["doc"])
        finally:
            conn.close()

    def active_policy(self) -> tuple[int, dict]:
        version = self.active_version()
        if version is None:
            raise PolicyError("尚未发布任何策略版本")
        return version, self.get_policy_doc(version)

    def list_versions(self) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT version, status, note, created_at, published_at "
                "FROM policies ORDER BY version"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def save_draft(self, doc: dict) -> None:
        validate_policy(doc)
        if not HEX_RE.match(doc["salt"]):
            raise PolicyError("salt 必须是 >=16 位的十六进制串，才能安全内嵌为视图密钥")
        with self._writelock:
            conn = self.connect()
            try:
                conn.execute(
                    "INSERT INTO policies(version, doc, status, note, created_at) "
                    "VALUES(?,?, 'draft', ?, ?) "
                    "ON CONFLICT(version) DO UPDATE SET doc=excluded.doc, note=excluded.note",
                    (doc["version"], json.dumps(doc, ensure_ascii=False),
                     doc.get("note"), time.time()),
                )
                conn.commit()
            finally:
                conn.close()

    def publish(self, doc: dict) -> int:
        """原子发布：校验 -> 建视图 -> 切活动版本 -> 失效缓存/旧导出。"""
        validate_policy(doc)
        if not HEX_RE.match(doc["salt"]):
            raise PolicyError("salt 必须是 >=16 位的十六进制串，才能安全内嵌为视图密钥")
        version = doc["version"]
        conn = self.txn_connection()  # 持写锁 + BEGIN IMMEDIATE
        try:
            self._create_version_views(conn, doc)
            now = time.time()
            old = self.active_version()
            conn.execute(
                "INSERT INTO policies(version, doc, status, note, created_at, published_at) "
                "VALUES(?,?,'active',?,?,?) "
                "ON CONFLICT(version) DO UPDATE SET doc=excluded.doc, "
                "status='active', note=excluded.note, published_at=excluded.published_at",
                (version, json.dumps(doc, ensure_ascii=False),
                 doc.get("note"), now, now),
            )
            if old is not None and old != version:
                conn.execute(
                    "UPDATE policies SET status='archived' WHERE version=?",
                    (old,))
            conn.execute(
                "INSERT INTO policy_meta(key,value) VALUES('active_version',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(version),))
            # 规则撤回：旧范围导出立即失效，队列任务改由执行器按新版本重算
            conn.execute(
                "UPDATE jobs SET status='revoked', finished_at=? "
                "WHERE status='done'", (now,))
            conn.execute(
                "INSERT INTO audit_log(ts,purpose,action,requested_version,"
                "executed_version,detail) VALUES(?,?,?,?,?,?)",
                (now, "*", "publish", old, version,
                 f"发布 v{version}" + (f"，旧版 v{old} 归档" if old else ""))),
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self.release(conn)
        with self._cache_lock:
            self._cache.clear()
        return version

    def _create_version_views(self, conn: sqlite3.Connection, doc: dict) -> None:
        for purpose, pconf in doc["purposes"].items():
            for table, tconf in pconf["tables"].items():
                ddl, _ = build_view_ddl(
                    doc["version"], purpose, table, tconf, doc["salt"])
                # 重发同版本草稿时先删旧视图
                conn.execute(
                    f"DROP VIEW IF EXISTS {view_name(doc['version'], purpose, table)}")
                conn.execute(ddl)

    def preview_connection(self, doc: dict) -> "PreviewSession":
        """在独立连接上用 TEMP VIEW 试运行草稿，不污染正式视图。

        预览视图统一映射到固定版本名（PREVIEW_VERSION），
        使同一会话/不同文档的对比不依赖文档自身版本号；
        TEMP VIEW 随连接关闭即销毁，绝不触碰正式版本。
        """
        validate_policy(doc)
        if not HEX_RE.match(doc["salt"]):
            raise PolicyError("salt 必须是 >=16 位的十六进制串")
        return PreviewSession(self.path, doc)

    def preview_version(self) -> int:
        return PreviewSession.PREVIEW_VERSION

    # ------------------------------------------------------------------- cache
    def cache_get(self, version: int, purpose: str, query: dict) -> dict | None:
        key = self._cache_key(version, purpose, query)
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
            return hit

    def cache_put(self, version: int, purpose: str, query: dict, result: dict) -> None:
        key = self._cache_key(version, purpose, query)
        with self._cache_lock:
            self._cache[key] = result
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_MAX:
                self._cache.popitem(last=False)

    @staticmethod
    def _cache_key(version: int, purpose: str, query: dict) -> tuple:
        fingerprint = hashlib.sha256(
            json.dumps(query, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        return version, purpose, fingerprint

    # ------------------------------------------------------------------ audits
    def audit(self, purpose: str, action: str, *, requested_version: int | None,
              executed_version: int | None, cache_hit: bool | None = None,
              row_count: int | None = None, query: dict | None = None,
              detail: str | None = None) -> None:
        with self._writelock:
            conn = self.connect()
            try:
                conn.execute(
                    "INSERT INTO audit_log(ts,purpose,action,requested_version,"
                    "executed_version,cache_hit,row_count,query_json,detail) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (time.time(), purpose, action, requested_version, executed_version,
                     None if cache_hit is None else int(cache_hit), row_count,
                     None if query is None else json.dumps(query, ensure_ascii=False),
                     detail),
                )
                conn.commit()
            finally:
                conn.close()

    def list_audit(self, limit: int = 100) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def close(self) -> None:
        # 连接均为按需创建并即时关闭，这里只需保证缓存可丢弃
        with self._cache_lock:
            self._cache.clear()


class PreviewSession:
    """草稿预览：独立连接 + TEMP VIEW，关闭即销毁，绝不触碰正式版本。"""

    PREVIEW_VERSION = 999999  # 预览专用固定版本号，不会与正式版本冲突

    def __init__(self, db_path: str, doc: dict):
        self.doc = doc
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("pse", 2, _pse, deterministic=True)
        for purpose, pconf in doc["purposes"].items():
            for table, tconf in pconf["tables"].items():
                # 用文档自身版本生成 DDL（保证密钥派生与发布后一致），
                # 再把视图名替换成固定预览名，避免版本号干扰对比
                ddl, _ = build_view_ddl(
                    doc["version"], purpose, table, tconf, doc["salt"])
                fixed = view_name(self.PREVIEW_VERSION, purpose, table)
                ddl = ddl.replace(
                    view_name(doc["version"], purpose, table), fixed, 1)
                self.conn.execute(ddl.replace("CREATE VIEW", "CREATE TEMP VIEW", 1))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PreviewSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
