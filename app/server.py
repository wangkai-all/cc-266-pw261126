"""HTTP 服务：纯标准库 ThreadingHTTPServer，提供 API 与静态页面。

所有数据接口都经过 Executor / JobManager / Store，不存在绕过策略的旁路。
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .default_policy import upgraded_policy
from .executor import Executor, QueryError
from .jobs import JobManager
from .policy import PolicyError
from .preview import preview_impact
from .store import Store

STATIC_DIR = os.path.join(os.path.dirname(__file__), "web")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Handler(BaseHTTPRequestHandler):
    store: Store = None
    executor: Executor = None
    jobs: JobManager = None

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):  # 简洁日志
        pass

    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "缺少请求体")
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"JSON 解析失败: {exc}")

    def _file(self, path: str):
        full = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            data = fh.read()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # --------------------------------------------------------------- routing
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        path = self.path.split("?", 1)[0]
        try:
            if method == "GET" and (path == "/" or path == "/index.html"):
                return self._file("index.html")
            if method == "GET" and path.startswith("/static/"):
                return self._file(path[len("/static/"):])
            if path == "/api/health":
                return self._json({"ok": True,
                                   "active_version": self.store.active_version()})
            if method == "GET" and path == "/api/policies":
                return self._json({
                    "active_version": self.store.active_version(),
                    "versions": self.store.list_versions(),
                })
            if method == "GET" and path.startswith("/api/policies/"):
                return self._get_policy(int(path.rsplit("/", 1)[1]))
            if method == "GET" and path == "/api/purpose/catalog":
                return self._catalog()
            if method == "POST" and path == "/api/query":
                return self._query()
            if method == "POST" and path == "/api/query/preview-policy":
                return self._preview_policy()
            if method == "POST" and path == "/api/policies/draft":
                return self._save_draft()
            if method == "POST" and path == "/api/policies/publish":
                return self._publish()
            if method == "POST" and path == "/api/policies/seed-upgrade":
                return self._seed_upgrade()
            if method == "POST" and path == "/api/jobs":
                return self._submit_job()
            if method == "GET" and path == "/api/jobs":
                return self._json({"jobs": self.jobs.list_jobs()})
            if method == "GET" and re.fullmatch(
                    r"/api/jobs/[0-9a-f]{32}/download", path):
                return self._download(path.split("/")[3])
            if method == "GET" and path == "/api/audit":
                return self._json({"audit": self.store.list_audit()})
            raise ApiError(HTTPStatus.NOT_FOUND, f"无此接口: {path}")
        except ApiError as exc:
            self._json({"error": str(exc)}, exc.status)
        except (QueryError, PolicyError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - 统一兜底，避免堆栈外泄
            self._json({"error": f"服务器内部错误: {exc}"},
                       HTTPStatus.INTERNAL_SERVER_ERROR)

    # ------------------------------------------------------------- api impls
    def _get_policy(self, version: int):
        self._json({"version": version, "doc": self.store.get_policy_doc(version)})

    def _catalog(self):
        """前端构建查询用：当前活动策略下各用途暴露的列/关联/k 值。"""
        version, doc = self.store.active_policy()
        catalog = {"version": version, "purposes": {}}
        for purpose, pconf in doc["purposes"].items():
            catalog["purposes"][purpose] = {
                "k": pconf["k"],
                "allow_detail": pconf["allow_detail"],
                "tables": {
                    t: {"columns": [c["name"] for c in tc["columns"]],
                        "joins": tc.get("joins", [])}
                    for t, tc in pconf["tables"].items()
                },
            }
        self._json(catalog)

    def _query(self):
        payload = self._body()
        purpose = payload.get("purpose")
        query = payload.get("query")
        if not purpose or not isinstance(query, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "需要 purpose 与 query")
        self._json(self.executor.run(purpose, query))

    def _preview_policy(self):
        draft = self._body().get("doc")
        if not isinstance(draft, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "需要策略 doc")
        self._json(preview_impact(self.store, self.executor, draft))

    def _save_draft(self):
        doc = self._body().get("doc")
        if not isinstance(doc, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "需要策略 doc")
        self.store.save_draft(doc)
        self._json({"saved": True, "version": doc["version"]})

    def _publish(self):
        doc = self._body().get("doc")
        if not isinstance(doc, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "需要策略 doc")
        version = self.store.publish(doc)
        self._json({"published": True, "active_version": version})

    def _seed_upgrade(self):
        """一键生成 v2 收紧草稿（演示规则升级；仍需人工预览+发布）。"""
        current = self.store.active_version()
        draft = upgraded_policy(current + 1)
        self.store.save_draft(draft)
        self._json({"saved": True, "draft": draft})

    def _submit_job(self):
        payload = self._body()
        purpose, query = payload.get("purpose"), payload.get("query")
        if not purpose or not isinstance(query, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "需要 purpose 与 query")
        job_id = self.jobs.submit(purpose, query)
        self._json({"job_id": job_id}, HTTPStatus.ACCEPTED)

    def _download(self, job_id: str):
        path, job = self.jobs.download_path(job_id)
        with open(path, "rb") as fh:
            data = fh.read()
        filename = f"export_{job['purpose']}_v{job['executed_version']}.csv"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename*=UTF-8''{filename}")
        self.send_header("X-Policy-Version", str(job["executed_version"]))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build_server(db_path: str, export_dir: str, host: str = "127.0.0.1",
                 port: int = 8000) -> tuple[ThreadingHTTPServer, Store, JobManager]:
    store = Store(db_path)
    executor = Executor(store)
    jobs = JobManager(store, executor, export_dir)
    Handler.store = store
    Handler.executor = executor
    Handler.jobs = jobs
    server = ThreadingHTTPServer((host, port), Handler)
    return server, store, jobs
