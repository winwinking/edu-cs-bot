"""mock-knowledge：假的知识检索服务（附录 C 契约）。

读的是和真实 pgvector 检索同一套 data/knowledge/*.md 文件，但故意用一个很简单的打分方式
（两字片段重合度），不复用 app/common 里任何解析或 embedding 代码——mock 服务概念上是"假的外部
系统"，应该和内部实现完全独立，换掉真实检索实现不应该影响这个 mock 的行为。
"""
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException

app = FastAPI(title="mock-knowledge")

DATA_DIR = Path("data/knowledge")


def _parse_front_matter(lines: list[str]) -> tuple[dict[str, str], int]:
    front_matter: dict[str, str] = {}
    end = lines.index("---", 1)
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if sep:
            front_matter[key.strip()] = value.strip()
    return front_matter, end


def _parse_clauses(lines: list[str]) -> list[dict[str, str]]:
    clauses: list[dict[str, str]] = []
    chapter = ""
    clause_no = None
    clause_title = ""
    content_lines: list[str] = []

    def flush() -> None:
        if clause_no is not None:
            clauses.append(
                {
                    "chapter": chapter,
                    "clause_no": clause_no,
                    "clause_title": clause_title,
                    "content": "\n".join(content_lines).strip(),
                }
            )

    for line in lines:
        if line.startswith("### "):
            flush()
            heading = line[len("### ") :].strip()
            clause_no, _, clause_title = heading.partition(" ")
            clause_title = clause_title.strip()
            content_lines = []
        elif line.startswith("## "):
            flush()
            chapter = line[len("## ") :].strip()
            clause_no = None
            clause_title = ""
            content_lines = []
        elif line.startswith("# "):
            continue
        elif clause_no is not None:
            content_lines.append(line)
    flush()
    return clauses


def _load_tenant_docs(tenant_id: str) -> list[dict]:
    """一份文档解析出 front matter + 条款列表；格式不对的文件直接跳过，不让一份坏文件搞垮整个检索"""
    tenant_dir = DATA_DIR / tenant_id
    if not tenant_dir.is_dir():
        return []

    docs = []
    for path in sorted(tenant_dir.glob("*.md")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            front_matter, end = _parse_front_matter(lines)
            if front_matter.get("tenant_id") != tenant_id:
                continue
            docs.append(
                {
                    "doc_id": front_matter["doc_id"],
                    "title": front_matter["title"],
                    "clauses": _parse_clauses(lines[end + 1 :]),
                }
            )
        except (ValueError, KeyError):
            continue
    return docs


def _bigrams(text: str) -> set[str]:
    chars = [c for c in text if not c.isspace()]
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def _score(query: str, text: str) -> float:
    """两字片段（bigram）重合度打分：Jaccard 相似度，值域 [0, 1]"""
    query_grams = _bigrams(query)
    text_grams = _bigrams(text)
    if not query_grams or not text_grams:
        return 0.0
    return len(query_grams & text_grams) / len(query_grams | text_grams)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/search")
async def search(q: str, tenant_id: str = "", top_k: int = 3) -> dict:
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id 是必填参数")

    docs = _load_tenant_docs(tenant_id)
    scored = []
    for doc in docs:
        for clause in doc["clauses"]:
            text = f"{doc['title']} {clause['chapter']} {clause['clause_no']} {clause['clause_title']} {clause['content']}"
            scored.append((_score(q, text), doc, clause))

    scored.sort(key=lambda item: item[0], reverse=True)
    results = [
        {
            "snippet": clause["content"],
            "source": {"doc_id": doc["doc_id"], "title": doc["title"], "clause_no": clause["clause_no"]},
            "score": score,
        }
        for score, doc, clause in scored[:top_k]
    ]
    return {"results": results}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
