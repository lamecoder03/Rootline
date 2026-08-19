# Adapter for the Anthropic Messages API, kept working alongside the Groq one.
# Exists so the provider pivot is reversible rather than a one-way door: the claim that swapping
# providers costs one adapter is only credible if two adapters actually exist and both run.
# Differences handled here: system is top-level, tool results batch into one user message.

from __future__ import annotations

from .base import AssistantTurn, AssistantTurnRef, LLMProvider, ToolCall, ToolResultsTurn, UserTurn

_STOP = {
    "tool_use": "tool_calls",
    "end_turn": "end",
    "max_tokens": "length",
    "refusal": "refusal",
    "stop_sequence": "end",
}


class AnthropicProvider(LLMProvider):
    def __init__(self, model, http_client=None, thinking=None, effort=None):
        import anthropic

        self.name = "anthropic"
        self.model = model
        self.thinking = thinking
        self.effort = effort
        self.client = anthropic.Anthropic(http_client=http_client) if http_client \
            else anthropic.Anthropic()

    def _tools(self, tools):
        return [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools]

    def _messages(self, turns):
        """Replays assistant turns from `raw` when it is available. That is not an optimisation:
        thinking blocks must go back byte-identical, and rebuilding them from text would drop
        the signature the API checks."""
        messages = []
        for turn in turns:
            if isinstance(turn, UserTurn):
                messages.append({"role": "user", "content": turn.text})

            elif isinstance(turn, AssistantTurnRef):
                if turn.turn.raw is not None:
                    messages.append({"role": "assistant", "content": turn.turn.raw})
                else:
                    blocks = []
                    if turn.turn.text:
                        blocks.append({"type": "text", "text": turn.turn.text})
                    for call in turn.turn.tool_calls:
                        blocks.append({"type": "tool_use", "id": call.id,
                                       "name": call.name, "input": call.arguments})
                    messages.append({"role": "assistant", "content": blocks})

            elif isinstance(turn, ToolResultsTurn):
                # All results in ONE user message - the opposite of the OpenAI shape. Splitting
                # them teaches the model to stop batching parallel calls.
                messages.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": r.id,
                    "content": r.content, "is_error": r.is_error,
                } for r in turn.results]})
        return messages

    def chat(self, system, turns, tools=None, max_tokens=4096, require_tool=None):
        request = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": self._messages(turns),
        }
        if self.thinking:
            request["thinking"] = self.thinking
        if self.effort:
            request["output_config"] = {"effort": self.effort}
        if tools:
            request["tools"] = self._tools(tools)
            if require_tool:
                request["tool_choice"] = {"type": "tool", "name": require_tool}

        response = self.client.messages.create(**request)

        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        calls = [ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
                 for b in response.content if b.type == "tool_use"]

        return AssistantTurn(
            text=text, tool_calls=calls,
            stop=_STOP.get(response.stop_reason, "end"),
            raw=response.content,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
