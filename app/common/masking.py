"""敏感信息脱敏（PHASE2.md 2.9 第 3 点）。

和阶段一的日志脱敏（app/common/logging.py）共用同一套函数——这里定义的规则就是日志脱敏用的规则，
不会出现"财务回复里脱敏格式"和"日志里脱敏格式"不一致的情况。已知字段（邮箱、手机号、银行卡、
身份证）用专门的函数，比正则猜测更精确；`mask_text()` 对不知道具体是什么字段的整段自由文本用
正则兜底。
"""
import re

_EMAIL_RE = re.compile(r"([\w.+-])[\w.+-]*(@[\w-]+\.[\w.-]+)")
_PHONE_RE = re.compile(r"\b(1[3-9]\d)\d{4}(\d{4})\b")
# 中国大陆身份证号固定 18 位：前 3 位 + 中间 11 位 + 后 4 位（最后一位可能是 X）
_ID_CARD_RE = re.compile(r"\b(\d{3})\d{11}([\dXx]{4})\b")
# 银行卡号长度不固定（12~19 位常见），只取后 4 位——前面全部丢弃，不像身份证/手机号那样保留前缀
_BANK_CARD_RE = re.compile(r"\b\d{8,15}(\d{4})\b")


def mask_email(email: str) -> str:
    return _EMAIL_RE.sub(r"\1***\2", email)


def mask_phone(phone: str) -> str:
    return _PHONE_RE.sub(r"\1****\2", phone)


def mask_id_card(id_card: str) -> str:
    return _ID_CARD_RE.sub(r"\1********\2", id_card)


def mask_bank_card(card_no: str) -> str:
    """已知这就是一个银行卡号字段时用——直接取后四位，不用正则去猜卡号在字符串里的位置。"""
    if not card_no:
        return card_no
    return f"尾号 {card_no[-4:]}"


def mask_text(text: str) -> str:
    """对一整段不知道具体结构的自由文本脱敏，正则兜底，按由具体到容易混淆的顺序处理：
    身份证（18 位）先于手机号（11 位）先于银行卡（不定长），避免一个长数字串被前一条规则
    处理到一半、后一条规则又误伤剩下的部分。
    """
    text = _EMAIL_RE.sub(r"\1***\2", text)
    text = _ID_CARD_RE.sub(r"\1********\2", text)
    text = _PHONE_RE.sub(r"\1****\2", text)
    text = _BANK_CARD_RE.sub(lambda m: f"尾号 {m.group(1)}", text)
    return text
