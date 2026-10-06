"""Base de conocimiento de FAQs para dar contexto al diagnóstico (RAG).

``FaqKnowledgeBase`` es una recuperación léxica sencilla (demo y tests). En producción con Gemini se
puede apuntar ``FAQ_STORE_NAME`` a un File Search Store administrado y el LLM recupera por su cuenta.
"""

import json
import re
from pathlib import Path

from app.llm import strip_accents

_WORD_RE = re.compile(r"\w{4,}")
MIN_SHARED_WORDS = 2  # menos coincidencias es ruido, no una FAQ aplicable


class FaqKnowledgeBase:
    def __init__(self, faqs: list[dict[str, str]]):
        self._faqs = faqs
        self._index = [(faq, set(_WORD_RE.findall(strip_accents(faq["question"] + " " + faq["answer"])))) for faq in faqs]

    @classmethod
    def from_file(cls, path: str) -> "FaqKnowledgeBase":
        file = Path(path)
        return cls(json.loads(file.read_text(encoding="utf-8")) if file.exists() else [])

    def search(self, text: str, k: int = 3) -> list[str]:
        words = set(_WORD_RE.findall(strip_accents(text)))
        scored = [(len(words & vocabulary), faq) for faq, vocabulary in self._index]
        best = sorted((s for s in scored if s[0] >= MIN_SHARED_WORDS), key=lambda s: -s[0])[:k]
        return [f"{faq['question']} → {faq['answer']}" for _, faq in best]
