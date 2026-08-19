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
from .pacing import TokenPacer

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
                 http_client=None, temperature=0.0, max_retries=6, verbose_retries=True,
                 tokens_per_minute=None, reasoning_effort=None):
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
        # gpt-oss models emit reasoning tokens that are billed as OUTPUT and charged against
        # max_tokens, so a turn can exhaust its whole allowance before writing a word. Sent
        # only when set; providers that do not know the field ignore it.
        self.reasoning_effort = reasoning_effort
        # Proactive pacing beats reactive retrying when one request costs most of a window.
        self.pacer = (TokenPacer(tokens_per_minute, verbose=verbose_retries)
                      if tokens_per_minute else None)
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

    # "try again in 12.5s", "try again in 23m49.92s", "try again in 1h3m2s". The earlier pattern
    # only understood the bare-seconds form, so every DAILY-quota advisory - which is always
    # given in minutes - failed to parse and silently fell through to exponential backoff. The
    # 240-second waits that looked like honoured advice were guesses against a 24-minute wall.
    _WAIT = re.compile(
        r"try again in (?:([0-9.]+)h)?(?:([0-9.]+)m)?(?:([0-9.]+)s)?", re.IGNORECASE)

    # Long enough to sit out a daily-quota advisory in ONE sleep. The free tier's 200,000/day is
    # a rolling 24-hour window, not a midnight reset, so tokens return at roughly 139/minute and
    # a stalled request genuinely has to wait tens of minutes. Sleeping the advised time once is
    # honest; six short sleeps that each fail is a livelock wearing a progress bar.
    MAX_BACKOFF = 1_800.0

    def _sleep_for(self, error, attempt):
        """How long the provider says to wait, honoured as given. Guessing is the fallback, not
        the plan: the server knows when the bucket refills and says so in the message body."""
        match = self._WAIT.search(str(error))
        if match and any(match.groups()):
            hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
            return min(hours * 3600 + minutes * 60 + seconds + 1.0, self.MAX_BACKOFF)
        headers = getattr(getattr(error, "response", None), "headers", {}) or {}
        if headers.get("retry-after"):
            try:
                return min(float(headers["retry-after"]) + 1.0, self.MAX_BACKOFF)
            except ValueError:
                pass
        return min(2 ** attempt + random.uniform(0, 5), self.MAX_BACKOFF)

    def chat(self, system, turns, tools=None, max_tokens=4096, require_tool=None):
        request = {
            "model": self.model,
            "messages": self._messages(system, turns),
            "max_tokens": max_tokens,
            "temperature": self.temperature,
        }
        if self.reasoning_effort:
            request["reasoning_effort"] = self.reasoning_effort
        if tools:
            request["tools"] = self._tools(tools)
            # Forcing a named function is how the grader guarantees a structured record instead
            # of prose about the brief. Anthropic spells the same idea {"type": "tool", ...};
            # that difference is the adapter's problem, not the caller's.
            request["tool_choice"] = (
                {"type": "function", "function": {"name": require_tool}} if require_tool
                else "auto"
            )

        # Free tiers meter tokens per minute, and max_tokens counts against that budget before a
        # single token is generated. A 429/413 here is a pacing problem, not a failure, so it is
        # waited out rather than propagated - an investigation that dies mid-way wastes every
        # tool call it already spent.
        from openai import APIStatusError

        # Wait for quota BEFORE spending it. The estimate is prompt characters plus the output
        # cap, which is exactly what the provider bills, so the pacer schedules against the same
        # arithmetic the rate limiter uses.
        estimated = 0
        if self.pacer:
            body = sum(len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or ""))
                       for m in request["messages"])
            body += len(str(request.get("tools") or ""))
            estimated = int(body / 3.3) + max_tokens
            self.pacer.reserve(estimated)

        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.chat.completions.create(**request)
                break
            except APIStatusError as error:
                # "Request too large" is NOT a pacing problem and must fail immediately: the
                # single request exceeds the whole per-minute budget, so no amount of waiting
                # shrinks it. Treating it as retryable turned a 5-second failure into six
                # pointless sleeps before the same error. A 429 with an advised wait is the
                # genuinely retryable case.
                if "too large" in str(error).lower():
                    raise RuntimeError(
                        f"The request itself exceeds the provider's per-request token ceiling, so "
                        f"retrying cannot help. Lower agent.config.MAX_TOKENS or MAX_RESULT_CHARS, "
                        f"or raise LLM_CONTEXT_TOKEN_LIMIT if the account's real limit is higher. "
                        f"Provider said: {error}"
                    ) from error
                retryable = error.status_code in (408, 409, 429, 500, 502, 503, 529)
                if not retryable or attempt == self.max_retries:
                    raise
                delay = self._sleep_for(error, attempt)
                if self.verbose_retries:
                    scope = "DAILY quota" if "(TPD)" in str(error) else "per-minute quota"
                    print(f"      [rate limit] {scope} exhausted - waiting {delay / 60:.1f} min "
                          f"(attempt {attempt + 1}/{self.max_retries})")
                time.sleep(delay)

        choice = response.choices[0]
        message = choice.message

        # Replace the estimate with what was actually billed, so a drifting estimator self-corrects
        # rather than compounding across a twenty-call investigation.
        if self.pacer and response.usage:
            billed = (getattr(response.usage, "prompt_tokens", 0) or 0) + max_tokens
            self.pacer.correct(estimated, billed)

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
