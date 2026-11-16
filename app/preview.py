"""草稿策略的发布前影响评估。

用固定的一组代表性场景分别在「现行版本视图」与「草稿 TEMP VIEW」上执行，
对比行数/被抑制小组数，让规则调整的影响在发布前可见；不修改任何正式数据。
"""
from __future__ import annotations

from .executor import Executor, QueryError

# 每个用途一个固定场景：同一场景跨用途/跨版本可直接对比
PREVIEW_SCENARIOS = {
    "operations": {
        "table": "customers",
        "select": ["uid", "name_masked", "city", "age_band"],
        "limit": 5,
    },
    "analytics": {
        "table": "customers",
        "group_by": ["city"],
        "aggregations": [
            {"func": "count", "column": "uid", "alias": "客户数"},
        ],
        "limit": 50,
    },
    "outsourcing": {
        "table": "orders",
        "group_by": ["channel"],
        "aggregations": [
            {"func": "count", "column": "oid", "alias": "订单量"},
            {"func": "avg", "column": "amount", "alias": "客单价"},
        ],
        "limit": 50,
    },
}


def preview_impact(store, executor: Executor, draft: dict) -> dict:
    current_version = store.active_version()
    current_doc = (store.get_policy_doc(current_version)
                   if current_version else None)
    report = {"draft_version": draft["version"],
              "current_version": current_version, "purposes": {}}

    for purpose, scenario in PREVIEW_SCENARIOS.items():
        entry: dict = {"scenario": scenario}
        # 草稿结果
        try:
            draft_res = executor.run_on_preview(draft, purpose, scenario)
            entry["draft"] = _summarize(draft_res)
        except QueryError as exc:
            entry["draft"] = {"error": str(exc)}
        # 现行结果（可能尚无已发布版本）
        if current_doc and purpose in current_doc["purposes"]:
            try:
                cur_res = executor.run_on_preview(current_doc, purpose, scenario)
                entry["current"] = _summarize(cur_res)
            except QueryError as exc:
                entry["current"] = {"error": str(exc)}
        else:
            entry["current"] = {"error": "尚无已发布版本"}
        report["purposes"][purpose] = entry
    return report


def _summarize(result: dict) -> dict:
    return {
        "returned_rows": len(result["rows"]),
        "suppressed_groups": result.get("suppressed_groups", 0),
        "k": result["k"],
        "allow_detail": result["allow_detail"],
        "sample": result["rows"][:3],
    }
