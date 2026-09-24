"""知识库增量重建索引（阶段二 2.2）。

按文件内容哈希判断要不要重算：没变的跳过，变了的在一个事务里删旧块、写新块、更新
knowledge_documents；目录里已经删除的文档，连同它的块一起删掉。--force 忽略哈希，全量重建。
"""
import argparse
import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.common.db import AsyncSessionLocal
from app.common.embedding import get_embedder
from app.common.knowledge_parser import KnowledgeParseError, embedding_text, parse_document
from app.common.models import KnowledgeChunk, KnowledgeDocument

KNOWLEDGE_ROOT = Path("data/knowledge")


async def _reindex_one(tenant_id: str, md_path: Path, force: bool, embedder) -> tuple[str, str]:
    """返回 (doc_id, status)，status ∈ added / updated / skipped"""
    parsed = parse_document(md_path, expected_tenant_id=tenant_id)
    content_hash = hashlib.sha256(md_path.read_bytes()).hexdigest()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(KnowledgeDocument.content_hash).where(
                KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.doc_id == parsed.doc_id
            )
        )
        existing_hash = result.scalar_one_or_none()

    is_new = existing_hash is None
    if not force and not is_new and existing_hash == content_hash:
        return parsed.doc_id, "skipped"

    texts = [embedding_text(parsed.title, clause) for clause in parsed.clauses]
    vectors = embedder.embed(texts)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                delete(KnowledgeChunk).where(
                    KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.doc_id == parsed.doc_id
                )
            )
            doc_stmt = (
                pg_insert(KnowledgeDocument)
                .values(
                    tenant_id=tenant_id,
                    doc_id=parsed.doc_id,
                    title=parsed.title,
                    version=parsed.version,
                    content_hash=content_hash,
                    updated_at=parsed.updated_at,
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "doc_id"],
                    set_={
                        "title": parsed.title,
                        "version": parsed.version,
                        "content_hash": content_hash,
                        "updated_at": parsed.updated_at,
                    },
                )
            )
            await session.execute(doc_stmt)

            for clause, vector in zip(parsed.clauses, vectors):
                session.add(
                    KnowledgeChunk(
                        tenant_id=tenant_id,
                        doc_id=parsed.doc_id,
                        doc_title=parsed.title,
                        chapter=clause.chapter,
                        clause_no=clause.clause_no,
                        clause_title=clause.clause_title,
                        content=clause.content,
                        embedding=vector,
                    )
                )

    return parsed.doc_id, ("added" if is_new else "updated")


async def _remove_deleted_docs(scanned_tenant_ids: set[str], seen_docs: set[tuple[str, str]]) -> list[tuple[str, str]]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(KnowledgeDocument.tenant_id, KnowledgeDocument.doc_id).where(
                KnowledgeDocument.tenant_id.in_(scanned_tenant_ids)
            )
        )
        existing_docs = list(result.all())

    removed: list[tuple[str, str]] = []
    for tenant_id, doc_id in existing_docs:
        if (tenant_id, doc_id) in seen_docs:
            continue
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(
                    delete(KnowledgeChunk).where(
                        KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.doc_id == doc_id
                    )
                )
                await session.execute(
                    delete(KnowledgeDocument).where(
                        KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.doc_id == doc_id
                    )
                )
        removed.append((tenant_id, doc_id))
    return removed


async def main(force: bool) -> None:
    embedder = get_embedder()
    results: list[tuple[str, str, str]] = []
    seen_docs: set[tuple[str, str]] = set()

    tenant_dirs = sorted(p for p in KNOWLEDGE_ROOT.iterdir() if p.is_dir())
    for tenant_dir in tenant_dirs:
        tenant_id = tenant_dir.name
        for md_path in sorted(tenant_dir.glob("*.md")):
            try:
                doc_id, status = await _reindex_one(tenant_id, md_path, force, embedder)
            except KnowledgeParseError as exc:
                print(f"[跳过，格式错误] {exc}")
                continue
            seen_docs.add((tenant_id, doc_id))
            results.append((tenant_id, doc_id, status))

    scanned_tenant_ids = {p.name for p in tenant_dirs}
    for tenant_id, doc_id in await _remove_deleted_docs(scanned_tenant_ids, seen_docs):
        results.append((tenant_id, doc_id, "removed"))

    for tenant_id, doc_id, status in results:
        print(f"{tenant_id}/{doc_id}: {status}")

    summary: dict[str, int] = {}
    for _, _, status in results:
        summary[status] = summary.get(status, 0) + 1
    print(f"完成：{summary}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="知识库增量重建索引")
    parser.add_argument("--force", action="store_true", help="忽略内容哈希，全量重建")
    args = parser.parse_args()
    asyncio.run(main(args.force))
