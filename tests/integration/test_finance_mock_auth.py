"""覆盖题目 6.2 点名的"财务 mock：带正确 token 能查，越权返回 403"。

直接调用 app.common.finance_client.fetch_finance_data，打真实网络请求到 mock-finance 容器
（不 mock 这一层）：这是"两层权限校验"里独立于 worker 业务判断的第二层（app/worker/graph/
finance.py 顶部注释），要单独证明就算不经过 worker，mock-finance 自己也会拒绝越权。
"""
import pytest

from app.common.finance_client import FinanceForbidden, fetch_finance_data


@pytest.mark.asyncio
async def test_correct_token_and_own_data_succeeds():
    data = await fetch_finance_data(
        "balance", tenant_id="t_a", acting_user_id="u_a_1001", target_user_id="u_a_1001"
    )
    assert data["balance"] == 120.0


@pytest.mark.asyncio
async def test_cross_user_query_without_guardian_link_is_forbidden():
    # u_a_1002 是 u_a_1001 的家长（有关联），但跟 u_a_1004 完全没有关联关系——越权
    with pytest.raises(FinanceForbidden):
        await fetch_finance_data(
            "balance", tenant_id="t_a", acting_user_id="u_a_1002", target_user_id="u_a_1004"
        )


@pytest.mark.asyncio
async def test_guardian_link_allows_parent_to_query_linked_student():
    # 反证：u_a_1002 查自己真正关联的学员 u_a_1001，应该放行——证明上一条测试拒绝的原因
    # 是"没有关联关系"，不是这个 acting_user_id 本身被写死拒绝
    data = await fetch_finance_data(
        "balance", tenant_id="t_a", acting_user_id="u_a_1002", target_user_id="u_a_1001"
    )
    assert data["balance"] == 120.0
