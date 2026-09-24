"""覆盖 PHASE2.md 2.9 第 3 点的脱敏格式，字面对照题目给的例子。"""
from app.common.masking import mask_bank_card, mask_email, mask_id_card, mask_phone, mask_text


def test_mask_email_matches_spec_example():
    assert mask_email("lin.xiaoyu@example.com") == "l***@example.com"


def test_mask_phone_matches_spec_example():
    assert mask_phone("13812345678") == "138****5678"


def test_mask_bank_card_keeps_only_last_four():
    assert mask_bank_card("6222021234567890") == "尾号 7890"


def test_mask_bank_card_empty_input_passthrough():
    assert mask_bank_card("") == ""


def test_mask_id_card_keeps_front_three_and_back_four():
    masked = mask_id_card("310101199003077890")
    assert masked.startswith("310")
    assert masked.endswith("7890")
    assert "199003077" not in masked  # 中间号段被打码


def test_mask_text_handles_email_phone_id_card_bank_card_together():
    text = (
        "联系人邮箱 lin.xiaoyu@example.com，手机号 13812345678，"
        "身份证 310101199003077890，退款银行卡 6222021234567890。"
    )
    masked = mask_text(text)
    assert "lin.xiaoyu@example.com" not in masked
    assert "13812345678" not in masked
    assert "310101199003077890" not in masked
    assert "6222021234567890" not in masked
    assert "l***@example.com" in masked
    assert "138****5678" in masked
    assert "尾号 7890" in masked


def test_mask_text_does_not_touch_unrelated_numbers():
    assert mask_text("寒假班请假需提前 24 小时提交") == "寒假班请假需提前 24 小时提交"
