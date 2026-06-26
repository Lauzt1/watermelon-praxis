"""Offline tests for recall: the exact-hash fast path and the canonical signature.

The signature LLM call is exercised with a stub so no network/key is needed.
"""
from praxis.recall import exact_hash, signature


class _StubLLM:
    def __init__(self, payload):
        self._payload = payload
        self.llm_calls = 0

    def complete(self, messages, **kwargs):
        self.llm_calls += 1
        return self._payload


def test_exact_hash_is_stable_and_case_insensitive():
    assert exact_hash("Create a BUG") == exact_hash("create a bug")


def test_exact_hash_ignores_surrounding_and_collapsed_whitespace():
    assert exact_hash("  create   a  bug ") == exact_hash("create a bug")


def test_exact_hash_distinguishes_different_instructions():
    assert exact_hash("create a bug") != exact_hash("list all bugs")


def test_signature_has_canonical_shape():
    stub = _StubLLM({"verb": "create", "entity": "issue",
                     "filters": {"priority": "high"}, "artifact": "bug"})
    sig = signature("create a high priority bug issue", llm=stub)
    assert set(sig) == {"verb", "entity", "filters", "artifact"}
    assert sig["verb"] == "create" and sig["entity"] == "issue"
    assert stub.llm_calls == 1


def test_signature_fills_missing_keys():
    # an under-specified model response must still yield the full canonical shape
    stub = _StubLLM({"verb": "list"})
    sig = signature("list the issues", llm=stub)
    assert set(sig) == {"verb", "entity", "filters", "artifact"}
    assert sig["filters"] == {} and sig["artifact"] == "" and sig["entity"] == ""
