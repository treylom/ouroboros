"""An interview question is a draft until its required advisory lanes complete.

Owner report 2026-11-23: the host showed interview questions 10-12 to the user
while the required lanes (data_context, ambiguity_contrarian, answer_simplifier)
were still pending, because the directive told it to "keep the question above
visible". These tests pin the replacement contract:

* a question whose fan-out has required lanes *and* a fanout_id is held: the
  directive says "DRAFT — do not show the user" and never "keep ... visible";
* a ``partial`` submission keeps it held and hands back no question;
* only a complete submission returns ``question_release: released`` together
  with the ``releasable_question`` text;
* questions with no required lanes, or with no fan-out id to redeem, are not
  held (holding them would hold them forever).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ouroboros.mcp.tools.advisory_dispatch import (
    append_question_advisory_dispatch,
    split_appended_dispatch,
)
from ouroboros.mcp.tools.fanout import (
    QUESTION_RELEASE_HELD,
    QUESTION_RELEASE_RELEASED,
    FanoutRegistry,
    question_release_fields,
    register_question_advisory_fanout,
    submit_fanout_results,
)
from ouroboros.mcp.tools.fanout_handler import SubmitFanoutResultsHandler
from ouroboros.mcp.tools.question_advisory import attach_question_advisory
from ouroboros.mcp.tools.subagent import build_subagent_payload
from ouroboros.orchestrator.disposable_memory import DisposableMemory
from ouroboros.persistence.artifact_store import ArtifactStore

QUESTION = "질문 10. 토큰당 점수의 분모는 입력+출력 토큰 합인가요?"
DRAFT_MARK = "DRAFT — do not show the user"
VISIBLE_MARK = "keep the question above visible"


def _payload(lane_id: str, *, required: bool) -> Any:
    return build_subagent_payload(
        tool_name="ouroboros_interview",
        title=f"advisory:{lane_id}",
        prompt="inspect",
        agent="researcher",
        context={
            "lane_id": lane_id,
            "question_identity": "interview-question:0123456789abcdef",
            "required": required,
        },
    )


def _meta(*, held: bool, fanout_id: str | None = "fanout_abc", required: bool = True) -> dict:
    payloads = [
        _payload("code_context", required=required).to_dict(),
        _payload("web_context", required=False).to_dict(),
    ]
    meta: dict[str, Any] = {
        "question_advisory_host_action": "dispatch_subagents_if_supported",
        "question_advisory_subagents": payloads,
        "question_advisory_result_correlation_key": "context.lane_id",
        "session_id": "interview_x",
    }
    if fanout_id:
        meta["question_advisory_fanout_id"] = fanout_id
    if held:
        meta["question_release"] = QUESTION_RELEASE_HELD
    return meta


# --- directive wording -------------------------------------------------------


def test_held_question_is_marked_draft_not_visible() -> None:
    text = append_question_advisory_dispatch(f"Session interview_x\n\n{QUESTION}", _meta(held=True))
    assert DRAFT_MARK in text
    assert VISIBLE_MARK not in text
    assert "question_release: released" in text
    assert "releasable_question" in text
    # The question text still leads, and the auto driver still cuts the directive off.
    assert text.startswith(f"Session interview_x\n\n{QUESTION}")
    assert split_appended_dispatch(text) == f"Session interview_x\n\n{QUESTION}"


def test_unheld_question_keeps_the_old_visible_wording() -> None:
    text = append_question_advisory_dispatch(QUESTION, _meta(held=False))
    assert VISIBLE_MARK in text
    assert DRAFT_MARK not in text
    assert "Release rule" not in text


# --- attach decides held / not held -----------------------------------------


def _attach(tmp_path: Path, *, registry: bool) -> tuple[dict[str, Any], FanoutRegistry]:
    reg = FanoutRegistry(tmp_path / "fanout")
    meta: dict[str, Any] = {}
    attach_question_advisory(
        meta,
        tool_name="ouroboros_interview",
        session_id="interview_x",
        question=QUESTION,
        code_investigation_request={
            "question": QUESTION,
            "reason": "policy",
            "repository_path": "/repo/api",
        },
        fanout_registry=reg if registry else None,
    )
    return meta, reg


def test_attach_holds_question_with_required_lanes_and_persists_it(tmp_path: Path) -> None:
    meta, reg = _attach(tmp_path, registry=True)
    required = [
        p["context"]["lane_id"]
        for p in meta["question_advisory_subagents"]
        if p["context"].get("required")
    ]
    assert required, "interview advisory is expected to declare required lanes"
    assert meta["question_release"] == QUESTION_RELEASE_HELD
    record = reg.load(meta["question_advisory_fanout_id"])
    assert record is not None
    assert record.synthesizer_input["question"] == QUESTION


def test_attach_without_registry_does_not_hold(tmp_path: Path) -> None:
    """No fanout_id means nothing can redeem the hold, so it must not be set."""
    meta, _ = _attach(tmp_path, registry=False)
    assert "question_advisory_fanout_id" not in meta
    assert "question_release" not in meta


# --- submission releases only when complete ---------------------------------


def _register(tmp_path: Path, *, required: bool = True) -> tuple[FanoutRegistry, str]:
    reg = FanoutRegistry(tmp_path / "fanout")
    fanout_id = register_question_advisory_fanout(
        reg,
        session_id="interview_x",
        payloads=[
            _payload("code_context", required=required),
            _payload("web_context", required=False),
        ],
        question=QUESTION,
    )
    assert fanout_id is not None
    return reg, fanout_id


def test_partial_submission_keeps_question_held(tmp_path: Path) -> None:
    reg, fanout_id = _register(tmp_path)
    reply = submit_fanout_results(
        reg,
        session_id="interview_x",
        correlation_key="context.lane_id",
        fanout_id=fanout_id,
        results=[{"key": "web_context", "content": {"claim": "x"}}],
    )
    assert reply["status"] == "partial"
    assert reply["question_release"] == QUESTION_RELEASE_HELD
    assert "releasable_question" not in reply


def test_complete_submission_releases_the_question(tmp_path: Path) -> None:
    reg, fanout_id = _register(tmp_path)
    reply = submit_fanout_results(
        reg,
        session_id="interview_x",
        correlation_key="context.lane_id",
        fanout_id=fanout_id,
        results=[{"key": "code_context", "content": {"claim": "x"}}],
    )
    assert reply["status"] == "complete"
    assert reply["question_release"] == QUESTION_RELEASE_RELEASED
    assert reply["releasable_question"] == QUESTION


def test_no_required_lanes_reports_no_release_state(tmp_path: Path) -> None:
    reg, fanout_id = _register(tmp_path, required=False)
    record = reg.load(fanout_id)
    assert record is not None
    assert question_release_fields(record, complete=True) == {}
    assert question_release_fields(record, complete=False) == {}


@pytest.mark.asyncio
async def test_handler_reply_carries_releasable_question_only_when_complete(
    tmp_path: Path,
) -> None:
    """The MCP tool path publishes an envelope; the release fields ride on it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = ArtifactStore.for_project(workspace)
    store.initialize()
    reg, fanout_id = _register(tmp_path)
    handler = SubmitFanoutResultsHandler(
        fanout_registry=reg, disposable_memory=DisposableMemory(artifact_store=store)
    )
    base = {
        "session_id": "interview_x",
        "fanout_id": fanout_id,
        "correlation_key": "context.lane_id",
    }

    partial = (
        await handler.handle({**base, "results": [{"key": "web_context", "content": {"c": 1}}]})
    ).unwrap()
    assert partial.meta["status"] == "partial"
    assert partial.meta["question_release"] == QUESTION_RELEASE_HELD
    assert "releasable_question" not in partial.meta

    done = (
        await handler.handle({**base, "results": [{"key": "code_context", "content": {"c": 1}}]})
    ).unwrap()
    assert done.meta["question_release"] == QUESTION_RELEASE_RELEASED
    assert done.meta["releasable_question"] == QUESTION
    assert QUESTION in done.content[0].text


def test_attach_does_not_hold_when_no_lane_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fan-out with only optional lanes has nothing to wait for."""
    import ouroboros.mcp.tools.question_advisory as qa

    real = qa.build_question_advisory_subagents

    def optional_only(request: Any) -> list[Any]:
        payloads = real(request)
        for payload in payloads:
            payload.context["required"] = False
        return payloads

    monkeypatch.setattr(qa, "build_question_advisory_subagents", optional_only)
    meta, _ = _attach(tmp_path, registry=True)
    assert "question_advisory_fanout_id" in meta
    assert "question_release" not in meta


def test_other_fanout_kinds_carry_no_release_fields(tmp_path: Path) -> None:
    from ouroboros.mcp.tools.fanout import FANOUT_KIND_LATERAL_PERSONA_PANEL

    reg = FanoutRegistry(tmp_path / "fanout")
    fanout_id = reg.register(
        kind=FANOUT_KIND_LATERAL_PERSONA_PANEL,
        session_id="s1",
        correlation_key="context.persona",
        expected_keys=["researcher"],
        synthesizer_input={"entries": [], "question": QUESTION},
        required_keys=["researcher"],
    )
    record = reg.load(fanout_id)
    assert record is not None
    assert question_release_fields(record, complete=True) == {}
    assert question_release_fields(record, complete=False) == {}
