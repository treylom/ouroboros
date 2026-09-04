"""The ``km_context`` advisory lane and the hits contract it ships."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator
import pytest

from ouroboros.mcp.tools.advisory_prompts import _advisory_output_section
from ouroboros.mcp.tools.authoring_handlers import _build_question_advisory_request
from ouroboros.mcp.tools.question_advisory import (
    _lane_instructions,
    build_question_advisory_request,
    build_question_advisory_subagents,
)
from ouroboros.mcp.tools.subagent import build_interview_question_advisory_subagents
from ouroboros.orchestrator.capabilities.interview_schemas import (
    _interview_km_hits_answer_contract,
    _interview_question_advisory_fanout_metadata,
)

QUESTION = "Has this decision already been written down?"

_ORIGINAL_SIX = {
    "code_context",
    "web_context",
    "data_context",
    "ambiguity_contrarian",
    "answer_simplifier",
    "architecture_implications",
}


def _interview_payloads() -> list[dict[str, Any]]:
    request = _build_question_advisory_request(
        session_id="sess-km",
        question=QUESTION,
        phase="answer",
        score=None,
    )
    return [payload.to_dict() for payload in build_interview_question_advisory_subagents(request)]


def _pm_payloads() -> list[dict[str, Any]]:
    request = build_question_advisory_request(
        tool_name="ouroboros_pm_interview",
        session_id="pm-km",
        question=QUESTION,
        repository_roster=[],
    )
    return [payload.to_dict() for payload in build_question_advisory_subagents(request)]


def _km_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [payload for payload in payloads if payload["context"]["lane_id"] == "km_context"]


def _valid_hits() -> dict[str, Any]:
    return {
        "question_identity": "interview-question:0123456789abcdef",
        "lane_id": "km_context",
        "hits": [
            {
                "path": "notes/sso.md",
                "one_line": "Existing note already covers the SSO decision.",
                "score": 0.81,
                "tier": "graphrag",
            }
        ],
    }


def test_interview_and_pm_emit_one_km_context_payload() -> None:
    interview = _km_payloads(_interview_payloads())
    pm = _km_payloads(_pm_payloads())

    assert len(interview) == 1
    assert len(pm) == 1
    assert interview[0]["context"]["capability"] == "recall_knowledge"
    assert interview[0]["context"]["required"] is False


def test_answer_contract_schema_is_enforceable() -> None:
    schema = _interview_km_hits_answer_contract()["response_model_schema"]
    Draft202012Validator.check_schema(schema)


def test_valid_hits_pass_the_contract() -> None:
    schema = _interview_km_hits_answer_contract()["response_model_schema"]
    assert list(Draft202012Validator(schema).iter_errors(_valid_hits())) == []


def test_tier_web_is_rejected() -> None:
    schema = _interview_km_hits_answer_contract()["response_model_schema"]
    bad = _valid_hits()
    bad["hits"][0]["tier"] = "web"
    assert list(Draft202012Validator(schema).iter_errors(bad)) != []


def test_lane_instructions_are_defined() -> None:
    request = _build_question_advisory_request(
        session_id="sess-km",
        question=QUESTION,
        phase="answer",
        score=None,
    )
    km_lane = next(lane for lane in request["lanes"] if lane["lane_id"] == "km_context")
    assert _lane_instructions("km_context", km_lane, request, {}) is not None


def test_original_six_lane_names_are_unchanged() -> None:
    names = {
        str(lane["lane_id"]) for lane in _interview_question_advisory_fanout_metadata()["lanes"]
    }
    assert names >= _ORIGINAL_SIX


def test_empty_result_requires_question_identity() -> None:
    schema = _interview_km_hits_answer_contract()["response_model_schema"]
    validator = Draft202012Validator(schema)
    without_identity = {"lane_id": "km_context", "hits": []}
    with_identity = {
        "question_identity": "interview-question:0123456789abcdef",
        "lane_id": "km_context",
        "hits": [],
    }
    assert validator.is_valid(without_identity) is False
    assert validator.is_valid(with_identity) is True


def _tier_web() -> dict[str, Any]:
    bad = _valid_hits()
    bad["hits"][0]["tier"] = "web"
    return bad


def _four_hits() -> dict[str, Any]:
    bad = _valid_hits()
    bad["hits"] = [dict(bad["hits"][0]) for _ in range(4)]
    return bad


def _root_extra_key() -> dict[str, Any]:
    bad = _valid_hits()
    bad["notes"] = "extra"
    return bad


def _hit_extra_key() -> dict[str, Any]:
    bad = _valid_hits()
    bad["hits"][0]["body"] = "note body"
    return bad


def _wrong_lane_id() -> dict[str, Any]:
    bad = _valid_hits()
    bad["lane_id"] = "data_context"
    return bad


@pytest.mark.parametrize(
    "build_answer",
    [
        _tier_web,
        _four_hits,
        _root_extra_key,
        _hit_extra_key,
        _wrong_lane_id,
    ],
)
def test_invalid_shapes_are_rejected(build_answer: Any) -> None:
    schema = _interview_km_hits_answer_contract()["response_model_schema"]
    assert Draft202012Validator(schema).is_valid(build_answer()) is False


def test_lane_instruction_empty_shape_includes_identity() -> None:
    request = _build_question_advisory_request(
        session_id="sess-km",
        question=QUESTION,
        phase="answer",
        score=None,
    )
    km_lane = next(lane for lane in request["lanes"] if lane["lane_id"] == "km_context")
    instructions = _lane_instructions("km_context", km_lane, request, {})
    assert instructions is not None
    assert "question_identity" in instructions[0]
    assert "question_identity" in _interview_km_hits_answer_contract()["runtime_instruction"]


def test_optional_lane_prompt_does_not_claim_required() -> None:
    section = _advisory_output_section(_interview_km_hits_answer_contract())
    assert "because this lane is required" not in section
    assert "optional" in section


def test_skill_doc_paths_and_sections() -> None:
    doc = Path(__file__).resolve().parents[4] / "skills" / "km" / "SKILL.md"
    text = doc.read_text(encoding="utf-8")
    assert "~/.claude/km-config.json" in text
    assert "~/.ouroboros/km-config.json" not in text
    assert "## Manual recall once" in text
    assert "\n## Once\n" not in text
    assert "current session only" in text


def test_skill_doc_fenced_json_examples_validate() -> None:
    doc = Path(__file__).resolve().parents[4] / "skills" / "km" / "SKILL.md"
    blocks = re.findall(r"```json\n(.*?)```", doc.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) >= 1
    schema = _interview_km_hits_answer_contract()["response_model_schema"]
    validator = Draft202012Validator(schema)
    for block in blocks:
        assert validator.is_valid(json.loads(block))


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("bad", False),
        ("interview-question:xyz", False),
        ("pm-question:0123456789abcde", False),
        ("pm-question:0123456789abcdef", True),
    ],
)
def test_invalid_question_identity_pattern_is_rejected(identity: str, expected: bool) -> None:
    schema = _interview_km_hits_answer_contract()["response_model_schema"]
    candidate = _valid_hits()
    candidate["question_identity"] = identity
    assert Draft202012Validator(schema).is_valid(candidate) is expected


def test_child_prompt_has_no_output_contradiction() -> None:
    prompt = _km_payloads(_interview_payloads())[0]["prompt"]
    assert "not for your output — except a field the contract itself" in prompt
    assert "question_identity" in prompt
    assert "not for your output." not in prompt
