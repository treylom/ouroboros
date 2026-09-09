"""Knowledge-recall label injection into interview, seed, and PM prompts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine, InterviewRound, InterviewState
from ouroboros.bigbang.pm_interview import PMInterviewEngine
from ouroboros.bigbang.seed_generator import SeedGenerator
from ouroboros.config.models import KMConfig
from ouroboros.km import KMHit, KMRecall


def _empty_search(_query: str, _top_k: int) -> list[KMHit]:
    return []


def _hit_search(_query: str, _top_k: int) -> list[KMHit]:
    return [KMHit("100-project/spec.md", "existing note", 0.9, "text")]


def _hits() -> KMRecall:
    return KMRecall(KMConfig(), searchers=[_hit_search])


def _empty() -> KMRecall:
    return KMRecall(KMConfig(), searchers=[_empty_search])


def _state() -> InterviewState:
    return InterviewState(
        interview_id="km-inject",
        initial_context="Build a CLI tool",
        rounds=[
            InterviewRound(round_number=1, question="What platform?", user_response="Linux"),
        ],
    )


def test_interview_stub_hits_include_label() -> None:
    engine = InterviewEngine(llm_adapter=MagicMock())
    prompt = engine._build_system_prompt(_state(), km_recall=_hits(), max_chars=4000)
    assert "## Knowledge recall" in prompt
    assert "100-project/spec.md" in prompt


def test_interview_empty_stub_matches_baseline_bytes() -> None:
    engine = InterviewEngine(llm_adapter=MagicMock())
    state = _state()
    baseline = engine._build_system_prompt(state)
    empty = engine._build_system_prompt(state, km_recall=_empty())
    assert "## Knowledge recall" not in empty
    assert empty == baseline


def test_interview_load_config_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("config unavailable")

    engine = InterviewEngine(llm_adapter=MagicMock())
    monkeypatch.setattr("ouroboros.config.loader.load_config", _boom)
    prompt = engine._build_system_prompt(_state(), km_recall=None)
    assert "Build a CLI tool" in prompt
    assert "## Knowledge recall" not in prompt


def test_seed_and_pm_stub_hits_include_label(tmp_path: Path) -> None:
    state = _state()
    seed_ctx = SeedGenerator(llm_adapter=MagicMock())._build_interview_context(
        state, km_recall=_hits()
    )
    assert "## Knowledge recall" in seed_ctx
    assert "100-project/spec.md" in seed_ctx

    pm = PMInterviewEngine.create(llm_adapter=MagicMock(), state_dir=tmp_path)
    pm._install_pm_steering()
    pm_prompt = pm.inner._build_system_prompt(state, km_recall=_hits(), max_chars=5000)
    assert "## Knowledge recall" in pm_prompt
    assert "100-project/spec.md" in pm_prompt


def test_small_max_prompt_chars_drops_label_keeps_body() -> None:
    engine = InterviewEngine(llm_adapter=MagicMock())
    state = _state()
    # Below header+label+overhead (~704 for this state); above the no-label header.
    cap = 700
    baseline = engine._build_system_prompt(state, km_recall=_empty(), max_chars=cap)
    capped = engine._build_system_prompt(state, km_recall=_hits(), max_chars=cap)
    assert "## Knowledge recall" not in capped
    assert "Build a CLI tool" in capped
    assert capped == baseline


def test_saturated_cap_drops_label_keeps_focus(tmp_path: Path) -> None:
    engine = InterviewEngine(llm_adapter=MagicMock())
    state = _state()
    cap = 1000
    baseline = engine._build_system_prompt(state, km_recall=_empty(), max_chars=cap)
    assert "Focus:" in baseline
    capped = engine._build_system_prompt(state, km_recall=_hits(), max_chars=cap)
    focus_line = baseline[baseline.index("Focus:") :].split("\n", 1)[0]
    assert "## Knowledge recall" not in capped
    assert focus_line in capped
    assert capped == baseline

    generous = engine._build_system_prompt(state, km_recall=_hits(), max_chars=4000)
    assert "## Knowledge recall" in generous
    assert "100-project/spec.md" in generous

    pm = PMInterviewEngine.create(llm_adapter=MagicMock(), state_dir=tmp_path)
    pm._install_pm_steering()
    pm_base = pm.inner._build_system_prompt(state, km_recall=_empty(), max_chars=cap)
    pm_hits = pm.inner._build_system_prompt(state, km_recall=_hits(), max_chars=cap)
    assert "## Knowledge recall" not in pm_hits
    assert pm_hits == pm_base
    pm_wide = pm.inner._build_system_prompt(state, km_recall=_hits(), max_chars=5000)
    assert "## Knowledge recall" in pm_wide


def test_injected_recaller_honors_max_label_chars() -> None:
    from ouroboros.bigbang.interview import _km_data_label

    recaller = KMRecall(KMConfig(max_label_chars=50), searchers=[_hit_search])
    label = _km_data_label("Build a CLI tool Linux", km_recall=recaller)
    assert label
    assert len(label) <= 50
    engine = InterviewEngine(llm_adapter=MagicMock())
    prompt = engine._build_system_prompt(_state(), km_recall=recaller, max_chars=4000)
    assert label in prompt
