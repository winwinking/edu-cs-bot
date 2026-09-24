"""解析 data/knowledge/{tenant_id}/*.md 知识文档：front matter + 按章节/条款切块。

格式固定（阶段二 2.2）：开头是 front matter；`## ` 是章，`### ` 是条款，条款标题行第一个词是
条款号（如 4.2、Q3），后面是条款标题；一个条款切成一块。
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ParsedClause:
    chapter: str
    clause_no: str
    clause_title: str
    content: str


@dataclass
class ParsedDocument:
    doc_id: str
    title: str
    tenant_id: str
    tenant_name: str
    version: str
    updated_at: datetime
    clauses: list[ParsedClause]


class KnowledgeParseError(Exception):
    """文档格式不对（缺字段、front matter 的 tenant_id 和所在目录不一致等），调用方应该跳过这份文档"""


_REQUIRED_FRONT_MATTER_FIELDS = ("doc_id", "title", "tenant_id", "tenant_name", "version", "updated_at")


def parse_document(path: Path, expected_tenant_id: str) -> ParsedDocument:
    text = path.read_text(encoding="utf-8")
    front_matter, body = _split_front_matter(text, path)

    missing = [f for f in _REQUIRED_FRONT_MATTER_FIELDS if f not in front_matter]
    if missing:
        raise KnowledgeParseError(f"{path}: front matter 缺少字段 {missing}")

    if front_matter["tenant_id"] != expected_tenant_id:
        raise KnowledgeParseError(
            f"{path}: front matter 里 tenant_id={front_matter['tenant_id']!r}，"
            f"和所在目录 {expected_tenant_id!r} 不一致"
        )

    try:
        updated_at = datetime.fromisoformat(front_matter["updated_at"]).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise KnowledgeParseError(f"{path}: updated_at 格式不对：{front_matter['updated_at']!r}") from exc

    clauses = _parse_clauses(body)
    if not clauses:
        raise KnowledgeParseError(f"{path}: 没有解析出任何条款")

    return ParsedDocument(
        doc_id=front_matter["doc_id"],
        title=front_matter["title"],
        tenant_id=front_matter["tenant_id"],
        tenant_name=front_matter["tenant_name"],
        version=front_matter["version"],
        updated_at=updated_at,
        clauses=clauses,
    )


def _split_front_matter(text: str, path: Path) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise KnowledgeParseError(f"{path}: 缺少 front matter（文件必须以 --- 开头）")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise KnowledgeParseError(f"{path}: front matter 没有找到结束的 ---") from exc

    front_matter: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        front_matter[key.strip()] = value.strip()

    body = "\n".join(lines[end + 1 :])
    return front_matter, body


def _parse_clauses(body: str) -> list[ParsedClause]:
    clauses: list[ParsedClause] = []
    chapter = ""
    clause_no: str | None = None
    clause_title = ""
    content_lines: list[str] = []

    def flush() -> None:
        if clause_no is not None:
            clauses.append(
                ParsedClause(
                    chapter=chapter,
                    clause_no=clause_no,
                    clause_title=clause_title,
                    content="\n".join(content_lines).strip(),
                )
            )

    for line in body.splitlines():
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
            continue  # 文档大标题，不算章节
        elif clause_no is not None:
            content_lines.append(line)
    flush()
    return clauses


def embedding_text(doc_title: str, clause: ParsedClause) -> str:
    """算向量用的文本 = 文档标题 + 章名 + 条款号 + 条款标题 + 正文（阶段二 1.5）"""
    return f"{doc_title} {clause.chapter} {clause.clause_no} {clause.clause_title} {clause.content}"
