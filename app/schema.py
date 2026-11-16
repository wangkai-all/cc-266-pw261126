"""底层明细表的物理结构（虚构业务：电商客户/订单）。

策略层与执行层都只通过这里声明的元数据认识原始表，
避免编译器里出现硬编码表名/列名。
"""

# 原始明细表：表 -> 物理列顺序（首列为代理主键）
BASE_TABLES = {
    "customers": [
        "customer_id", "name", "phone", "email", "id_card",
        "gender", "age", "city", "register_date",
    ],
    "addresses": [
        "address_id", "customer_id", "province", "city", "detail", "is_default",
    ],
    "orders": [
        "order_id", "customer_id", "order_date", "status", "amount", "channel",
    ],
    "order_items": [
        "item_id", "order_id", "product_id", "quantity", "unit_price",
    ],
    "products": [
        "product_id", "category", "product_name", "brand",
    ],
}

PRIMARY_KEY = {
    "customers": "customer_id",
    "addresses": "address_id",
    "orders": "order_id",
    "order_items": "item_id",
    "products": "product_id",
}

# 原始表之间允许的外键关联（左表.左列 = 右表.右列）
RAW_JOIN_PATHS = [
    ("addresses", "customer_id", "customers", "customer_id"),
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
]

# 策略行过滤允许的运算符白名单
FILTER_OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "like", "between"}

# 聚合函数白名单
AGG_FUNCS = {"count", "sum", "avg", "min", "max"}
