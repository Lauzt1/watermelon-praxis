"""Offline tests for the GitHub adapter: the pure inverse-derivation logic only.

Live REST/GraphQL calls are exercised by `doctor` and the demo, never mocked here.
`inverse_of()` is a pure function mapping (method, path, body, response) -> InverseOp | None
over the five known mutating shapes; everything else (reads, unknown shapes) returns None.
"""
from praxis.platform.github import inverse_of
from praxis.models import InverseOp


def test_inverse_of_issue_create_is_delete_via_close():
    # GitHub issues can't be hard-deleted; inverse = close (PATCH state=closed)
    resp = {"number": 42}
    inv = inverse_of("rest_post", "/repos/o/r/issues", {}, resp)
    assert isinstance(inv, InverseOp)
    assert inv.method == "rest_patch"
    assert inv.path == "/repos/o/r/issues/42"
    assert inv.body == {"state": "closed"}


def test_inverse_of_add_label_is_delete_label():
    resp = [{"name": "bug"}]
    inv = inverse_of("rest_post", "/repos/o/r/issues/42/labels", {"labels": ["bug"]}, resp)
    assert inv.method == "rest_delete"
    assert inv.path == "/repos/o/r/issues/42/labels/bug"


def test_inverse_of_set_milestone_is_clear_milestone():
    # set milestone = PATCH issue with a milestone number; inverse clears it (null)
    resp = {"number": 42, "milestone": {"number": 5}}
    inv = inverse_of("rest_patch", "/repos/o/r/issues/42", {"milestone": 5}, resp)
    assert isinstance(inv, InverseOp)
    assert inv.method == "rest_patch"
    assert inv.path == "/repos/o/r/issues/42"
    assert inv.body == {"milestone": None}


def test_inverse_of_create_label_is_delete_label():
    resp = {"name": "priority:high"}
    inv = inverse_of("rest_post", "/repos/o/r/labels", {"name": "priority:high", "color": "ededed"}, resp)
    assert inv.method == "rest_delete"
    assert inv.path == "/repos/o/r/labels/priority:high"


def test_inverse_of_create_milestone_is_delete_milestone():
    resp = {"number": 7, "title": "Q3"}
    inv = inverse_of("rest_post", "/repos/o/r/milestones", {"title": "Q3"}, resp)
    assert inv.method == "rest_delete"
    assert inv.path == "/repos/o/r/milestones/7"


def test_inverse_of_read_returns_none():
    assert inverse_of("rest_get", "/repos/o/r/issues", None, [{"number": 1}]) is None


def test_inverse_of_unknown_mutation_returns_none():
    # a PATCH that doesn't touch milestone/state isn't a shape we know how to invert
    assert inverse_of("rest_patch", "/repos/o/r/issues/42", {"title": "edited"}, {"number": 42}) is None
