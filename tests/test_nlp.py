"""Tests for the NLP narrative analyzer."""

import pytest

from memecoin_alert_bot.engine.nlp import (
    NarrativeAnalyzer,
    _clean,
    get_narrative_analyzer,
)


def test_clean_removes_urls_and_punctuation():
    result = _clean("Check https://example.com for the best $DOGE coin!!!")
    assert "https://example.com" not in result
    assert "$" not in result
    assert "!!!" not in result
    assert "best doge coin" in result


def test_singleton_returns_same_instance():
    a1 = get_narrative_analyzer()
    a2 = get_narrative_analyzer()
    assert a1 is a2


def test_extract_keywords_returns_list():
    na = NarrativeAnalyzer()
    text = "Autonomous AI trading agent for memecoin ecosystem on Solana"
    kw = na.extract_keywords(text)
    assert isinstance(kw, list)
    assert len(kw) > 0
    assert all(isinstance(k, str) for k in kw)


def test_extract_keywords_empty_for_short_text():
    na = NarrativeAnalyzer()
    assert na.extract_keywords("a") == []
    assert na.extract_keywords("") == []


def test_narrative_strength_high_for_detailed_text():
    na = NarrativeAnalyzer()
    short = na.narrative_strength("Moon coin")
    long = na.narrative_strength(
        "The first community-driven AI agent that autonomously trades "
        "memecoins on Solana using neural network predictions and on-chain analysis"
    )
    assert long > short


def test_narrative_strength_zero_for_empty():
    na = NarrativeAnalyzer()
    assert na.narrative_strength("") == 0.0


def test_vamp_risk_zero_for_unique_text():
    na = NarrativeAnalyzer()
    na.add_token("mint1", "A unique autonomous trading bot on Solana")
    na.add_token("mint2", "Completely different DeFi protocol for staking")
    score = na.check_vamp_risk("Another completely different token")
    assert 0.0 <= score <= 1.0


def test_vamp_risk_high_for_similar_text():
    na = NarrativeAnalyzer()
    na.add_token("mint1", "autonomous ai trading agent for memecoins on solana")
    na.add_token("mint2", "decentralized exchange for swapping tokens")
    score = na.check_vamp_risk("ai trading agent for meme coins on solana autonomous")
    # Should be similar to mint1.
    assert score > 0.3
