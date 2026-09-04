"""The ``km_context`` advisory lane and the hits contract it ships."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

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
