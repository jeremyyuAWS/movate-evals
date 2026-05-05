"""Adapter base class. Every backend conforms to this contract."""
from __future__ import annotations

import abc
from typing import Any

from ..models import AdapterResult


class AgentAdapter(abc.ABC):
    name: str = "base"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    @abc.abstractmethod
    async def run(self, scenario_input: dict[str, Any]) -> AdapterResult: ...

    async def aclose(self) -> None:
        return None


def extract_turns(scenario_input: dict[str, Any]) -> list[str]:
    """Normalize a scenario input into a list of user messages.

    Supports three shapes:
        {"turns": ["msg1", "msg2", ...]}    -- explicit multi-turn
        {"prompt": "single message"}        -- single-turn shorthand
        {"input":  "single message"}        -- legacy single-turn shorthand

    Returns at least one message (empty string if nothing is provided), so
    adapters can always assume `turns[-1]` is well-defined.
    """
    turns = scenario_input.get("turns")
    if isinstance(turns, list) and turns:
        return [str(t) for t in turns]
    single = scenario_input.get("prompt") or scenario_input.get("input") or ""
    return [str(single)]


def is_multi_turn(scenario_input: dict[str, Any]) -> bool:
    """True if the scenario explicitly declares more than one turn."""
    turns = scenario_input.get("turns")
    return isinstance(turns, list) and len(turns) > 1
