"""覆盖 PHASE2.md 2.7 第 7 点：按句子缓冲、去掉禁用套话。"""
from app.worker.graph.guard import OutputGuard


def test_feed_splits_on_sentence_end_and_streams_immediately():
    guard = OutputGuard()
    assert guard.feed("你好") == []  # 还没遇到句末标点，先攒着
    assert guard.feed("，在吗？下一句还没完") == ["你好，在吗？"]
    assert guard.flush() == ["下一句还没完"]


def test_multiple_sentences_in_one_delta():
    guard = OutputGuard()
    sentences = guard.feed("第一句。第二句！第三句还没完")
    assert sentences == ["第一句。", "第二句！"]
    assert guard.flush() == ["第三句还没完"]


def test_banned_phrase_is_stripped_but_rest_of_sentence_kept():
    guard = OutputGuard()
    sentences = guard.feed("希望对你有帮助！")
    # 套话删完只剩标点，这句话没有信息量，不发给用户；但要记一次删除
    assert sentences == []
    assert guard.banned_phrases_removed == 1


def test_banned_phrase_removed_from_middle_of_sentence_keeps_rest():
    guard = OutputGuard()
    sentences = guard.feed("总之，你可以直接联系客服。")
    assert sentences == ["，你可以直接联系客服。"]
    assert guard.banned_phrases_removed == 1


def test_no_banned_phrase_passes_through_unchanged():
    guard = OutputGuard()
    sentences = guard.feed("寒假班请假需要提前 24 小时提交。")
    assert sentences == ["寒假班请假需要提前 24 小时提交。"]
    assert guard.banned_phrases_removed == 0


def test_flush_on_empty_buffer_returns_nothing():
    guard = OutputGuard()
    assert guard.flush() == []


# ---------- 出处核对 + 出处开头合并（PHASE2.md 2.8 第 4、5 点） ----------


def test_sentence_with_allowed_citation_passes_through():
    guard = OutputGuard(allowed_citations=[("课程服务协议", "4.2")])
    sentences = guard.feed("寒假班请假需提前 24 小时提交。")
    assert sentences == ["寒假班请假需提前 24 小时提交。"]
    assert guard.dropped_sentences == 0


def test_sentence_with_disallowed_citation_is_dropped():
    guard = OutputGuard(allowed_citations=[("课程服务协议", "4.2")])
    sentences = guard.feed("根据《课程服务协议》第 9.9 条，所有课程都可以随时全额退款。")
    assert sentences == []
    assert guard.dropped_sentences == 1


def test_dropped_sentence_records_disallowed_citation_number_only():
    # PHASE4.md 4.6 人审发现：只有 dropped_sentences 计数排查不出具体是哪条编造的出处被拦下，
    # 补记编号，但不能把整句原文也记下来
    guard = OutputGuard(allowed_citations=[("课程服务协议", "4.2")])
    guard.feed("根据《课程服务协议》第 9.9 条，所有课程都可以随时全额退款。")
    assert guard.dropped_citations == [("课程服务协议", "9.9")]


def test_dropped_sentence_with_multiple_citations_records_only_disallowed_ones():
    guard = OutputGuard(allowed_citations=[("课程服务协议", "4.2")])
    guard.feed("参见《课程服务协议》第 4.2 条和《退费政策》第 9.9 条，随时可退款。")
    assert guard.dropped_citations == [("退费政策", "9.9")]


def test_citation_without_book_title_brackets_is_not_checked():
    # "本协议第 5.2 条"没有书名号，不是我们要核对的出处格式，不应该被误伤
    guard = OutputGuard(allowed_citations=[("课程服务协议", "4.2")])
    sentences = guard.feed("寒假班的退费规则见本协议第 5.2 条。")
    assert sentences == ["寒假班的退费规则见本协议第 5.2 条。"]
    assert guard.dropped_sentences == 0


def test_lead_in_is_merged_with_first_successful_sentence_only():
    guard = OutputGuard(allowed_citations=[("课程服务协议", "4.2")], lead_in="依据《课程服务协议》第 4.2 条：")
    sentences = guard.feed(
        "根据《课程服务协议》第 9.9 条，随时可退款。寒假班请假需提前 24 小时提交。后续内容。"
    )
    # 第一句因为出处不在允许范围内被丢掉，出处开头不会跟着空句子一起浪费掉，
    # 而是跟第一句真正发出去的句子拼在一起
    assert sentences == ["依据《课程服务协议》第 4.2 条：寒假班请假需提前 24 小时提交。", "后续内容。"]
    assert guard.dropped_sentences == 1
    assert guard.emitted_any is True


def test_all_sentences_dropped_leaves_lead_in_unconsumed():
    guard = OutputGuard(allowed_citations=[("课程服务协议", "4.2")], lead_in="依据《课程服务协议》第 4.2 条：")
    sentences = guard.feed("根据《课程服务协议》第 9.9 条，随时可退款。")
    assert sentences == []
    assert guard.emitted_any is False  # respond() 靠这个字段判断要不要拼兜底话术


def test_no_allowed_citations_means_no_citation_check():
    # allowed_citations=None（chitchat 等非知识问答场景）不做出处核对，随便写"出处"也不会被丢
    guard = OutputGuard()
    sentences = guard.feed("根据《课程服务协议》第 9.9 条，随时可退款。")
    assert sentences == ["根据《课程服务协议》第 9.9 条，随时可退款。"]
    assert guard.dropped_sentences == 0
