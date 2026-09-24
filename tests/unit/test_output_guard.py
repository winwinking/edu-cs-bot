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
