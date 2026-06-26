"""OpenRouter LLM client — the single swap point for model ids and the only place
the `openai` SDK is touched. Counts every call so the report can show llm_calls.

Workhorse (synthesis/failure reasoning) is a *reasoning* model: it returns the answer
in `message.content` and chain-of-thought separately. With a small token budget the
reasoning eats it all and `content` comes back None — so we default to a generous
`max_tokens` and read `.content` only, raising a clear error if it is missing.
"""
from __future__ import annotations

import json
from typing import Any

from .config import Config, load

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class LLMError(Exception):
    """Raised when the model returns no usable content (e.g. reasoning consumed the
    whole token budget, leaving message.content == None)."""


class LLM:
    def __init__(self, config: Config | None = None, client: Any | None = None):
        self.config = config or load()
        self.llm_calls = 0
        if client is None:
            from openai import OpenAI  # imported lazily so offline tests never need it

            client = OpenAI(base_url=OPENROUTER_BASE, api_key=self.config.openrouter_key)
        self._client = client

    def complete(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        schema: Any | None = None,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        json_mode: bool = True,
    ) -> Any:
        """One chat completion. Defaults to the planner model; synthesis passes the
        workhorse explicitly. Returns parsed JSON, or a validated pydantic instance
        when `schema` is given."""
        model = model or self.config.model_planner
        self.llm_calls += 1
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content
        if content is None:
            raise LLMError(
                f"model {model} returned no content "
                "(reasoning may have consumed the token budget — raise max_tokens)"
            )
        if not json_mode:
            return content
        data = json.loads(content)
        if schema is not None:
            return schema.model_validate(data)
        return data
