"""Unit tests for knowledge-vault recall (no live search server)."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from ouroboros.config.loader import load_config
from ouroboros.config.models import KMConfig, OuroborosConfig
from ouroboros.km import KMHit, KMRecall, extract_query, render_label
from ouroboros.km import recall as recall_module

SPEC_BODY = "이번 재기획에 성역 없음 원칙을 적용한다"
OTHER_BODY = "날씨가 맑고 창문이 열려 있다"
POSITIVE_QUERY = "Q20. 이번 재기획에 성역 없이 …"
NEGATIVE_QUERY = "Q99. 오늘 점심 뭐 먹을까요"
EXTRACT_QUERY = "Q20. 이번 재기획에 성역 없이 적용할까요 — 재기획 인터뷰"


def _empty(_query: str, _top_k: int) -> list[KMHit]:
    return []


def _constant(hit: KMHit) -> object:
    def _search(_query: str, _top_k: int) -> list[KMHit]:
        return [hit]

    return _search


def _write_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    spec = vault / "100-project" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(SPEC_BODY, encoding="utf-8")
    (vault / "other.md").write_text(OTHER_BODY, encoding="utf-8")
    return vault


def test_text_tier_positive(tmp_path: Path) -> None:
    vault = _write_vault(tmp_path)
    cfg = KMConfig(vault_path=str(vault))
    recaller = KMRecall(cfg)
    hits = KMRecall(cfg, searchers=[recaller._text]).recall(POSITIVE_QUERY)
    assert len(hits) >= 1
    assert hits[0].path.endswith("100-project/spec.md")
    assert hits[0].tier == "text"


def test_text_tier_negative(tmp_path: Path) -> None:
    vault = _write_vault(tmp_path)
    cfg = KMConfig(vault_path=str(vault))
    recaller = KMRecall(cfg)
    assert KMRecall(cfg, searchers=[recaller._text]).recall(NEGATIVE_QUERY) == []


def test_fail_open_graphrag_unreachable() -> None:
    cfg = KMConfig(endpoint="http://127.0.0.1:1", vault_path=None, timeout_seconds=0.2)
    recaller = KMRecall(cfg)
    hits = KMRecall(
        cfg,
        searchers=[recaller._graphrag, _empty, _empty],
    ).recall("anything")
    assert hits == []


def test_fallback_order() -> None:
    hit_b = KMHit(path="b.md", one_line="b", score=0.5, tier="text")
    hit_c = KMHit(path="c.md", one_line="c", score=0.1, tier="text")
    cfg = KMConfig(vault_path=None)
    hits = KMRecall(
        cfg,
        searchers=[_empty, _constant(hit_b), _constant(hit_c)],
    ).recall("query")
    assert hits == [hit_b]


def test_disabled() -> None:
    calls = {"n": 0}

    def spy(query: str, top_k: int) -> list[KMHit]:
        calls["n"] += 1
        return [KMHit(path="x.md", one_line="x", score=1.0, tier="text")]

    hits = KMRecall(KMConfig(enabled=False), searchers=[spy]).recall("anything")
    assert hits == []
    assert calls["n"] == 0


def test_render_label_cap() -> None:
    hits = [
        KMHit(
            path=f"note-{i}.md",
            one_line="x" * 200,
            score=0.5,
            tier="text",
        )
        for i in range(10)
    ]
    label = render_label("cap-query", hits, max_chars=600)
    assert len(label) <= 600
    assert render_label("cap-query", [], max_chars=600) == ""
    lowered = label.lower()
    assert "must" not in lowered
    assert "should" not in lowered


def test_extract_query() -> None:
    tokens = extract_query(EXTRACT_QUERY).split()
    assert len(tokens) <= 7
    assert len(tokens) == len(set(tokens))
    assert "20" not in tokens


def test_config_defaults(tmp_path: Path) -> None:
    assert OuroborosConfig().km.top_k == 3
    config_path = tmp_path / "config.yaml"
    config_path.write_text("km:\n  top_k: 5\n  enabled: false\n", encoding="utf-8")
    loaded = load_config(config_path)
    assert loaded.km.top_k == 5
    assert loaded.km.enabled is False


def test_obsidian_cli_nonzero_exit_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recall_module.shutil, "which", lambda _name: "/fake/obsidian-cli")

    def _run(args, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args, returncode=1, stdout='["stale.md"]', stderr="boom"
        )

    monkeypatch.setattr(recall_module.subprocess, "run", _run)
    recaller = KMRecall(KMConfig(vault_path=None), searchers=None)
    assert recaller._obsidian_cli("stale note", 3) == []


def test_obsidian_cli_zero_exit_returns_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recall_module.shutil, "which", lambda _name: "/fake/obsidian-cli")

    def _run(args, **_kwargs):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(
            args, returncode=0, stdout='["notes/a.md","notes/b.md"]', stderr=""
        )

    monkeypatch.setattr(recall_module.subprocess, "run", _run)
    recaller = KMRecall(KMConfig(vault_path=None), searchers=None)
    hits = recaller._obsidian_cli("stale note", 3)
    assert len(hits) == 2
    assert hits[0].path == "notes/a.md"
    assert hits[0].tier == "obsidian_cli"


def test_recall_log_tier_uses_searcher_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def custom(q: str, k: int) -> list[KMHit]:
        raise RuntimeError("x")

    seen: list[dict[str, object]] = []

    def _info(event: str, **kwargs: object) -> None:
        seen.append({"event": event, **kwargs})

    monkeypatch.setattr(recall_module.log, "info", _info)
    assert KMRecall(KMConfig(vault_path=None), searchers=[custom]).recall("alpha beta") == []
    assert len(seen) == 1
    assert seen[0]["tier"] == "custom"


def test_extract_query_zero_terms() -> None:
    assert extract_query("alpha beta", max_terms=0) == ""
