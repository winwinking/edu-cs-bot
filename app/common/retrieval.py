"""知识检索接口：`search(tenant_id, query, top_k)` 统一入口，两种实现按 RETRIEVER 环境变量切换
（阶段二 2.3）。tenant_id 是必填参数，没有默认值，为空直接报错——检索是租户隔离最容易出漏洞的
地方，宁可在最外层就拦住，不依赖调用方记得传。
"""
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import select

from app.common.config import get_settings
from app.common.db import AsyncSessionLocal
from app.common.embedding import get_embedder
from app.common.models import KnowledgeChunk

settings = get_settings()


@dataclass
class SearchResult:
    doc_id: str
    doc_title: str
    clause_no: str
    clause_title: str
    content: str
    score: float


class Retriever(Protocol):
    # 阈值是检索器自身的属性，不是外部配置项：pgvector 和 mock_knowledge 的打分尺度完全不同
    # （见 docs/phase2_threshold.md），共用一个阈值会导致切换检索器后知识问答静默失效。
    # 调用方（2.8 知识问答节点）应该从当前检索器实例上取 min_score，不要自己读某个固定配置。
    min_score: float

    async def search(self, tenant_id: str, query: str, top_k: int = 3) -> list[SearchResult]: ...


def _require_tenant_id(tenant_id: str) -> None:
    if not tenant_id:
        raise ValueError("tenant_id 是必填参数，不能为空")


class PgvectorRetriever:
    """一条 SQL 完成租户过滤和相似度排序：WHERE tenant_id = :tenant_id ORDER BY embedding <=> :query_vec。
    租户过滤和排序在同一条查询里，不会有"先查出来再在应用层过滤"的空档，杜绝跨租户串数据。
    """

    def __init__(self) -> None:
        self._embedder = get_embedder()
        self.min_score = settings.knowledge_min_score

    async def search(self, tenant_id: str, query: str, top_k: int = 3) -> list[SearchResult]:
        _require_tenant_id(tenant_id)
        query_vector = self._embedder.embed([query])[0]

        distance = KnowledgeChunk.embedding.cosine_distance(query_vector).label("distance")
        stmt = (
            select(
                KnowledgeChunk.doc_id,
                KnowledgeChunk.doc_title,
                KnowledgeChunk.clause_no,
                KnowledgeChunk.clause_title,
                KnowledgeChunk.content,
                distance,
            )
            .where(KnowledgeChunk.tenant_id == tenant_id)
            .order_by(distance)
            .limit(top_k)
        )
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(stmt)).all()

        return [
            SearchResult(
                doc_id=row.doc_id,
                doc_title=row.doc_title,
                clause_no=row.clause_no,
                clause_title=row.clause_title,
                content=row.content,
                score=1.0 - row.distance,
            )
            for row in rows
        ]


class MockKnowledgeRetriever:
    """调用 mock-knowledge 的 /search（附录 C 契约），超时 2 秒——外部调用必须有超时，
    检索失败不应该拖住整个知识问答流程。
    """

    def __init__(self) -> None:
        self.min_score = settings.mock_knowledge_min_score

    async def search(self, tenant_id: str, query: str, top_k: int = 3) -> list[SearchResult]:
        _require_tenant_id(tenant_id)
        async with httpx.AsyncClient(timeout=settings.mock_knowledge_timeout_seconds) as client:
            resp = await client.get(
                f"{settings.mock_knowledge_base_url}/search",
                params={"q": query, "tenant_id": tenant_id, "top_k": top_k},
            )
            resp.raise_for_status()
            data = resp.json()

        return [
            SearchResult(
                doc_id=item["source"]["doc_id"],
                doc_title=item["source"]["title"],
                clause_no=item["source"]["clause_no"],
                # mock-knowledge 的契约里没有单独的条款标题字段，只有整段 snippet
                clause_title="",
                content=item["snippet"],
                score=item["score"],
            )
            for item in data["results"]
        ]


def get_retriever(name: str | None = None) -> Retriever:
    retriever_name = name or settings.retriever
    if retriever_name == "pgvector":
        return PgvectorRetriever()
    if retriever_name == "mock_knowledge":
        return MockKnowledgeRetriever()
    raise ValueError(f"未知的 RETRIEVER: {retriever_name}")
