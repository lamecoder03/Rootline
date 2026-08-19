# Adapter for any OpenAI-compatible chat-completions endpoint. Groq is one instantiation of it.
# Exists as the generic form rather than a Groq-specific file because the endpoint shape is a de
# facto standard - Groq, Together, OpenRouter, Fireworks and a local vLLM all speak it, so this
# one adapter covers the realistic next provider switch as well as the current one.

from __future__ import annotations

import json
import os
import random
import re
import time

from .base import AssistantTurn, AssistantTurnRef, LLMProvider, ToolCall, ToolResultsTurn, UserTurn

# finish_reason -> the four outcomes the loop branches on. Anything unrecognised is treated as a
# completed turn, which is the safe default: the loop stops and writes the brief.
_STOP = {
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",
    "stop": "end",
    "length": "length",
    "content_filter": "refusal",
}


class OpenAICompatProvider(LLMProvider):
    """Talks chat-completions. The whole vendor difference is base_url, model and key name."""

    def __init__(self, model, base_url, api_key_env, name="openai-compatible",
                 http_client=None, temperature=0.0, max_retries=6, verbose_retries=True):
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise SystemExit(
                f"{api_key_env} is not set. Add it to .env - see .env.example."
            )
        from openai import OpenAI

        self.name = name
        self.model = model
        # temperature 0: an investigation that reaches a different conclusion on a re-run is not
        # something an eval can score, and the brief is a factual report, not prose.
        self.temperature = temperature
        self.max_retries = max_retries
        self.verbose_retries = verbose_retries
        kwargs = {"api_key": api_key, "base_url": base_url}
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = OpenAI(**kwargs)

    def _tools(self, tools):
        return [{
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        } for tool in tools]

    def _messages(self, system, turns):
        """Flattens the neutral turn list into chat-completions messages. The system prompt is a
        message here rather than a top-level field, which is the main structural difference from
        Anthropic and exactly the kind of thing the loop should not have to know."""
        messages = [{"role": "system", "content": system}]
        for turn in turns:
            if isinstance(turn, UserTurn):
                messages.append({"role": "user", "content": turn.text})

            elif isinstance(turn, AssistantTurnRef):
                assistant = {"role": "assistant", "content": turn.turn.text or None}
                if turn.turn.tool_calls:
                    assistant["tool_calls"] = [{
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name,
                                     "arguments": json.dumps(call.arguments)},
                    } for call in turn.turn.tool_calls]
                messages.append(assistant)

            elif isinstance(turn, ToolResultsTurn):
                # One message per result, each keyed to its call id. Anthropic batches all
                # results into a single user message; this format does the opposite, and the
                # adapter is where that difference belongs.
                for result in turn.results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": result.id,
                        "content": result.content,
                    })
        return messages

    def _sleep_for(self, error, attempt):
        """Groq's free tier meters tokens per minute and returns the wait in the message body.
        Honouring the number it gives beats a blind exponential backoff, which either wastes a
        minute or retries too early and burns another quota slot."""
        text = str(error)
        match = re.search(r"try again in ([0-9.]+)s", text)
        if match:
            return min(float(match.group(1)) + 0.5, 65.0)
        retry_after = getattr(getattr(error, "response", None), "headers", {}) or {}
        if retry_after.get("retry-after"):
            try:
                return min(float(retry_after["retry-after"]) + 0.5, 65.0)
            except ValueError:
                pass
        return min(2 ** attempt + random.uniform(0, 1), 65.0)

    def chat(self, system, turns, tools=None, max_tokens=4096):
        request = {
            "model": self.model,
            "messages": self._messages(system, turns),
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if tools:
            request["tools"] = self._tools(tools)
            request["tool_choice"] = "auto"

        # Free tiers meter tokens per minute, and max_tokens counts against that budget before a
        # single token is generated. A 429/413 here is a pacing problem, not a failure, so it is
        # waited out rather than propagated - an investigation that dies mid-way wastes every
        # tool call it already spent.
        from openai import APIStatusError

        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(**request)
                break
            except APIStatusError as error:
                retryable = error.status_code in (408, 409, 413, 429, 500, 502, 503, 529)
                if not retryable or attempt == self.max_retries:
                    raise
                delay = self._sleep_for(error, attempt)
                if self.verbose_retries:
                    print(f"      [rate limit] waiting {delay:.0f}s "
                          f"(attempt {attempt + 1}/{self.max_retries})")
                time.sleep(delay)

        choice = response.choices[0]
        message = choice.message

        calls = []
        for call in (message.tool_calls or []):
            raw_args = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                # A malformed argument blob is the model's error, not a crash. Passing it through
                # as a field lets the tool layer reject it and tell the model what went wrong.
                arguments = {"_unparsed_arguments": raw_args}
            calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))

        usage = response.usage
        return AssistantTurn(
            text=(message.content or "").strip(),
            tool_calls=calls,
            # Some OpenAI-compatible servers report "stop" even when they emitted tool calls, so
            # the presence of calls wins over the reported reason.
            stop="tool_calls" if calls else _STOP.get(choice.finish_reason, "end"),
            raw=None,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def groq_provider(model, http_client=None, **kwargs):
    """Groq, which is the current provider because it has a genuinely free tier. Nothing about
    this function is special - it is the generic adapter with three values filled in."""
    return OpenAICompatProvider(
        model=model, base_url=GROQ_BASE_URL, api_key_env="GROQ_API_KEY",
        name="groq", http_client=http_client, **kwargs
    )
