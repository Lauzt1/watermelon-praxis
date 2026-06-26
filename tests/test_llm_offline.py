"""Offline tests for the LLM client: call counting, JSON parsing, schema validation,
and the reasoning-model gotcha (content=None when reasoning eats the token budget).

A fake OpenAI-shaped client is injected so no network or API key is needed.
"""
import pytest
from pydantic import BaseModel

from praxis.config import Config
from praxis.llm import LLM, LLMError


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Completion:
    def __init__(self, content):
        self.choices = [_Choice(content)]


class _Completions:
    def __init__(self, content):
        self._content = content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Completion(self._content)


class FakeOpenAI:
    """Mimics the openai SDK surface used by LLM: client.chat.completions.create(...)."""

    def __init__(self, content):
        self.chat = type("C", (), {"completions": _Completions(content)})()


class _Sig(BaseModel):
    verb: str
    entity: str


def _llm(content):
    return LLM(config=Config(), client=FakeOpenAI(content))


def test_complete_parses_json_and_counts_calls():
    llm = _llm('{"verb": "create", "entity": "issue"}')
    out = llm.complete([{"role": "user", "content": "make a bug"}])
    assert out == {"verb": "create", "entity": "issue"}
    assert llm.llm_calls == 1


def test_complete_validates_against_schema():
    llm = _llm('{"verb": "create", "entity": "issue"}')
    out = llm.complete([{"role": "user", "content": "x"}], schema=_Sig)
    assert isinstance(out, _Sig)
    assert out.verb == "create" and out.entity == "issue"


def test_complete_defaults_to_planner_model():
    llm = _llm('{"verb": "v", "entity": "e"}')
    llm.complete([{"role": "user", "content": "x"}])
    sent = llm._client.chat.completions.calls[0]
    assert sent["model"] == llm.config.model_planner


def test_complete_raises_on_none_content():
    # reasoning model returned only chain-of-thought; content is None on every attempt
    llm = _llm(None)
    with pytest.raises(LLMError):
        llm.complete([{"role": "user", "content": "x"}])


class _SeqCompletions:
    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Completion(self._contents.pop(0))


class SeqOpenAI:
    """Returns a different content per call, so the retry path can be exercised offline."""

    def __init__(self, contents):
        self.chat = type("C", (), {"completions": _SeqCompletions(contents)})()


def _seq_llm(contents):
    return LLM(config=Config(), client=SeqOpenAI(contents))


def test_complete_retries_on_none_then_succeeds():
    # a reasoning model that ate its budget once recovers on a retry with more room
    llm = _seq_llm([None, '{"verb": "v", "entity": "e"}'])
    out = llm.complete([{"role": "user", "content": "x"}])
    assert out == {"verb": "v", "entity": "e"}
    assert llm.llm_calls == 2, "the retry is a real call and is counted"
    calls = llm._client.chat.completions.calls
    assert calls[1]["max_tokens"] > calls[0]["max_tokens"], "retry raises the token budget"


def test_complete_retries_on_truncated_json_then_succeeds():
    # the flash planner sometimes truncates its JSON (unterminated string) -> retry recovers
    llm = _seq_llm(['{"verb": "v", "entity":', '{"verb": "v", "entity": "e"}'])
    out = llm.complete([{"role": "user", "content": "x"}])
    assert out == {"verb": "v", "entity": "e"}
    assert llm.llm_calls == 2


def test_complete_raises_after_retries_exhausted():
    llm = _seq_llm([None, None])
    with pytest.raises(LLMError):
        llm.complete([{"role": "user", "content": "x"}])
    assert llm.llm_calls == 2, "exactly two attempts, then give up"
