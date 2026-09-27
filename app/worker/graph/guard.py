"""OutputGuard（PHASE2.md 2.7 第 7 点 + 2.8 第 5 点）：按句子缓冲，去掉禁用套话，核对出处。

按句子检查而不是等整段生成完再检查，是为了保留流式输出，首字仍然快。

出处检查（2.8）：句子里出现《X》第 N 条，而 (X, N) 不在这次检索允许引用的范围里，整句丢掉——
LLM 编不出一个真实存在但没被检索到的出处还能蒙混过关，因为核对的是"这次给它看过的资料"，
不是全量知识库。`allowed_citations` 为 None 表示不做这项检查（chitchat 等非知识问答场景）。

出处开头（lead_in）：不单独先发，跟第一句真正通过检查、有内容的句子一起发出，这样即使后面的
句子全被拦下，也不会出现一个孤零零的"依据……"开头。
"""
import re
from typing import Iterable, List, Optional, Tuple

# 遇到这些字符就认为一句话结束了，把缓冲区里攒的内容当一句话输出
_SENTENCE_END_CHARS = "。！？；\n"

# 禁用套话列表（附录 B"禁止或慎用"）：按子串整体删除，不是丢掉整句——保留句子里其余有信息量的内容
BANNED_PHRASES: List[str] = [
    "作为AI助手",
    "作为一个AI",
    "作为AI",
    "我很乐意为你服务",
    "我很乐意",
    "总之，希望对你有所帮助",
    "总之",
    "希望对你有帮助",
    "希望能帮到你",
    "亲亲",
]

# 只认《书名》第 x 条这种带书名号的具体格式，普通的"见本协议第 5.2 条"（没有书名号）不算，
# 不会被误判成编造出处
_CITATION_RE = re.compile(r"《([^》]+)》第\s*([^\s》]+?)\s*条")


class OutputGuard:
    def __init__(
        self,
        allowed_citations: Optional[Iterable[Tuple[str, str]]] = None,
        lead_in: Optional[str] = None,
    ) -> None:
        self._buffer = ""
        self.banned_phrases_removed = 0
        self.dropped_sentences = 0
        # 被丢掉的句子里引用过的出处（书名, 条款号），只记编号不记整句原文——供知识问答路径的
        # 结构化日志用（PHASE4.md 4.6 人审发现：故障注入时靠 trace_id 查不到 OutputGuard 有没有
        # 生效，只有 dropped_sentences 计数不够排查，得知道具体是哪条编造的出处被拦下）
        self.dropped_citations: List[Tuple[str, str]] = []
        self._allowed_citations = {tuple(c) for c in allowed_citations} if allowed_citations is not None else None
        self._lead_in = lead_in
        # 有没有成功发出过至少一句——知识问答场景下，respond() 靠这个字段判断要不要拼兜底话术
        self.emitted_any = False

    def feed(self, delta: str) -> List[str]:
        """喂入一段新的文本分片，返回这次新攒够的完整句子（可能是 0 句、1 句或多句）。"""
        self._buffer += delta
        sentences: List[str] = []
        while True:
            end_idx = self._find_sentence_end()
            if end_idx is None:
                break
            raw_sentence = self._buffer[: end_idx + 1]
            self._buffer = self._buffer[end_idx + 1 :]
            processed = self._process(raw_sentence)
            if processed:
                sentences.append(processed)
        return sentences

    def flush(self) -> List[str]:
        """流结束了，把缓冲区里剩下不够一句（没遇到句末标点）的内容也当最后一句处理。"""
        if not self._buffer:
            return []
        raw_sentence = self._buffer
        self._buffer = ""
        processed = self._process(raw_sentence)
        return [processed] if processed else []

    def _find_sentence_end(self) -> Optional[int]:
        positions = [self._buffer.find(c) for c in _SENTENCE_END_CHARS if c in self._buffer]
        return min(positions) if positions else None

    def _process(self, raw_sentence: str) -> str:
        cleaned = self._clean(raw_sentence)
        if not cleaned:
            return ""
        if self._allowed_citations is not None:
            disallowed = self._find_disallowed_citations(cleaned)
            if disallowed:
                self.dropped_sentences += 1
                self.dropped_citations.extend(disallowed)
                return ""
        if self._lead_in and not self.emitted_any:
            cleaned = self._lead_in + cleaned
        self.emitted_any = True
        return cleaned

    def _find_disallowed_citations(self, sentence: str) -> List[Tuple[str, str]]:
        return [
            (match.group(1), match.group(2))
            for match in _CITATION_RE.finditer(sentence)
            if (match.group(1), match.group(2)) not in self._allowed_citations
        ]

    def _clean(self, sentence: str) -> str:
        cleaned = sentence
        for phrase in BANNED_PHRASES:
            if phrase in cleaned:
                cleaned = cleaned.replace(phrase, "")
                self.banned_phrases_removed += 1
        # 套话删完之后如果只剩标点/空白，这句话已经没有信息量了，不发给用户
        if cleaned.strip(_SENTENCE_END_CHARS + " 　，,"):
            return cleaned
        return ""
