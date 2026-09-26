"""覆盖题目 6.2 点名的"知识检索：检索 SQL 带 tenant_id，t_a 查不到 t_b 的条款"。

用真实 pgvector 数据库（种子数据由 `make seed` + `make reindex` 灌进去），不 mock 检索层——
这条 SQL 本身（WHERE tenant_id = :tenant_id）能不能真的挡住跨租户数据，只有连真实数据库才测得出来，
tests/unit 里手写假数据测不出"SQL 语句本身忘了加 tenant_id 过滤"这类问题。

"三人拼团"是 t_b 独有的活动条款（t_a 对应位置是"老带新"，见 data/knowledge/{t_a,t_b}/activities.md），
两边用词完全不同，用这个词当查询语句，能不能查到就是租户隔离生不生效的直接证据。
"""
import pytest

from app.common.retrieval import PgvectorRetriever

_TENANT_B_ONLY_PHRASE = "三人拼团"


@pytest.mark.asyncio
async def test_tenant_a_cannot_retrieve_tenant_b_only_clause():
    retriever = PgvectorRetriever()
    results = await retriever.search("t_a", f"{_TENANT_B_ONLY_PHRASE}活动怎么参加", top_k=5)

    assert all(_TENANT_B_ONLY_PHRASE not in r.content for r in results)
    assert all(_TENANT_B_ONLY_PHRASE not in r.clause_title for r in results)


@pytest.mark.asyncio
async def test_tenant_b_can_retrieve_its_own_clause():
    # 反证：同一份检索代码、同一个查询词，换成 t_b 自己就应该能查到——证明上面查不到是因为
    # tenant_id 过滤生效了，不是这个查询词本身检索不出任何结果
    retriever = PgvectorRetriever()
    results = await retriever.search("t_b", f"{_TENANT_B_ONLY_PHRASE}活动怎么参加", top_k=5)

    assert any(_TENANT_B_ONLY_PHRASE in r.content for r in results)
