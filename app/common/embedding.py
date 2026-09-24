"""embedding 接口：把文本变成向量，供知识库检索使用。

阶段二默认用不依赖网络、不依赖模型的哈希向量（见 REQUIREMENTS/PHASE2 1.5）：
结果完全确定、可复现，压测时不占 CPU；代价是只能匹配字面相近的内容，同义词效果差。
接口做成可替换的（EMBEDDING_PROVIDER 环境变量选实现），以后换成真实 embedding 模型
（比如中文 bge）只需要新增一个实现类，不用改调用方代码。
"""
import hashlib
from typing import Protocol

from app.common.config import get_settings

settings = get_settings()

EMBEDDING_DIM = 512


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedding:
    """把文本拆成单字和相邻两字，每个片段用固定哈希算法映射到 512 维向量的某一位上计数，最后 L2 归一化。

    注意：不能用 Python 内置的 hash()，它每次进程启动的结果不一样（受随机种子影响），
    同一段文字在两次进程里会算出不同的向量，测试没法复现。这里用 hashlib.md5，结果永远固定。
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        for token in self._tokenize(text):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIM
            vector[index] += 1.0
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        # 去空白后取单字 + 相邻两字组合，中文没有天然分词边界，这种做法能兜住大部分字面相近的匹配
        chars = [c for c in text if not c.isspace()]
        bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
        return chars + bigrams


def get_embedder() -> Embedder:
    if settings.embedding_provider == "hash":
        return HashEmbedding()
    raise ValueError(f"未知的 EMBEDDING_PROVIDER: {settings.embedding_provider}")
