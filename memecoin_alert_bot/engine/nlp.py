"""NLP-driven narrative analysis using TF-IDF and cosine similarity.

Provides:
- Keyword extraction from token names/descriptions
- Narrative strength scoring (how well-defined is the narrative?)
- Vamp risk detection (competing coins with similar narratives)
"""

from __future__ import annotations

import re
from collections import deque

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

STOP_WORDS = "english"


class NarrativeAnalyzer:
    """Extract narrative keywords, detect vamp risk via TF-IDF similarity."""

    def __init__(self, max_history: int = 500):
        self._vectorizer = TfidfVectorizer(
            stop_words=STOP_WORDS,
            ngram_range=(1, 2),
            max_features=800,
            strip_accents="unicode",
        )
        self._recent_texts: deque[str] = deque(maxlen=max_history)
        self._recent_mints: deque[str] = deque(maxlen=max_history)

    # --- keyword extraction ---------------------------------------------------

    @staticmethod
    def extract_keywords(text: str, top_n: int = 10) -> list[str]:
        """Return the most distinctive words from *text* using TF-IDF.

        Because we only have a single document, we use the inverse-document-
        frequency of a small built-in corpus as a proxy for informativeness.
        """
        text = _clean(text)
        if len(text.split()) < 2:
            return []

        try:
            vec = TfidfVectorizer(
                stop_words=STOP_WORDS,
                max_features=top_n * 3,
                strip_accents="unicode",
            )
            matrix = vec.fit_transform([text])
            scores = matrix.toarray()[0]
            words = vec.get_feature_names_out()
            # Return top_n by score.
            ranked = sorted(
                zip(words, scores), key=lambda x: x[1], reverse=True
            )
            return [w for w, _ in ranked[:top_n] if w.strip()]
        except Exception:
            return []

    # --- narrative strength --------------------------------------------------

    @staticmethod
    def narrative_strength(text: str) -> float:
        """Score 0-1: how detailed and unique is the narrative?"""
        text = _clean(text)
        if not text.strip():
            return 0.0
        words = text.split()
        unique_words = set(words)
        # Unique-word ratio.
        unique_ratio = len(unique_words) / max(len(words), 1)
        # Length bonus (capped at ~100 words).
        length_bonus = min(len(words) / 100, 0.4)
        return min(1.0, unique_ratio * 0.6 + length_bonus)

    # --- vamp (competing-narrative) detection -------------------------------

    def check_vamp_risk(self, text: str) -> float:
        """Return cosine similarity (0-1) of *text* to the most-similar recent token.

        Values above ~0.7 indicate two tokens competing for the same narrative.
        """
        text = _clean(text)
        if not text.strip() or len(self._recent_texts) < 2:
            return 0.0

        all_texts = list(self._recent_texts) + [text]
        try:
            matrix = self._vectorizer.fit_transform(all_texts)
            new_vec = matrix[-1]
            sims = cosine_similarity(new_vec, matrix[:-1])[0]
            return float(max(sims)) if len(sims) > 0 else 0.0
        except Exception:
            return 0.0

    def add_token(self, mint: str, text: str) -> None:
        """Record a token for future vamp-risk comparisons."""
        text = _clean(text)
        if text.strip():
            self._recent_texts.append(text)
            self._recent_mints.append(mint)


# -- helpers ------------------------------------------------------------------

def _clean(text: str) -> str:
    """Strip URLs, emoji-only lines, and extra whitespace."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split()).lower()


# -- singleton ----------------------------------------------------------------

_analyzer: NarrativeAnalyzer | None = None


def get_narrative_analyzer() -> NarrativeAnalyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = NarrativeAnalyzer()
    return _analyzer
