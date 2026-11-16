"""默认脱敏策略 v1 与升级演示策略 v2（纯虚构规则，可在页面继续编辑）。

三用途差异：
* operations 运营：内部运营处置，允许明细，但直接标识全部遮蔽/伪标识化，k=1；
* analytics 分析：只做群体统计，禁明细，k=5，只暴露泛化维度与伪标识；
* outsourcing 外包：最严格，禁明细，k=10，城市白名单 + 最小字段集，
  且伪标识密钥与其他用途不同（天然不可跨用途关联）。
"""
from __future__ import annotations

SALT_V1 = "a1" + "9f3c7e21b08d4e6ac5f10d22e8b7461d"  # hex，满足内嵌安全要求
SALT_V2 = "b2" + "7c04e91af36d48b291e5ca07f63bd821"


def default_policy(version: int = 1) -> dict:
    return {
        "version": version,
        "note": "v1 初始策略：运营可看遮蔽明细，分析 k=5，外包 k=10 且限城市",
        "salt": SALT_V1,
        "purposes": {
            "operations": _operations(),
            "analytics": _analytics(),
            "outsourcing": _outsourcing(),
        },
    }


def upgraded_policy(version: int = 2) -> dict:
    """规则收紧示例：分析 k 5->8；外包 k 10->15 且再砍城市、订单表降维。"""
    doc = default_policy(version)
    doc["salt"] = SALT_V2
    doc["note"] = "v2 收紧：分析 k=8；外包 k=15、城市减至 3 个、订单仅留月度维度"
    doc["purposes"]["analytics"]["k"] = 8
    out = doc["purposes"]["outsourcing"]
    out["k"] = 15
    for tconf in out["tables"].values():
        for flt in tconf.get("row_filter", []):
            if flt["column"] == "city":
                flt["value"] = ["杭州", "宁波", "温州"]
    out["tables"]["orders"]["columns"] = [
        {"name": "oid", "source": "order_id", "transform": "pseudo"},
        {"name": "uid", "source": "customer_id", "transform": "pseudo"},
        {"name": "order_month", "source": "order_date", "transform": "month_only"},
        {"name": "status", "source": "status", "transform": "none"},
        {"name": "amount", "source": "amount", "transform": "none"},
        {"name": "channel", "source": "channel", "transform": "none"},
    ]
    return doc


# ---------------------------------------------------------------- purposes
def _operations() -> dict:
    return {
        "k": 1,
        "allow_detail": True,
        "tables": {
            "customers": {
                "columns": [
                    {"name": "uid", "source": "customer_id", "transform": "pseudo"},
                    {"name": "name_masked", "source": "name", "transform": "mask_name"},
                    {"name": "phone_masked", "source": "phone", "transform": "mask_phone"},
                    {"name": "email_masked", "source": "email", "transform": "mask_email"},
                    {"name": "gender", "source": "gender", "transform": "none"},
                    {"name": "age_band", "source": "age", "transform": "age_bucket"},
                    {"name": "city", "source": "city", "transform": "none"},
                    {"name": "register_month", "source": "register_date",
                     "transform": "month_only"},
                ],
                "joins": ["orders", "addresses"],
            },
            "addresses": {
                "columns": [
                    {"name": "aid", "source": "address_id", "transform": "pseudo"},
                    {"name": "uid", "source": "customer_id", "transform": "pseudo"},
                    {"name": "province", "source": "province", "transform": "none"},
                    {"name": "city", "source": "city", "transform": "none"},
                    {"name": "detail_masked", "source": "detail",
                     "transform": "mask_detail"},
                    {"name": "is_default", "source": "is_default", "transform": "none"},
                ],
                "joins": ["customers"],
            },
            "orders": {
                "columns": [
                    {"name": "oid", "source": "order_id", "transform": "pseudo"},
                    {"name": "uid", "source": "customer_id", "transform": "pseudo"},
                    {"name": "order_date", "source": "order_date",
                     "transform": "date_only"},
                    {"name": "status", "source": "status", "transform": "none"},
                    {"name": "amount", "source": "amount", "transform": "none"},
                    {"name": "channel", "source": "channel", "transform": "none"},
                ],
                "joins": ["customers", "order_items"],
            },
            "order_items": {
                "columns": [
                    {"name": "iid", "source": "item_id", "transform": "pseudo"},
                    {"name": "oid", "source": "order_id", "transform": "pseudo"},
                    {"name": "pid", "source": "product_id", "transform": "pseudo"},
                    {"name": "quantity", "source": "quantity", "transform": "none"},
                    {"name": "unit_price", "source": "unit_price", "transform": "none"},
                ],
                "joins": ["orders", "products"],
            },
            "products": {
                "columns": [
                    {"name": "pid", "source": "product_id", "transform": "pseudo"},
                    {"name": "category", "source": "category", "transform": "none"},
                    {"name": "brand", "source": "brand", "transform": "none"},
                ],
                "joins": ["order_items"],
            },
        },
    }


def _analytics() -> dict:
    return {
        "k": 5,
        "allow_detail": False,
        "tables": {
            "customers": {
                "columns": [
                    {"name": "uid", "source": "customer_id", "transform": "pseudo"},
                    {"name": "gender", "source": "gender", "transform": "none"},
                    {"name": "age_band", "source": "age", "transform": "age_bucket"},
                    {"name": "city", "source": "city", "transform": "none"},
                    {"name": "register_month", "source": "register_date",
                     "transform": "month_only"},
                ],
                "joins": ["orders"],
            },
            "orders": {
                "columns": [
                    {"name": "oid", "source": "order_id", "transform": "pseudo"},
                    {"name": "uid", "source": "customer_id", "transform": "pseudo"},
                    {"name": "order_month", "source": "order_date",
                     "transform": "month_only"},
                    {"name": "status", "source": "status", "transform": "none"},
                    {"name": "amount", "source": "amount", "transform": "none"},
                    {"name": "channel", "source": "channel", "transform": "none"},
                ],
                "joins": ["customers"],
            },
            "products": {
                "columns": [
                    {"name": "pid", "source": "product_id", "transform": "pseudo"},
                    {"name": "category", "source": "category", "transform": "none"},
                ],
                "joins": [],
            },
        },
    }


def _outsourcing() -> dict:
    city_filter = ["杭州", "宁波", "温州", "嘉兴", "绍兴"]
    return {
        "k": 10,
        "allow_detail": False,
        "tables": {
            "customers": {
                "row_filter": [
                    {"column": "city", "op": "in", "value": city_filter},
                ],
                "columns": [
                    {"name": "uid", "source": "customer_id", "transform": "pseudo"},
                    {"name": "gender", "source": "gender", "transform": "none"},
                    {"name": "age_band", "source": "age", "transform": "age_bucket"},
                    {"name": "city", "source": "city", "transform": "none"},
                ],
                "joins": ["orders"],
            },
            "orders": {
                "row_filter": [
                    {"column": "channel", "op": "in",
                     "value": ["APP", "小程序", "H5"]},
                ],
                "columns": [
                    {"name": "oid", "source": "order_id", "transform": "pseudo"},
                    {"name": "uid", "source": "customer_id", "transform": "pseudo"},
                    {"name": "order_date", "source": "order_date",
                     "transform": "date_only"},
                    {"name": "status", "source": "status", "transform": "none"},
                    {"name": "amount", "source": "amount", "transform": "none"},
                    {"name": "channel", "source": "channel", "transform": "none"},
                ],
                "joins": ["customers"],
            },
        },
    }
