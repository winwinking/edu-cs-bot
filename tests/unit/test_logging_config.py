"""覆盖 PHASE4.md 4.6 人审确认第 2 条：httpx/httpcore 的 logger 级别要调到 WARNING，去掉
每次 LLM 调用都打的纯文本 HTTP Request 日志（这行不带 trace_id，格式也不是 JSON）。
"""
import logging

from app.common.logging import configure_logging


def test_configure_logging_silences_httpx_and_httpcore_info_logs():
    configure_logging()

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
