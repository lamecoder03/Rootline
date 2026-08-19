# The provider-neutral shape of an LLM turn: what goes in, what comes back, what a tool call is.
# Exists so the investigation loop never sees a vendor's message format - switching providers is
# writing one adapter against this file, not editing the loop that decides what to investigate.
# Deliberately the smallest surface that supports tool use: system prompt, turns, tools, reply.

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    """One callable tool, described the way the JSON Schema world describes it. Both providers
    accept this shape after a rename, which is the whole reason it is the neutral one."""

    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    """A model's request to run a tool. `arguments` is always a parsed dict - OpenAI-compatible
    APIs deliver it as a JSON string and Anthropic as an object, and the loop should not care."""

    id: str
    name: str
    arguments: dict


@dataclass
class ToolResult:
    id: str
    content: str
    is_error: bool = False


@dataclass
class AssistantTurn:
    """One reply. `stop` is normalised to the four outcomes the loop actually branches on, and
    `raw` carries the provider's own representation so the next request can replay this turn
    without lossy round-tripping - Anthropic's thinking blocks in particular must go back
    unchanged, and re-serialising them from neutral fields would corrupt them."""

    text: str = ""
    tool_calls: list = field(default_factory=list)
    stop: str = "end"          # "tool_calls" | "end" | "refusal" | "length"
    raw: Any = None
    input_tokens: int = 0
    output_tokens: int = 0


# --- The conversation, in neutral form --------------------------------------------------
# Three turn kinds are enough for a tool-use loop. Each adapter translates this list into its
# own wire format on every request; nothing here is provider-shaped.

@dataclass
class UserTurn:
    text: str


@dataclass
class AssistantTurnRef:
    """A previous assistant reply being replayed. Holds the AssistantTurn so the adapter can use
    its `raw` if it has one and reconstruct from neutral fields if it does not."""

    turn: AssistantTurn


@dataclass
class ToolResultsTurn:
    results: list


class LLMProvider(ABC):
    """What an adapter must implement. One method, because one method is all a tool-use loop
    needs: hand over the conversation and the tools, get the next assistant turn back."""

    name: str = "unknown"
    model: str = "unknown"

    @abstractmethod
    def chat(self, system, turns, tools=None, max_tokens=4096):
        """Return the next AssistantTurn. `tools=None` means answer without calling anything,
        which is how the loop forces a final write-up once the tool budget is gone."""

    def describe(self):
        return f"{self.name}:{self.model}"
