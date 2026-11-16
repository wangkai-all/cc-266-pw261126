"""虚构演示数据生成（确定性随机，全部为虚构个人信息）。

刻意构造小城市「丽水」仅 4 名客户，用于验证分析(k=5)/外包(k=10) 的小群体抑制；
所有姓名、手机号、邮箱、身份证号均由代码生成，不对应真实个人。
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

from .schema import BASE_TABLES

SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜")
GIVEN = ["伟", "芳", "娜", "敏", "静", "强", "磊", "洋", "艳", "勇",
         "杰", "娟", "涛", "明", "超", "霞", "平", "刚", "桂英", "晓东"]
CITIES = {
    "浙江省": ["杭州", "宁波", "温州", "嘉兴", "绍兴", "金华"],
    "江苏省": ["南京", "苏州", "无锡"],
}
SMALL_CITY = ("浙江省", "丽水", 4)  # 小群体
CHANNELS = ["APP", "小程序", "H5", "线下门店", "电话客服"]
STATUSES = ["已完成", "已发货", "待支付", "已取消", "退款中"]
CATEGORIES = ["数码", "家居", "美妆", "食品", "户外"]
BRANDS = ["虚构牌", "示例科技", "演示优选", "假造工坊", "样板间"]


def generate(conn: sqlite3.Connection, n_customers: int = 180) -> dict:
    rnd = random.Random(20261116)
    counts: dict[str, int] = {}

    # ------------------------------------------------------------ products
    products = []
    for pid in range(1, 25):
        products.append((
            f"P{pid:04d}",
            rnd.choice(CATEGORIES),
            f"虚构商品{pid}号",
            rnd.choice(BRANDS),
        ))
    conn.executemany(
        f"INSERT INTO products ({','.join(BASE_TABLES['products'])}) "
        "VALUES (?,?,?,?)", products)
    counts["products"] = len(products)

    # ----------------------------------------------------------- customers
    customers, addresses = [], []
    city_pool = [(p, c) for p, cs in CITIES.items() for c in cs]
    for cid in range(1, n_customers + 1):
        province, city = rnd.choice(city_pool)
        name = rnd.choice(SURNAMES) + rnd.choice(GIVEN)
        phone = "1" + rnd.choice("35789") + "".join(rnd.choices("0123456789", k=9))
        email = f"user{cid:04d}@example.test"
        id_card = "33000019" + "".join(rnd.choices("0123456789", k=10))
        reg = date(2024, 1, 1) + timedelta(days=rnd.randint(0, 680))
        customers.append((
            f"C{cid:05d}", name, phone, email, id_card,
            rnd.choice(["男", "女"]), rnd.randint(18, 65),
            city, reg.isoformat(),
        ))
        # 一到两个地址
        for k in range(rnd.randint(1, 2)):
            addresses.append((
                f"A{len(addresses) + 1:06d}", f"C{cid:05d}", province, city,
                f"虚构路{rnd.randint(1, 99)}号{rnd.randint(1, 20)}栋"
                f"{rnd.randint(101, 2501)}室",
                1 if k == 0 else 0,
            ))
    # 小群体城市：固定 4 人
    for j in range(SMALL_CITY[2]):
        cid = n_customers + j + 1
        customers.append((
            f"C{cid:05d}",
            rnd.choice(SURNAMES) + rnd.choice(GIVEN),
            "1" + rnd.choice("35789") + "".join(rnd.choices("0123456789", k=9)),
            f"small{j}@example.test",
            "33000019" + "".join(rnd.choices("0123456789", k=10)),
            rnd.choice(["男", "女"]), rnd.randint(18, 65),
            SMALL_CITY[1],
            (date(2025, 6, 1) + timedelta(days=j)).isoformat(),
        ))
        addresses.append((
            f"A{len(addresses) + 1:06d}", f"C{cid:05d}",
            SMALL_CITY[0], SMALL_CITY[1],
            f"虚构路{rnd.randint(1, 9)}号{rnd.randint(101, 301)}室", 1,
        ))
    conn.executemany(
        f"INSERT INTO customers ({','.join(BASE_TABLES['customers'])}) "
        "VALUES (?,?,?,?,?,?,?,?,?)", customers)
    conn.executemany(
        f"INSERT INTO addresses ({','.join(BASE_TABLES['addresses'])}) "
        "VALUES (?,?,?,?,?,?)", addresses)
    counts["customers"] = len(customers)
    counts["addresses"] = len(addresses)

    # -------------------------------------------------------------- orders
    orders, items = [], []
    oid = 0
    for c in customers:
        cust_id = c[0]
        city = c[7]
        for _ in range(rnd.randint(1, 4)):
            oid += 1
            order_id = f"O{oid:06d}"
            od = date(2025, 1, 1) + timedelta(days=rnd.randint(0, 318))
            orders.append((
                order_id, cust_id, od.isoformat(),
                rnd.choice(STATUSES),
                round(rnd.uniform(25, 2800), 2),
                rnd.choice(CHANNELS),
            ))
            for _ in range(rnd.randint(1, 3)):
                pid = rnd.randint(1, 24)
                items.append((
                    f"I{len(items) + 1:07d}", order_id, f"P{pid:04d}",
                    rnd.randint(1, 5),
                    round(rnd.uniform(10, 1500), 2),
                ))
    conn.executemany(
        f"INSERT INTO orders ({','.join(BASE_TABLES['orders'])}) "
        "VALUES (?,?,?,?,?,?)", orders)
    conn.executemany(
        f"INSERT INTO order_items ({','.join(BASE_TABLES['order_items'])}) "
        "VALUES (?,?,?,?,?)", items)
    counts["orders"] = len(orders)
    counts["order_items"] = len(items)
    conn.commit()
    return counts
