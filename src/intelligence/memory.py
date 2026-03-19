"""Memory Store (s06) - TF-IDF 搜索"""
import math
import re
from pathlib import Path
from typing import List, Dict

MEMORY_FILE = Path("./workspace/MEMORY.md")

class MemoryStore:
    """混合搜索记忆存储 (s06)"""

    def __init__(self):
        self._ensure_file()

    def _ensure_file(self):
        MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not MEMORY_FILE.exists():
            MEMORY_FILE.write_text("", encoding="utf-8")

    def write(self, content: str) -> str:
        """写入记忆"""
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"- {timestamp}: {content}\n")
        return f"Remembered: {content[:50]}..."

    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        """TF-IDF 搜索"""
        chunks = self._load_chunks()
        if not chunks:
            return []

        query_tokens = self._tokenize(query)
        chunk_tokens = [self._tokenize(c["text"]) for c in chunks]

        # 计算 TF-IDF
        df = {}
        for tokens in chunk_tokens:
            for t in set(tokens):
                df[t] = df.get(t, 0) + 1

        n = len(chunks)

        def tfidf(tokens):
            tf = {}
            for t in tokens:
                tf[t] = tf.get(t, 0) + 1
            return {t: c * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1)
                    for t, c in tf.items()}

        def cosine(a, b):
            common = set(a) & set(b)
            if not common:
                return 0.0
            dot = sum(a.get(k, 0) * b.get(k, 0) for k in common)
            na = math.sqrt(sum(v * v for v in a.values()))
            nb = math.sqrt(sum(v * v for v in b.values()))
            return dot / (na * nb) if na and nb else 0.0

        qvec = tfidf(query_tokens)
        scored = []
        for i, tokens in enumerate(chunk_tokens):
            score = cosine(qvec, tfidf(tokens))
            if score > 0:
                scored.append({
                    "text": chunks[i]["text"],
                    "score": score,
                    "path": chunks[i].get("path", "memory"),
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:top_k]

    def _tokenize(self, text: str) -> List[str]:
        text = text.lower()
        tokens = re.findall(r'\w+', text)
        return [t for t in tokens if len(t) > 1]

    def _load_chunks(self) -> List[Dict]:
        chunks = []

        if MEMORY_FILE.exists():
            content = MEMORY_FILE.read_text(encoding="utf-8")
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("-"):
                    chunks.append({"text": line[1:].strip(), "path": "MEMORY.md"})

        return chunks
