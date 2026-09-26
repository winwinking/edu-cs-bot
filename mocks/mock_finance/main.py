"""mock-finance：假的财务系统（阶段二 2.9，附录 C 契约）。

数据按"当前日期"动态生成（订单永远挂在"上个月"），保证任何时候跑演示都有数据可查，不用每隔
一段时间手动改种子数据。校验两件事：X-Service-Token 对不对（不对 401）、X-Acting-User-Id 有没有
权限查 user_id 这个人的数据（没有 403）——这是"两层权限校验"里独立于 worker 的第二层，就算 worker
那层判断出错，这里还能兜底拦一次。
"""
import asyncio
import os
from datetime import date
from typing import Literal, Optional

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

app = FastAPI(title="mock-finance")

_config: dict = {
    "latency_ms": int(os.getenv("MOCK_FINANCE_LATENCY_MS", "50")),
    "mode": os.getenv("MOCK_FINANCE_MODE", "normal"),
}
_DEFAULT_CONFIG: dict = dict(_config)

_SERVICE_TOKEN = os.getenv("FINANCE_SERVICE_TOKEN", "finance-mock-service-token")

# mock-finance 是"假的外部系统"，不导入 app.common，自己维护一份跟 scripts/seed.py 对得上的
# 最小用户/家长关联表，只用来做权限校验——不是真数据库，只是让越权测试有据可依
_TENANT_USERS: dict[str, set[str]] = {
    "t_a": {"u_a_1001", "u_a_1002", "u_a_1003", "u_a_1004"},
    "t_b": {"u_b_1001", "u_b_1002", "u_b_1003"},
}
_GUARDIAN_LINKS: dict[tuple[str, str], str] = {
    ("t_a", "u_a_1002"): "u_a_1001",
    ("t_b", "u_b_1002"): "u_b_1001",
}

# 阶段四 4.1：每个账号从"一笔订单"改成"最近三个月各一笔订单"，覆盖不同发票状态，这样才能测出
# "查不同月份/不同状态，回复不一样"，而不是每次都是同一条数据。months_ago=1 是上个月，2/3 依次
# 往前推——跟 _last_month_ym() 用的偏移量算法一致，年份跨界（比如现在是 1 月，3 个月前是去年
# 10 月）由 _ym_offset() 统一处理，不用在数据里手写年份。
# 退费和余额不是按月份查的（get_refunds/get_balance 没有 period 参数），所以单独放两张表，
# 覆盖题目要求的"退费审核中/已退款"和"余额有值/为 0"。部分记录带手机号/身份证号，专门用来验证
# 脱敏覆盖到了这些字段。
_ORDERS: dict[tuple[str, str], list[dict]] = {
    ("t_a", "u_a_1001"): [
        {
            "months_ago": 1,
            "course_name": "春季数学班",
            "order_suffix": "12-8831",
            "amount": 2399.0,
            "invoice_status": "已开具",
            "invoice_day": 18,
            "invoice_email": "lin.xiaoyu@example.com",
        },
        {
            "months_ago": 2,
            "course_name": "口语提高班",
            "order_suffix": "11-7702",
            "amount": 1599.0,
            "invoice_status": "未开具",
            "invoice_day": None,
            "invoice_email": None,
        },
        {
            "months_ago": 3,
            "course_name": "数学思维训练营",
            "order_suffix": "10-6650",
            "amount": 1299.0,
            "invoice_status": "已开具",
            "invoice_day": 9,
            "invoice_email": "lin.xiaoyu@example.com",
        },
    ],
    ("t_a", "u_a_1004"): [
        {
            "months_ago": 1,
            "course_name": "暑期英语班",
            "order_suffix": "05-1122",
            "amount": 1899.0,
            "invoice_status": "未开具",
            "invoice_day": None,
            "invoice_email": None,
        },
        {
            "months_ago": 2,
            "course_name": "作文提升班",
            "order_suffix": "04-3391",
            "amount": 999.0,
            "invoice_status": "已开具",
            "invoice_day": 22,
            "invoice_email": "wang.xiaohua@example.com",
        },
        {
            "months_ago": 3,
            "course_name": "阅读理解班",
            "order_suffix": "03-2280",
            "amount": 899.0,
            "invoice_status": "未开具",
            "invoice_day": None,
            "invoice_email": None,
        },
    ],
    ("t_b", "u_b_1001"): [
        {
            "months_ago": 1,
            "course_name": "春季英语班",
            "order_suffix": "20-2233",
            "amount": 2599.0,
            "invoice_status": "已开具",
            "invoice_day": 20,
            "invoice_email": "li.xiaohong@example.com",
        },
        {
            "months_ago": 2,
            "course_name": "语法专项班",
            "order_suffix": "19-1188",
            "amount": 1399.0,
            "invoice_status": "已开具",
            "invoice_day": 15,
            "invoice_email": "li.xiaohong@example.com",
        },
        {
            "months_ago": 3,
            "course_name": "听说读写强化班",
            "order_suffix": "18-0925",
            "amount": 1799.0,
            "invoice_status": "未开具",
            "invoice_day": None,
            "invoice_email": None,
        },
    ],
}

# 退费：u_a_1001 审核中、u_b_1001 已完成、u_a_1004 无退费记录——覆盖"退费审核中"和"已退款"两种
# 题目点名的状态
_REFUNDS: dict[tuple[str, str], dict] = {
    ("t_a", "u_a_1001"): {
        "refund_status": "审核中",
        "refund_amount": 2399.0,
        "refund_bank_card": "6222021234567890",
        "contact_phone": "13812345678",
    },
    ("t_a", "u_a_1004"): {
        "refund_status": "无退费记录",
        "refund_amount": 0.0,
        "refund_bank_card": None,
        "contact_id_card": "310101199003077890",
    },
    ("t_b", "u_b_1001"): {
        "refund_status": "已完成",
        "refund_amount": 300.0,
        "refund_bank_card": "6217001234567890",
        "contact_phone": "13900002001",
    },
}

# 余额：u_a_1001/u_b_1001 有值，u_a_1004 为 0——覆盖"余额有值和为 0"两种题目点名的状态
_BALANCES: dict[tuple[str, str], float] = {
    ("t_a", "u_a_1001"): 120.0,
    ("t_a", "u_a_1004"): 0.0,
    ("t_b", "u_b_1001"): 300.0,
}


class AdminConfigUpdate(BaseModel):
    latency_ms: Optional[int] = Field(default=None, ge=0)
    mode: Optional[Literal["normal", "timeout", "error500"]] = None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/admin/config")
async def get_config() -> dict:
    return _config


@app.post("/admin/config")
async def update_config(update: AdminConfigUpdate) -> dict:
    if update.latency_ms is not None:
        _config["latency_ms"] = update.latency_ms
    if update.mode is not None:
        _config["mode"] = update.mode
    return _config


@app.post("/admin/reset")
async def reset_config() -> dict:
    _config.update(_DEFAULT_CONFIG)
    return _config


def _ym_offset(months_ago: int) -> tuple[int, int]:
    """算"往前推 N 个月"是哪年哪月，统一处理跨年（比如现在是 1 月，3 个月前是去年 10 月）。"""
    today = date.today()
    total = today.year * 12 + (today.month - 1) - months_ago
    return total // 12, total % 12 + 1


def _last_month_ym() -> tuple[int, int]:
    return _ym_offset(1)


def _resolve_ym(period: Optional[str]) -> tuple[int, int]:
    """period 不传或传 last_month 都按上个月算；this_month 按本月；YYYY-MM 按字面解析。"""
    today = date.today()
    if period in (None, "last_month"):
        return _last_month_ym()
    if period == "this_month":
        return today.year, today.month
    year_str, _, month_str = period.partition("-")
    return int(year_str), int(month_str)


async def _apply_mode_and_latency() -> None:
    if _config["mode"] == "timeout":
        # 故意睡得比 worker 财务客户端的超时（1.5 秒）长很多，确保触发的是客户端超时，不是这里先响应
        await asyncio.sleep(5)
        return
    if _config["mode"] == "error500":
        raise HTTPException(status_code=500, detail="mock-finance 模拟的上游错误（error500 模式）")
    await asyncio.sleep(_config["latency_ms"] / 1000)


def _check_auth(x_service_token: str) -> None:
    if x_service_token != _SERVICE_TOKEN:
        raise HTTPException(status_code=401, detail="服务令牌不对")


def _check_permission(tenant_id: str, acting_user_id: str, target_user_id: str) -> None:
    tenant_members = _TENANT_USERS.get(tenant_id)
    if not tenant_members or acting_user_id not in tenant_members or target_user_id not in tenant_members:
        # 租户不对，或者发起人/被查人干脆不在这个租户里——一律当越权处理，不区分"用户不存在"，
        # 免得越权探测的人能借着报错信息反推出别的租户有哪些用户 id
        raise HTTPException(status_code=403, detail="无权查询该账号的财务信息")
    if acting_user_id == target_user_id:
        return
    if _GUARDIAN_LINKS.get((tenant_id, acting_user_id)) == target_user_id:
        return
    raise HTTPException(status_code=403, detail="无权查询该账号的财务信息")


def _get_orders(tenant_id: str, target_user_id: str) -> list[dict]:
    orders = _ORDERS.get((tenant_id, target_user_id))
    if orders is None:
        raise HTTPException(status_code=404, detail="查不到这个账号的财务底稿")
    return orders


def _get_refund(tenant_id: str, target_user_id: str) -> dict:
    refund = _REFUNDS.get((tenant_id, target_user_id))
    if refund is None:
        raise HTTPException(status_code=404, detail="查不到这个账号的财务底稿")
    return refund


def _get_balance(tenant_id: str, target_user_id: str) -> float:
    if (tenant_id, target_user_id) not in _BALANCES:
        raise HTTPException(status_code=404, detail="查不到这个账号的财务底稿")
    return _BALANCES[(tenant_id, target_user_id)]


def _find_order_for_period(orders: list[dict], year: int, month: int) -> Optional[dict]:
    for order in orders:
        if _ym_offset(order["months_ago"]) == (year, month):
            return order
    return None


async def _guard(
    x_service_token: str, x_tenant_id: str, x_acting_user_id: str, user_id: str
) -> None:
    # 鉴权、授权都是本地判断，先做完；latency/故障模式模拟的是"真正去查数据"这一步才会遇到的延迟
    # 或失败，放在权限校验通过之后，跟越权场景（不该有延迟，应该立刻 403）区分开
    _check_auth(x_service_token)
    _check_permission(x_tenant_id, x_acting_user_id, user_id)
    await _apply_mode_and_latency()


@app.get("/orders")
async def get_orders(
    user_id: str = Query(...),
    period: Optional[str] = Query(default=None),
    x_service_token: str = Header(...),
    x_tenant_id: str = Header(...),
    x_acting_user_id: str = Header(...),
) -> dict:
    await _guard(x_service_token, x_tenant_id, x_acting_user_id, user_id)
    orders = _get_orders(x_tenant_id, user_id)
    year, month = _resolve_ym(period)
    order = _find_order_for_period(orders, year, month)
    if order is None:
        return {"orders": []}
    return {
        "orders": [
            {
                "order_no": f"EDU-{year}{month:02d}{order['order_suffix']}",
                "course_name": order["course_name"],
                "amount": order["amount"],
                "period": f"{year}-{month:02d}",
            }
        ]
    }


@app.get("/bills")
async def get_bills(
    user_id: str = Query(...),
    period: Optional[str] = Query(default=None),
    x_service_token: str = Header(...),
    x_tenant_id: str = Header(...),
    x_acting_user_id: str = Header(...),
) -> dict:
    await _guard(x_service_token, x_tenant_id, x_acting_user_id, user_id)
    orders = _get_orders(x_tenant_id, user_id)
    year, month = _resolve_ym(period)
    order = _find_order_for_period(orders, year, month)
    if order is None:
        return {"bills": []}
    return {
        "bills": [
            {
                "order_no": f"EDU-{year}{month:02d}{order['order_suffix']}",
                "course_name": order["course_name"],
                "amount": order["amount"],
                "period": f"{year}-{month:02d}",
                "status": "已支付",
            }
        ]
    }


@app.get("/invoices")
async def get_invoices(
    user_id: str = Query(...),
    period: Optional[str] = Query(default=None),
    x_service_token: str = Header(...),
    x_tenant_id: str = Header(...),
    x_acting_user_id: str = Header(...),
) -> dict:
    await _guard(x_service_token, x_tenant_id, x_acting_user_id, user_id)
    orders = _get_orders(x_tenant_id, user_id)
    year, month = _resolve_ym(period)
    order = _find_order_for_period(orders, year, month)
    if order is None:
        return {"invoices": []}
    return {
        "invoices": [
            {
                "order_no": f"EDU-{year}{month:02d}{order['order_suffix']}",
                "amount": order["amount"],
                "status": order["invoice_status"],
                "sent_at": (
                    f"{year}-{month:02d}-{order['invoice_day']:02d}" if order["invoice_day"] else None
                ),
                "email": order["invoice_email"],
            }
        ]
    }


@app.get("/refunds")
async def get_refunds(
    user_id: str = Query(...),
    period: Optional[str] = Query(default=None),
    x_service_token: str = Header(...),
    x_tenant_id: str = Header(...),
    x_acting_user_id: str = Header(...),
) -> dict:
    await _guard(x_service_token, x_tenant_id, x_acting_user_id, user_id)
    refund = _get_refund(x_tenant_id, user_id)
    if refund["refund_bank_card"] is None:
        return {"refunds": []}
    return {
        "refunds": [
            {
                "status": refund["refund_status"],
                "amount": refund["refund_amount"],
                "bank_card": refund["refund_bank_card"],
            }
        ]
    }


@app.get("/balance")
async def get_balance(
    user_id: str = Query(...),
    x_service_token: str = Header(...),
    x_tenant_id: str = Header(...),
    x_acting_user_id: str = Header(...),
) -> dict:
    await _guard(x_service_token, x_tenant_id, x_acting_user_id, user_id)
    balance = _get_balance(x_tenant_id, user_id)
    return {"balance": balance}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
