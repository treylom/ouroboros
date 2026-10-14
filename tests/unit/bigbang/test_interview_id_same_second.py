"""Two interviews started in the same second must not share an id.

The default id used to be ``interview_<YYYYmmdd_HHMMSS>``; two callers
sharing a state directory (e.g. two bots on one machine) that started an
interview within the same second got the same id, and the second
``save_state`` silently overwrote the first caller's state file.
"""

import asyncio
from datetime import UTC, datetime
import re
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.bigbang.interview import InterviewEngine

_SERVER_ID_RE = re.compile(r"^interview_[a-f0-9]{16}$")


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return datetime(2026, 10, 14, 17, 11, 31, tzinfo=tz or UTC)


@pytest.mark.asyncio
async def test_same_second_starts_get_distinct_ids_and_files(tmp_path) -> None:
    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path)

    with patch("ouroboros.bigbang.interview.datetime", _FrozenDatetime):
        first = await engine.start_interview("BOT_A context")
        second = await engine.start_interview("BOT_B context")

    assert first.is_ok and second.is_ok
    id_a, id_b = first.value.interview_id, second.value.interview_id
    assert id_a != id_b
    assert _SERVER_ID_RE.fullmatch(id_a)
    assert _SERVER_ID_RE.fullmatch(id_b)

    files = sorted(tmp_path.glob("interview_*.json"))
    assert len(files) == 2
    loaded_a = await engine.load_state(id_a)
    loaded_b = await engine.load_state(id_b)
    assert loaded_a.value.initial_context == "BOT_A context"
    assert loaded_b.value.initial_context == "BOT_B context"


@pytest.mark.asyncio
async def test_list_interviews_still_orders_by_updated_at(tmp_path) -> None:
    engine = InterviewEngine(llm_adapter=MagicMock(), state_dir=tmp_path)
    older = (await engine.start_interview("older")).value
    await asyncio.sleep(0.002)  # distinct updated_at; ids no longer carry time
    newer = (await engine.start_interview("newer")).value

    listed = await engine.list_interviews()

    assert [row["interview_id"] for row in listed] == [
        newer.interview_id,
        older.interview_id,
    ]
