"""覆盖 PHASE2.md 2.8 第 1、4 点：检索前的问题改写、出处开头的生成格式。"""
from app.common.retrieval import SearchResult
from app.worker.graph.knowledge import _build_lead_in, _rewrite_query


def _history(*user_contents: str) -> list[dict]:
    return [{"role": "user", "content": c} for c in user_contents]


def test_short_question_gets_previous_question_prepended_and_repeated():
    # 当前问题在拼接结果里出现两次、上一条问题只出现一次——让当前问题在哈希向量里权重更高，
    # 不会被上一轮问题里的词面带偏检索排序（见 AGENT_LOG 步骤 2.8 的调整说明）
    history = _history("常规班请假要提前多久？")
    content = "那寒假班呢？"
    assert _rewrite_query(content, history) == "常规班请假要提前多久？" + content + content


def test_followup_word_triggers_rewrite_even_if_not_short():
    history = _history("常规班请假要提前多久呢，我记不清楚了？")
    content = "那寒假班的规则是不是也一样呢？"  # 超过 8 个字，但含"那""呢"，仍然要改写
    assert _rewrite_query(content, history) == history[0]["content"] + content + content


def test_long_question_without_followup_word_is_not_rewritten():
    content = "寒假班请假需要提前多长时间提交申请才不会被扣课时费"
    assert _rewrite_query(content, _history("上一个问题")) == content


def test_no_previous_user_message_keeps_original_content():
    # 历史里只有 assistant 消息（比如第一轮对话），没有上一条用户问题可拼，原样检索
    history = [{"role": "assistant", "content": "你好，我在"}]
    assert _rewrite_query("那呢？", history) == "那呢？"


def test_repeating_current_question_is_not_word_specific():
    # 改写逻辑本身不认识"寒假班""常规班"这些具体词，纯粹是"当前问题重复一次"这个通用规则——
    # 换一组完全不相关的词也应该是同样的拼接方式
    history = _history("上课需要带什么设备？")
    content = "那网络要求呢？"
    assert _rewrite_query(content, history) == "上课需要带什么设备？" + content + content


def test_build_lead_in_single_citation():
    citations = [SearchResult(doc_id="d", doc_title="课程服务协议", clause_no="4.2", clause_title="", content="", score=0.9)]
    assert _build_lead_in(citations) == "依据《课程服务协议》第 4.2 条："


def test_build_lead_in_multiple_citations_joined_by_dunhao():
    citations = [
        SearchResult(doc_id="a", doc_title="课程服务协议", clause_no="4.2", clause_title="", content="", score=0.9),
        SearchResult(doc_id="b", doc_title="请假规则", clause_no="2.2", clause_title="", content="", score=0.8),
    ]
    assert _build_lead_in(citations) == "依据《课程服务协议》第 4.2 条、《请假规则》第 2.2 条："
