"""导出任务：后台执行、版本绑定、规则升级后的恢复语义。

规则：
* 任务记录创建时的 requested_version；执行前重新读取活动版本，
  若版本已升级，则按新版本重算（executed_version 记录实际版本），
  旧结果绝不复用 —— 旧任务恢复不得越过新限制。
* 发布动作把所有 done 任务置 revoked 并删除结果文件，下载接口拒绝 revoked。
* 导出走的是与在线查询同一个 Executor，同样过列白名单/k 匿名/缓存。
"""
from __future__ import annotations

import csv
import json
import os
import queue
import threading
import time
import uuid

from .executor import QueryError
from .policy import PolicyError


class JobManager:
    def __init__(self, store, executor, export_dir: str):
        self.store = store
        self.executor = executor
        self.export_dir = export_dir
        os.makedirs(export_dir, exist_ok=True)
        self._queue: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self.worker = threading.Thread(target=self._loop, name="job-worker",
                                       daemon=True)
        self.worker.start()
        self._recover_on_startup()

    def _recover_on_startup(self) -> None:
        """进程重启：未完成任务重新入队；已完成任务保持原状态（含 revoked）。"""
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','running')"
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            self._queue.put(r["id"])

    def submit(self, purpose: str, query: dict) -> str:
        job_id = uuid.uuid4().hex
        version = self.store.active_version()
        with self.store.lock:
            conn = self.store.connect()
            try:
                conn.execute(
                    "INSERT INTO jobs(id,purpose,query_json,status,"
                    "requested_version,created_at) VALUES(?,?,?,'queued',?,?)",
                    (job_id, purpose, json.dumps(query, ensure_ascii=False),
                     version, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        self.store.audit(purpose, "job_submit", requested_version=version,
                         executed_version=None, query=query,
                         detail=f"任务 {job_id} 入队")
        self._queue.put(job_id)
        return job_id

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._run_one(job_id)

    def _run_one(self, job_id: str) -> None:
        conn = self.store.connect()
        try:
            job = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        finally:
            conn.close()
        if job is None or job["status"] == "revoked":
            return
        active_version = self.store.active_version()
        query = json.loads(job["query_json"])
        with self.store.lock:
            conn = self.store.connect()
            try:
                conn.execute(
                    "UPDATE jobs SET status='running' WHERE id=? AND status='queued'",
                    (job_id,))
                conn.commit()
            finally:
                conn.close()
        try:
            # 关键：以当前活动策略执行，而非任务创建时的旧版本
            result = self.executor.run(job["purpose"], query)
            detail = (f"任务创建于 v{job['requested_version']}，按当前 "
                      f"v{result['policy_version']} 执行")
            path = os.path.join(self.export_dir, f"{job_id}.csv")
            self._write_csv(path, result["rows"])
            with self.store.lock:
                conn = self.store.connect()
                try:
                    conn.execute(
                        "UPDATE jobs SET status='done', executed_version=?, "
                        "cache_hit=?, row_count=?, result_path=?, error=?, "
                        "finished_at=? WHERE id=?",
                        (result["policy_version"], int(result["cache_hit"]),
                         len(result["rows"]), path, None, time.time(), job_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            self.store.audit(job["purpose"], "job_done",
                             requested_version=job["requested_version"],
                             executed_version=result["policy_version"],
                             cache_hit=result["cache_hit"],
                             row_count=len(result["rows"]), query=query,
                             detail=detail)
        except (QueryError, PolicyError) as exc:
            with self.store.lock:
                conn = self.store.connect()
                try:
                    conn.execute(
                        "UPDATE jobs SET status='failed', error=?, "
                        "executed_version=?, finished_at=? WHERE id=?",
                        (str(exc), active_version, time.time(), job_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            self.store.audit(job["purpose"], "job_failed",
                             requested_version=job["requested_version"],
                             executed_version=active_version, query=query,
                             detail=str(exc))

    @staticmethod
    def _write_csv(path: str, rows: list[dict]) -> None:
        if not rows:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                fh.write("# 无满足规则的记录（可能被 k-匿名抑制）\n")
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def get(self, job_id: str) -> dict | None:
        conn = self.store.connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_jobs(self) -> list[dict]:
        conn = self.store.connect()
        try:
            rows = conn.execute(
                "SELECT id,purpose,status,requested_version,executed_version,"
                "cache_hit,row_count,error,created_at,finished_at "
                "FROM jobs ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def download_path(self, job_id: str) -> tuple[str, dict]:
        job = self.get(job_id)
        if job is None:
            raise QueryError("任务不存在")
        if job["status"] == "revoked":
            raise QueryError(
                f"任务结果已按规则撤回（创建于 v{job['requested_version']}，"
                "新版本已发布），请重新提交导出")
        if job["status"] != "done" or not job["result_path"]:
            raise QueryError(f"任务尚未完成（状态: {job['status']}）")
        if not os.path.exists(job["result_path"]):
            raise QueryError("结果文件缺失，请重新提交导出")
        return job["result_path"], job

    def shutdown(self) -> None:
        self._stop.set()
        self.worker.join(timeout=5)
