"""脱敏策略模型：规则校验、版本化用途视图 DDL 生成。

策略 JSON 结构（一个版本包含多个用途，每个用途是一组表级规则）：

{
  "version": 1,
  "note": "备注",
  "salt": "随机串，参与伪标识派生",
  "purposes": {
    "<purpose>": {
      "k": 5,                       # k-匿名最小群体
      "allow_detail": false,        # 是否允许明细行查询
      "tables": {
        "<base_table>": {
          "row_filter": [{"column": "city", "op": "in", "value": [...]}],
          "columns": [
            {"name": "uid", "source": "customer_id", "transform": "pseudo"},
            {"name": "city", "source": "city", "transform": "none"}
          ],
          "joins": ["orders"]       # 该用途下允许关联到的其他表
        }
      }
    }
  }
}
"""
from __future__ import annotations

import re
import secrets
from typing import Any

from .schema import BASE_TABLES, FILTER_OPS, RAW_JOIN_PATHS

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
COLUMN_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")

# 已知脱敏/泛化变换 -> SQL 表达式模板（{c} 为原始列占位）
# pseudo 通过 SQLite 自定义函数 pse(值, 密钥) 实现，密钥按用途+版本绑定
TRANSFORMS = {
    "none": "{c}",
    # DDL 不支持绑定参数，密钥以安全字面量内嵌（salt 限定为 hex，见 store 校验）
    "pseudo": "pse(CAST({c} AS TEXT), {k})",
    "mask_name": """CASE WHEN {c} IS NULL THEN NULL
        ELSE substr({c},1,1) || '**' END""",
    "mask_phone": """CASE WHEN {c} IS NULL THEN NULL
        ELSE substr({c},1,3) || '****' || substr({c},-4) END""",
    "mask_email": """CASE WHEN {c} IS NULL THEN NULL
        ELSE substr({c},1,2) || '***' || substr({c}, instr({c}, '@')) END""",
    "mask_id_card": """CASE WHEN {c} IS NULL THEN NULL
        ELSE substr({c},1,6) || '********' || substr({c},-4) END""",
    "mask_detail": """CASE WHEN {c} IS NULL THEN NULL
        ELSE substr({c},1,6) || '***（详情已截断）' END""",
    "age_bucket": "(({c}/10)*10) || '-' || (({c}/10)*10+9)",
    "date_only": "date({c})",
    "month_only": "strftime('%Y-%m', {c})",
}


class PolicyError(ValueError):
    """策略本身不合法（与运行期查询错误区分）。"""


def sql_literal(value: Any) -> str:
    """把策略/查询里的标量值编译成 SQL 字面量（标识符一律不走这里）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise PolicyError(f"不支持的过滤值类型: {type(value).__name__}")


def new_salt() -> str:
    return secrets.token_hex(16)


def view_name(version: int, purpose: str, table: str) -> str:
    return f"v{version}_{purpose}_{table}"


def validate_policy(doc: dict) -> None:
    """全量校验一份策略文档，不合法直接抛 PolicyError。"""
    if not isinstance(doc, dict):
        raise PolicyError("策略必须是 JSON 对象")
    if not isinstance(doc.get("version"), int) or doc["version"] < 1:
        raise PolicyError("version 必须是 >=1 的整数")
    if not isinstance(doc.get("salt"), str) or len(doc["salt"]) < 8:
        raise PolicyError("salt 必须是长度 >=8 的字符串")
    purposes = doc.get("purposes")
    if not isinstance(purposes, dict) or not purposes:
        raise PolicyError("purposes 非空对象")

    for purpose, pconf in purposes.items():
        if not SLUG_RE.match(purpose or ""):
            raise PolicyError(f"用途标识非法: {purpose!r}")
        if not isinstance(pconf, dict):
            raise PolicyError(f"{purpose}: 配置必须是对象")
        if not isinstance(pconf.get("k"), int) or pconf["k"] < 1:
            raise PolicyError(f"{purpose}: k 必须是 >=1 的整数")
        if not isinstance(pconf.get("allow_detail"), bool):
            raise PolicyError(f"{purpose}: allow_detail 必须是布尔值")
        tables = pconf.get("tables")
        if not isinstance(tables, dict) or not tables:
            raise PolicyError(f"{purpose}: tables 非空对象")

        for table, tconf in tables.items():
            if table not in BASE_TABLES:
                raise PolicyError(f"{purpose}.{table}: 原始表不存在")
            if not isinstance(tconf, dict):
                raise PolicyError(f"{purpose}.{table}: 配置必须是对象")
            cols = tconf.get("columns")
            if not isinstance(cols, list) or not cols:
                raise PolicyError(f"{purpose}.{table}: columns 非空数组")

            seen = set()
            for col in cols:
                name, source, transform = (
                    col.get("name"), col.get("source"), col.get("transform", "none"),
                )
                if not name or not COLUMN_RE.match(name):
                    raise PolicyError(
                        f"{purpose}.{table}: 暴露列名非法 {name!r}")
                if name in seen:
                    raise PolicyError(
                        f"{purpose}.{table}: 暴露列名重复 {name}")
                seen.add(name)
                if source not in BASE_TABLES[table]:
                    raise PolicyError(
                        f"{purpose}.{table}.{name}: 源列 {source!r} 不存在")
                if transform not in TRANSFORMS:
                    raise PolicyError(
                        f"{purpose}.{table}.{name}: 未知变换 {transform!r}")

            for flt in tconf.get("row_filter", []):
                if flt.get("column") not in BASE_TABLES[table]:
                    raise PolicyError(
                        f"{purpose}.{table}: 行过滤列 {flt.get('column')!r} 不存在")
                if flt.get("op") not in FILTER_OPS:
                    raise PolicyError(
                        f"{purpose}.{table}: 过滤运算符 {flt.get('op')!r} 不允许")
                _validate_filter_value(flt)

            joins = tconf.get("joins", [])
            if not isinstance(joins, list):
                raise PolicyError(f"{purpose}.{table}: joins 必须是数组")
            for jt in joins:
                if jt not in tables:
                    raise PolicyError(
                        f"{purpose}.{table}: 关联目标 {jt} 未在该用途中声明")
                if not _raw_join_exists(table, jt):
                    raise PolicyError(
                        f"{purpose}.{table}: 与 {jt} 之间没有白名单外键路径")


def _validate_filter_value(flt: dict) -> None:
    op, value = flt["op"], flt.get("value")
    if op == "in":
        if not isinstance(value, list) or not value:
            raise PolicyError("in 过滤需要非空数组 value")
    elif op == "between":
        if not (isinstance(value, list) and len(value) == 2):
            raise PolicyError("between 过滤需要长度为 2 的 value")
    elif value is None or isinstance(value, (list, dict)):
        raise PolicyError(f"{op} 过滤需要标量 value")


def _raw_join_exists(t1: str, t2: str) -> bool:
    for a, _, b, _ in RAW_JOIN_PATHS:
        if {a, b} == {t1, t2}:
            return True
    return False


def build_view_ddl(version: int, purpose: str, table: str, tconf: dict,
                   salt: str) -> tuple[str, list]:
    """生成某用途某表的版本化视图 DDL。

    视图只暴露策略声明的列，行过滤在视图定义内固化，
    任何查询（含关联/分组/导出）都无法绕过。
    CREATE VIEW 不接受绑定参数，密钥与过滤值编译为安全字面量
    （salt 限 hex，标量经 sql_literal 转义）。第二返回值保留空列表。
    """
    key = f"{purpose}|{version}|{salt}"
    select_exprs = []
    for col in tconf["columns"]:
        expr = TRANSFORMS[col.get("transform", "none")].format(
            c=col["source"], k=sql_literal(key))
        select_exprs.append(f"{expr} AS {col['name']}")

    where_sql = _compile_row_filter(tconf.get("row_filter", []))
    ddl = (
        f"CREATE VIEW {view_name(version, purpose, table)} AS "
        f"SELECT {', '.join(select_exprs)} FROM {table}{where_sql}"
    )
    return ddl, []


def _compile_row_filter(filters: list) -> str:
    if not filters:
        return ""
    clauses = []
    for flt in filters:
        col, op, value = flt["column"], flt["op"], flt["value"]
        if op == "in":
            placeholders = ", ".join(sql_literal(v) for v in value)
            clauses.append(f"{col} IN ({placeholders})")
        elif op == "between":
            clauses.append(
                f"{col} BETWEEN {sql_literal(value[0])} "
                f"AND {sql_literal(value[1])}")
        elif op == "like":
            clauses.append(f"{col} LIKE {sql_literal(value)}")
        else:
            clauses.append(f"{col} {op} {sql_literal(value)}")
    return " WHERE " + " AND ".join(clauses)


def exposed_columns(pconf: dict, table: str) -> dict:
    """暴露列名 -> 列配置（供编译器做列级白名单校验）。"""
    return {c["name"]: c for c in pconf["tables"][table]["columns"]}


def join_path(purpose: str, t1: str, t2: str, pconf: dict) -> tuple[str, str]:
    """返回两表在该用途视图上的关联列（暴露列名）。"""
    tables = pconf["tables"]
    if t2 not in tables[t1].get("joins", []):
        raise PolicyError(f"用途 {purpose} 未允许 {t1} 关联 {t2}")
    raw = next(
        (p for p in RAW_JOIN_PATHS if {p[0], p[2]} == {t1, t2}), None)
    if raw is None:  # 理论上 validate 已挡住
        raise PolicyError(f"{t1} 与 {t2} 无白名单外键路径")
    left, lcol, right, rcol = raw
    lname = _exposed_name(tables[left], lcol)
    rname = _exposed_name(tables[right], rcol)
    return (f"{t1}.{lname}", f"{t2}.{rname}")


def _exposed_name(tconf: dict, source: str) -> str:
    for col in tconf["columns"]:
        if col["source"] == source:
            return col["name"]
    raise PolicyError(f"关联键 {source} 未在用途视图中暴露，无法关联")


def derive_version(base: dict, new_version: int, note: str | None = None) -> dict:
    """基于现有策略派生新版本草稿（继承 salt，保证同用途伪标识可延续）。"""
    import copy
    doc = copy.deepcopy(base)
    doc["version"] = new_version
    if note is not None:
        doc["note"] = note
    return doc
