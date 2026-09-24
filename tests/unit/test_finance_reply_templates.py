"""覆盖 PHASE2.md 2.9 第 5 点的回复模板，字面对照题目给的发票示例。"""
from app.worker.graph.finance import (
    _build_balance_reply,
    _build_bill_reply,
    _build_invoice_reply,
    _build_order_reply,
    _build_refund_reply,
)


def test_invoice_reply_matches_spec_example():
    invoices = [
        {
            "order_no": "EDU-20260812-8831",
            "amount": 2399.0,
            "status": "已开具",
            "sent_at": "2026-08-18",
            "email": "lin.xiaoyu@example.com",
        }
    ]
    reply = _build_invoice_reply(invoices)
    assert reply == (
        "我查到 2026-08 有一笔订单 #EDU-20260812-8831，金额 ¥2,399，发票状态：已开具，"
        "电子发票已于 8 月 18 日发送到 l***@example.com。需要我重发吗？"
    )


def test_invoice_reply_never_leaks_raw_email():
    invoices = [
        {"order_no": "EDU-1", "amount": 100.0, "status": "已开具", "sent_at": "2026-08-01", "email": "a@b.com"}
    ]
    assert "a@b.com" not in _build_invoice_reply(invoices)


def test_invoice_reply_without_issued_invoice_does_not_mention_email():
    invoices = [{"order_no": "EDU-2", "amount": 1899.0, "status": "未开具", "sent_at": None, "email": None}]
    reply = _build_invoice_reply(invoices)
    assert "未开具" in reply
    assert "发送到" not in reply


def test_invoice_reply_empty_list():
    assert "没有查到" in _build_invoice_reply([])


def test_order_reply_formats_amount_with_comma():
    orders = [{"order_no": "EDU-3", "course_name": "春季数学班", "amount": 2399.0, "period": "2026-08"}]
    reply = _build_order_reply(orders)
    assert "¥2,399" in reply
    assert "春季数学班" in reply


def test_bill_reply_includes_status():
    bills = [{"order_no": "EDU-4", "course_name": "春季英语班", "amount": 2599.0, "period": "2026-08", "status": "已支付"}]
    reply = _build_bill_reply(bills)
    assert "已支付" in reply


def test_refund_reply_masks_bank_card():
    refunds = [{"status": "审核中", "amount": 2399.0, "bank_card": "6222021234567890"}]
    reply = _build_refund_reply(refunds)
    assert "6222021234567890" not in reply
    assert "尾号 7890" in reply


def test_refund_reply_without_bank_card_still_reports_status():
    refunds = [{"status": "已完成", "amount": 300.0, "bank_card": None}]
    reply = _build_refund_reply(refunds)
    assert "已完成" in reply
    assert "银行卡" not in reply


def test_refund_reply_empty_list():
    assert "没有查到你的退费记录" in _build_refund_reply([])


def test_balance_reply_formats_two_decimals():
    assert _build_balance_reply(120.0) == "我查到你的账户余额为 ¥120.00。"


def test_balance_reply_none():
    assert "没有查到" in _build_balance_reply(None)
