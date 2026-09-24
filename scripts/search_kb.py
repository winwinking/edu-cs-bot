"""命令行知识检索：打印前 3 条结果和分数，支持 --retriever 切换实现（阶段二 2.3）。

用法：python scripts/search_kb.py --tenant t_a "寒假班请假会退课时费吗"
"""
import argparse
import asyncio

from app.common.retrieval import get_retriever


async def main(tenant: str, query: str, top_k: int, retriever_name: str) -> None:
    retriever = get_retriever(retriever_name)
    print(f"[检索器] {type(retriever).__name__}  阈值(min_score)={retriever.min_score:.4f}")

    results = await retriever.search(tenant, query, top_k=top_k)

    if not results:
        print("没有检索到任何结果")
        return

    for i, r in enumerate(results, start=1):
        title = f"《{r.doc_title}》第 {r.clause_no} 条" if r.clause_no else r.doc_title
        print(f"[{i}] score={r.score:.4f} {title} {r.clause_title}")
        print(f"    {r.content}")

    top_score = results[0].score
    verdict = "超过阈值，可以作为依据" if top_score >= retriever.min_score else "低于阈值，判定为没有查到明确依据"
    print(f"[判定] 第一名 score={top_score:.4f}，{verdict}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="命令行知识检索")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--retriever", choices=["pgvector", "mock_knowledge"], default=None)
    parser.add_argument("query")
    args = parser.parse_args()
    asyncio.run(main(args.tenant, args.query, args.top_k, args.retriever))
