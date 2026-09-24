"""OutputGuard 第一版（PHASE2.md 2.7 第 7 点）：按句子缓冲，去掉禁用套话。

按句子检查而不是等整段生成完再检查，是为了保留流式输出，首字仍然快（2.8 知识问答的出处
校验也会用这同一个类，那时候还会有"整句因为出处对不上被丢掉"的逻辑，本步先只做套话过滤，
`dropped_sentences` 先恒为 0）。
"""
from typing import List, Optional

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


class OutputGuard:
    def __init__(self) -> None:
        self._buffer = ""
        self.banned_phrases_removed = 0
        # 2.8 知识问答会用到：句子里出现的出处如果不在检索结果里，整句丢掉。本步恒为 0
        self.dropped_sentences = 0

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
            cleaned = self._clean(raw_sentence)
            if cleaned:
                sentences.append(cleaned)
        return sentences

    def flush(self) -> List[str]:
        """流结束了，把缓冲区里剩下不够一句（没遇到句末标点）的内容也当最后一句发出去。"""
        if not self._buffer:
            return []
        cleaned = self._clean(self._buffer)
        self._buffer = ""
        return [cleaned] if cleaned else []

    def _find_sentence_end(self) -> Optional[int]:
        positions = [self._buffer.find(c) for c in _SENTENCE_END_CHARS if c in self._buffer]
        return min(positions) if positions else None

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
