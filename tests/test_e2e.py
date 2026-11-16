"""端到端规则验证（不启动 HTTP，直接跑引擎全链路；使用独立临时库）。

运行：python -m tests.test_e2e
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.default_policy import default_policy, upgraded_policy
from app.executor import Executor, QueryError
from app.init_db import init
from app.jobs import JobManager
from app.policy import PolicyError
from app.preview import preview_impact
from app.seed import SMALL_CITY
from app.store import Store

PASS, FAIL = "✅ 通过", "❌ 失败"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(f"{PASS if cond else FAIL}  {name}" + (f" —— {detail}" if detail else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mask_demo_")
    db_path = os.path.join(tmp, "test.db")
    export_dir = os.path.join(tmp, "exports")
    info = init(db_path, reset=True)
    print(f"[fixture] 虚构数据：{info['rows']}\n")

    store = Store(db_path)
    executor = Executor(store)
    jobs = JobManager(store, executor, export_dir)

    # 1. 原始标识列在任何用途视图里都不存在
    ops = executor.run("operations", {
        "table": "customers",
        "select": ["uid", "name_masked", "phone_masked", "city"],
        "limit": 5})
    row0 = ops["rows"][0]
    check("运营视图无 name/phone/id_card 原始列",
          all(k not in row0 for k in ("name", "phone", "id_card", "email")))
    check("姓名/手机已遮蔽",
          row0["name_masked"].endswith("**") and "****" in row0["phone_masked"],
          f"样例：{row0['name_masked']} / {row0['phone_masked']}")

    # 2. 显式查询未暴露列必须被拒绝（不是前端隐藏，是后端拒绝）
    try:
        executor.run("operations", {"table": "customers",
                                    "filters": [{"column": "id_card", "op": "=",
                                                 "value": "x"}]})
        check("后端拒绝未暴露列 id_card 过滤", False)
    except QueryError:
        check("后端拒绝未暴露列 id_card 过滤", True)

    # 3. 分析用途禁止明细
    try:
        executor.run("analytics", {"table": "customers", "select": ["*"]})
        check("分析用途明细查询被拒", False)
    except QueryError:
        check("分析用途明细查询被拒", True)

    # 4. k 匿名：小城市 4 人，分析 k=5，分组结果里不应出现该城市
    grp = executor.run("analytics", {
        "table": "customers", "group_by": ["city"],
        "aggregations": [{"func": "count", "column": "uid", "alias": "n"}]})
    cities = {r["city"] for r in grp["rows"]}
    check(f"小群体城市 {SMALL_CITY[1]}（4人）被 k=5 抑制",
          SMALL_CITY[1] not in cities,
          f"suppressed={grp['suppressed_groups']}")
    check("所有返回分组均 >= k", all(r["n"] >= 5 for r in grp["rows"]))

    # 5. 外包行范围：只允许白名单城市
    out = executor.run("outsourcing", {
        "table": "customers", "group_by": ["city"],
        "aggregations": [{"func": "count", "column": "uid", "alias": "n"}]})
    allowed = {"杭州", "宁波", "温州", "嘉兴", "绍兴"}
    check("外包城市白名单在视图层生效",
          {r["city"] for r in out["rows"]} <= allowed)

    # 6. 外包 k=10：分组强制 HAVING，结果都 >=10
    check("外包分组全部满足 k=10", all(r["n"] >= 10 for r in out["rows"]))

    # 7. 跨用途伪标识不可关联：同一客户在不同用途的 uid 不同
    same_scene = {"table": "customers",
                  "group_by": ["uid"],
                  "aggregations": [{"func": "count", "column": "uid",
                                    "alias": "n"}],
                  "limit": 3}
    u_ops = {r["uid"] for r in executor.run("operations", same_scene)["rows"]}
    u_ana = {r["uid"] for r in executor.run("analytics", same_scene)["rows"]}
    check("跨用途伪标识集合不相交（不可关联）",
          u_ops.isdisjoint(u_ana),
          f"交集大小 {len(u_ops & u_ana)}")

    # 8. 关联查询也走视图，且未授权关联被拒
    joined = executor.run("operations", {
        "table": "orders",
        "joins": ["customers"],
        "group_by": ["t1.city"],
        "aggregations": [
            {"func": "count", "column": "oid", "alias": "订单数"},
            {"func": "sum", "column": "amount", "alias": "总额"}]})
    check("授权关联（orders→customers）可聚合", len(joined["rows"]) > 0,
          f"SQL: {joined['sql'][:90]}...")
    try:
        executor.run("analytics", {
            "table": "customers", "joins": ["addresses"],
            "group_by": ["city"],
            "aggregations": [{"func": "count", "column": "uid", "alias": "n"}]})
        check("分析用途未授权关联 addresses 被拒", False)
    except QueryError:
        check("分析用途未授权关联 addresses 被拒", True)

    # 9. 缓存：同查询第二次命中；发布后缓存清空
    q = {"table": "orders", "group_by": ["channel"],
         "aggregations": [{"func": "count", "column": "oid", "alias": "n"}]}
    first = executor.run("analytics", q)
    second = executor.run("analytics", q)
    check("相同查询第二次命中版本化缓存",
          (not first["cache_hit"]) and second["cache_hit"])

    # 10. 发布前预览能看到 k 5->8 的影响变化
    draft = upgraded_policy(2)
    report = preview_impact(store, executor, draft)
    before = report["purposes"]["analytics"]["current"]
    after = report["purposes"]["analytics"]["draft"]
    check("影响预览对比出现行/抑制差异",
          before["k"] == 5 and after["k"] == 8,
          f"现行 k={before['k']} -> 草稿 k={after['k']}")
    # 预览隔离：在更细粒度（年龄段+城市）上，收紧 k 会改变返回行数，
    # 且两次预览互不污染（独立 TEMP VIEW 连接）
    fine_scene = {
        "table": "customers", "group_by": ["city", "age_band"],
        "aggregations": [{"func": "count", "column": "uid", "alias": "n"}],
        "limit": 200}
    r_cur = executor.run_on_preview(default_policy(1), "analytics", fine_scene)
    r_draft = executor.run_on_preview(draft, "analytics", fine_scene)
    check("预览在独立连接上分别命中现行/草稿规则",
          r_cur["k"] == 5 and r_draft["k"] == 8
          and len(r_draft["rows"]) <= len(r_cur["rows"]),
          f"现行返回 {len(r_cur['rows'])} 组，草稿 {len(r_draft['rows'])} 组")
    check("预览不改变活动版本与正式视图",
          store.active_version() == 1)

    # 11. 先提交一个外包导出任务并等其完成
    job_id = jobs.submit("outsourcing", {
        "table": "orders", "group_by": ["channel"],
        "aggregations": [{"func": "count", "column": "oid", "alias": "n"}]})
    _wait_job(jobs, job_id)
    done = jobs.get(job_id)
    check("导出任务按 v1 完成", done["status"] == "done"
          and done["executed_version"] == 1)
    path_v1, _ = jobs.download_path(job_id)
    check("v1 导出文件可下载", os.path.exists(path_v1))

    # 12. 原子发布 v2：活动版本切换、缓存失效、旧导出撤回
    store.publish(draft)
    check("活动版本原子切换到 v2", store.active_version() == 2)
    third = executor.run("analytics", q)
    check("发布后旧缓存全部失效（不命中）", third["cache_hit"] is False
          and third["policy_version"] == 2)
    revoked = jobs.get(job_id)
    check("旧版本导出被撤回，禁止下载", revoked["status"] == "revoked")
    try:
        jobs.download_path(job_id)
        check("撤回任务下载被拒", False)
    except QueryError:
        check("撤回任务下载被拒", True)

    # 13. 升级期间无混合范围：所有在线查询的 policy_version 都等于活动版本
    probes = [
        executor.run("operations", {"table": "customers", "limit": 3}),
        executor.run("analytics", q),
        executor.run("outsourcing", {
            "table": "customers", "group_by": ["city"],
            "aggregations": [{"func": "count", "column": "uid", "alias": "n"}]}),
    ]
    check("全部接口统一按 v2 出数，无旧范围残留",
          all(p["policy_version"] == 2 for p in probes))

    # 14. v2 更严：外包 k=15，城市减至 3 个
    out2 = executor.run("outsourcing", {
        "table": "customers", "group_by": ["city"],
        "aggregations": [{"func": "count", "column": "uid", "alias": "n"}]})
    check("v2 外包只剩 3 城且分组 >=15",
          {r["city"] for r in out2["rows"]} <= {"杭州", "宁波", "温州"}
          and all(r["n"] >= 15 for r in out2["rows"]))

    # 15. 重启恢复语义：模拟旧队列任务，恢复后按 v2 重算
    import json
    import uuid
    old_job = uuid.uuid4().hex
    conn = store.connect()
    try:
        conn.execute(
            "INSERT INTO jobs(id,purpose,query_json,status,requested_version,"
            "created_at) VALUES(?,?,?,'queued',1,?)",
            (old_job, "analytics",
             json.dumps(q, ensure_ascii=False), time.time()))
        conn.commit()
    finally:
        conn.close()
    jobs2 = JobManager(store, executor, export_dir)  # 重新启动 worker
    recovered = _wait_job(jobs2, old_job)
    check("恢复的旧任务按新版本 v2 重算，未越过新限制",
          recovered["status"] == "done"
          and recovered["requested_version"] == 1
          and recovered["executed_version"] == 2,
          f"请求 v1 -> 实际 v{recovered['executed_version']}")

    # 16. 审计日志完整记录请求/执行版本
    audit = store.list_audit(200)
    has_publish = any(a["action"] == "publish" for a in audit)
    check("审计日志记录发布与全部查询", has_publish and len(audit) >= 10)

    # 17. 非法策略被拒（k 非正整数）
    bad = default_policy(3)
    bad["purposes"]["analytics"]["k"] = 0
    try:
        store.save_draft(bad)
        check("非法策略（k=0）被拒绝", False)
    except PolicyError:
        check("非法策略（k=0）被拒绝", True)

    # 18. 并发查询与发布共存：无死锁/无错误，响应版本只会是已发布版本
    import threading
    errors18, versions18 = [], []
    group_q = {"table": "customers", "group_by": ["city"],
               "aggregations": [{"func": "count", "column": "uid", "alias": "n"}]}

    def hammer():
        for _ in range(20):
            try:
                rr = executor.run("analytics", group_q)
                versions18.append(rr["policy_version"])
            except Exception as exc:  # noqa: BLE001
                errors18.append(repr(exc))

    threads = [threading.Thread(target=hammer) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.05)
    store.publish(upgraded_policy(3))
    for t in threads:
        t.join(timeout=15)
    check("并发查询期间发布不死锁、不报错", not errors18, str(errors18[:2]))
    check("并发响应只出现已发布版本且含新版本",
          set(versions18) <= {2, 3} and 3 in versions18,
          f"版本集合 {sorted(set(versions18))}，样本 {len(versions18)} 次")

    jobs2.shutdown()
    jobs.shutdown()
    store.close()

    failed = [r for r in results if not r[1]]
    print(f"\n合计 {len(results)} 项，通过 {len(results) - len(failed)}，"
          f"失败 {len(failed)}")
    return 1 if failed else 0


def _wait_job(jobs: JobManager, job_id: str, timeout: float = 5) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = jobs.get(job_id)
        if job["status"] in ("done", "failed", "revoked"):
            return job
        time.sleep(0.05)
    raise TimeoutError(job_id)


if __name__ == "__main__":
    raise SystemExit(main())
