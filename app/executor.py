"""查询执行器：所有用途数据访问的唯一执行边界。

前端/API 只能提交结构化逻辑查询，不能提交原始 SQL。执行器负责：
1. 解析活动策略版本，定位版本化用途视图（绝不接触原始表）；
2. 列级白名单校验：过滤/分组/排序/聚合/关联键必须是该用途暴露列；
3. 编译受限 SELECT（标识符合法性正则 + 值绑定参数，杜绝注入）；
4. 小群体保护：分组查询强制 HAVING COUNT(*) >= k；明细查询要求 allow_detail；
5. 缓存以策略版本为键，规则发布即整体失效；
6. 每次访问写审计日志。
"""
from __future__ import annotations

import re
import sqlite3

from .schema import AGG_FUNCS, FILTER_OPS
from .policy import PolicyError, exposed_columns, join_path, view_name

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
# 输出别名允许中文等 Unicode 字符，但禁止引号/分号等可破坏 SQL 结构的字符
ALIAS_RE = re.compile(r"^[\w一-鿿]{1,63}$", re.UNICODE)
MAX_DETAIL_ROWS = 1000
MAX_GROUP_ROWS = 5000


class QueryError(ValueError):
    """用户查询违反策略或结构非法（HTTP 400 语义）。"""


def _check_ident(name: str) -> str:
    if not isinstance(name, str) or not IDENT_RE.match(name):
        raise QueryError(f"非法标识符: {name!r}")
    return name


def _check_alias(name: str) -> str:
    if not isinstance(name, str) or not ALIAS_RE.match(name):
        raise QueryError(f"非法输出别名: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def _resolve_col(col: str, alias: str, exposed: dict,
                 join_exposed: dict | None = None) -> str:
    """把逻辑查询里的列名解析成带表别名的安全 SQL 引用。

    支持两种写法：
    * 裸列名 city：必须在主表暴露列中；
    * 限定名 t1.city：在 join 表暴露列（键形如 't1.city'）中查找。
    """
    if not isinstance(col, str):
        raise QueryError(f"非法列名: {col!r}")
    if "." in col:
        t_alias, bare = col.split(".", 1)
        _check_ident(t_alias)
        _check_ident(bare)
        if join_exposed is None or col not in join_exposed:
            raise QueryError(f"列 {col!r} 未在当前用途的关联视图中暴露")
        return col
    _check_ident(col)
    if col not in exposed:
        raise QueryError(f"列 {col!r} 未在当前用途视图中暴露")
    return f"{alias}.{col}"


def _qualified(table_alias: str, col: str, exposed: dict[str, dict]) -> str:
    _check_ident(col)
    if col not in exposed:
        raise QueryError(f"列 {col!r} 未在当前用途视图中暴露")
    return f"{table_alias}.{col}"


def _compile_filter(filters: list, alias: str, exposed: dict) -> tuple[str, list]:
    if not filters:
        return "", []
    clauses, params = [], []
    for flt in filters:
        col, op, value = flt.get("column"), flt.get("op"), flt.get("value")
        if op not in FILTER_OPS:
            raise QueryError(f"不允许的过滤运算符: {op!r}")
        expr = _qualified(alias, col, exposed)
        if op == "in":
            if not isinstance(value, list) or not value:
                raise QueryError("in 过滤需要非空数组")
            clauses.append(f"{expr} IN ({','.join('?' for _ in value)})")
            params.extend(value)
        elif op == "between":
            if not (isinstance(value, list) and len(value) == 2):
                raise QueryError("between 过滤需要长度 2 的数组")
            clauses.append(f"{expr} BETWEEN ? AND ?")
            params.extend(value)
        else:
            if isinstance(value, (list, dict)):
                raise QueryError(f"{op} 过滤需要标量值")
            clauses.append(f"{expr} {op} ?")
            params.append(value)
    return " WHERE " + " AND ".join(clauses), params


def _compile_agg(spec: dict, alias: str, exposed: dict,
                 join_exposed: dict | None = None) -> str:
    func = spec.get("func", "").lower()
    if func not in AGG_FUNCS:
        raise QueryError(f"不允许的聚合函数: {func!r}")
    alias_name = _check_alias(spec.get("alias", f"{func}_x"))
    if func == "count" and spec.get("column") in (None, "*"):
        return f"COUNT(*) AS {alias_name}"
    col = _resolve_col(spec["column"], alias, exposed, join_exposed)
    return f"{func.upper()}({col}) AS {alias_name}"


class Executor:
    def __init__(self, store):
        self.store = store

    def run(self, purpose: str, query: dict) -> dict:
        """按活动策略执行查询，返回 rows + 规则元信息。"""
        version, doc = self.store.active_policy()
        pconf = doc["purposes"].get(purpose)
        if pconf is None:
            raise QueryError(f"未知用途: {purpose}")

        cached = self.store.cache_get(version, purpose, query)
        cache_hit = cached is not None
        if cached is not None:
            self.store.audit(purpose, "query", requested_version=version,
                             executed_version=version, cache_hit=True,
                             row_count=len(cached["rows"]), query=query)
            return {**cached, "cache_hit": True}

        # 独立只读连接：与其他线程的查询/发布不共享连接，避免互锁
        conn = self.store.connect()
        try:
            result = self._compile_and_run(
                conn, doc, version, purpose, pconf, query)
        finally:
            conn.close()
        self.store.cache_put(version, purpose, query, result)
        self.store.audit(purpose, "query", requested_version=version,
                         executed_version=version, cache_hit=False,
                         row_count=len(result["rows"]), query=query)
        return {**result, "cache_hit": False}

    def run_on_preview(self, doc: dict, purpose: str, query: dict) -> dict:
        """在草稿预览连接上执行（TEMP VIEW），用于发布前影响评估。"""
        pconf = doc["purposes"].get(purpose)
        if pconf is None:
            raise QueryError(f"草稿中不存在用途: {purpose}")
        preview_version = self.store.preview_version()
        session = self.store.preview_connection(doc)
        try:
            result = self._compile_and_run(
                session.conn, doc, preview_version, purpose, pconf, query)
            # 预览结果标注真实草稿版本，避免前端误读
            result["policy_version"] = doc["version"]
            result["preview"] = True
            return result
        finally:
            session.close()

    # -------------------------------------------------------------- compilation
    def _compile_and_run(self, conn, doc: dict, version: int, purpose: str,
                         pconf: dict, query: dict) -> dict:
        table = _check_ident(query.get("table", ""))
        if table not in pconf["tables"]:
            raise QueryError(f"用途 {purpose} 未开放表 {table}")
        tconf = pconf["tables"][table]
        exposed = exposed_columns(pconf, table)
        v = view_name(version, purpose, table)
        alias = "t0"
        params: list = []

        joins_sql, join_exposed = self._compile_joins(
            doc, version, purpose, pconf, table, query, alias)
        all_exposed = {**exposed, **join_exposed}

        where_sql, wparams = _compile_filter(
            query.get("filters", []), alias, exposed)
        params.extend(wparams)

        group_by = query.get("group_by", [])
        aggregations = query.get("aggregations", [])
        is_grouped = bool(group_by or aggregations)

        if is_grouped:
            sql, head, extra = self._compile_grouped(
                query, alias, exposed, join_exposed, all_exposed,
                pconf, params)
        else:
            sql, head = self._compile_detail(
                query, alias, exposed, pconf, params)
            extra = {}

        full_sql = (
            f"SELECT {head} FROM {v} AS {alias}{joins_sql}{where_sql}{sql}"
        )
        try:
            rows = [dict(r) for r in conn.execute(full_sql, params).fetchall()]
        except sqlite3.Error as exc:  # 视图/编译问题归为执行错误，不泄露原始结构
            raise QueryError(f"查询执行失败: {exc}") from exc

        result = {
            "rows": rows,
            "policy_version": version,
            "purpose": purpose,
            "k": pconf["k"],
            "allow_detail": pconf["allow_detail"],
            "sql": full_sql,
            **extra,
        }
        if is_grouped:
            result["suppressed_groups"] = self._count_suppressed(
                conn, v, alias, joins_sql, where_sql, params,
                group_by, pconf["k"], query, join_exposed, exposed)
        return result

    def _compile_joins(self, doc, version, purpose, pconf, table, query, alias):
        joins_sql, join_exposed = "", {}
        used = {table}
        aliases = {table: alias}
        for jt in query.get("joins", []):
            jt = _check_ident(jt)
            if jt in used:
                raise QueryError(f"重复关联表 {jt}")
            if jt not in pconf["tables"]:
                raise QueryError(f"用途 {purpose} 未开放表 {jt}")
            left_table, lname, rname = self._resolve_join(
                purpose, used, jt, pconf)
            j_alias = f"t{len(used)}"
            j_view = view_name(version, purpose, jt)
            joins_sql += (
                f" JOIN {j_view} AS {j_alias} ON "
                f"{aliases[left_table]}.{lname} = {j_alias}.{rname}")
            join_exposed.update({
                f"{j_alias}.{c['name']}": c
                for c in pconf["tables"][jt]["columns"]
            })
            used.add(jt)
            aliases[jt] = j_alias
        return joins_sql, join_exposed

    @staticmethod
    def _resolve_join(purpose, used: set, target: str, pconf: dict):
        """在已使用的表里找一个与 target 有白名单路径的表，返回(左表, 左暴露列, 右暴露列)。"""
        from .policy import _raw_join_exists  # 复用白名单
        last_err = None
        for t in used:
            if _raw_join_exists(t, target):
                try:
                    lcol, rcol = join_path(purpose, t, target, pconf)
                    # join_path 返回 '表.列'，这里只取暴露列名
                    return t, lcol.split(".", 1)[1], rcol.split(".", 1)[1]
                except PolicyError as exc:
                    last_err = exc
        raise QueryError(
            f"无法关联 {target}：当前用途未暴露所需外键"
            + (f"（{last_err}）" if last_err else ""))

    def _compile_detail(self, query, alias, exposed, pconf, params):
        if not pconf["allow_detail"]:
            raise QueryError(
                f"用途不允许明细查询（k={pconf['k']}），请改用分组聚合")
        select_cols = query.get("select", ["*"])
        if select_cols == ["*"] or not select_cols:
            head = ", ".join(f"{alias}.{c}" for c in exposed)
        else:
            head = ", ".join(_qualified(alias, c, exposed) for c in select_cols)
        order_sql = self._compile_order(query, alias, exposed)
        limit = min(int(query.get("limit", MAX_DETAIL_ROWS)), MAX_DETAIL_ROWS)
        params.append(limit)
        return f"{order_sql} LIMIT ?", head

    def _compile_grouped(self, query, alias, exposed, join_exposed,
                         all_exposed, pconf, params):
        raw_gb = query.get("group_by", [])
        gb_cols = [_resolve_col(c, alias, exposed, join_exposed)
                   for c in raw_gb]
        gb_expr = ", ".join(gb_cols)

        agg_heads = [
            _compile_agg(spec, alias, exposed, join_exposed)
            for spec in query.get("aggregations", [])
        ]
        if not agg_heads:
            # 只分组不聚合时自动补 COUNT，便于 k 匿名判断
            agg_heads = ["COUNT(*) AS group_size"]
        head = ", ".join([gb_expr] + agg_heads) if gb_expr else ", ".join(agg_heads)

        order_sql = self._compile_order(query, alias, exposed, join_exposed)
        k = pconf["k"]
        having = f" HAVING COUNT(*) >= {int(k)}"
        limit = min(int(query.get("limit", MAX_GROUP_ROWS)), MAX_GROUP_ROWS)
        params.append(limit)
        sql = (f" GROUP BY {gb_expr}{having}{order_sql} LIMIT ?"
               if gb_expr else f"{having}{order_sql} LIMIT ?")
        return sql, head, {"mode": "grouped", "group_cols": raw_gb}

    @staticmethod
    def _compile_order(query, alias, exposed, join_exposed=None):
        order = query.get("order_by")
        if not order:
            return ""
        if isinstance(order, dict):
            order = [order]
        parts = []
        for item in order:
            ref = _resolve_col(item["column"], alias, exposed, join_exposed)
            direction = item.get("direction", "asc").lower()
            if direction not in ("asc", "desc"):
                raise QueryError("direction 仅支持 asc/desc")
            parts.append(f"{ref} {direction.upper()}")
        return " ORDER BY " + ", ".join(parts)

    def _count_suppressed(self, conn, view, alias, joins_sql, where_sql,
                          params, group_by, k, query, join_exposed, exposed):
        """统计因不足 k 人而被抑制的小组数量（同样只在视图上计算）。"""
        if not group_by:
            return 0
        gb = ", ".join(_resolve_col(c, alias, exposed, join_exposed)
                       for c in group_by)
        sql = (f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM {view} AS {alias}"
               f"{joins_sql}{where_sql} GROUP BY {gb} HAVING COUNT(*) < {int(k)})")
        try:
            return conn.execute(sql, [p for p in params[:-1]]).fetchone()["n"]
        except sqlite3.Error:
            return 0
