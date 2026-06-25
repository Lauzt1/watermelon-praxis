"""GitHub adapter — exposes ONLY raw primitives (rest_get/post/patch/delete, graphql).

Nothing compound is hardcoded here; compound operations are synthesised at runtime
(Phase 2). Every mutating primitive derives its inverse via the pure `inverse_of()`
function and, when a journal is attached, records it so the run can be rolled back.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from ..models import InverseOp

# --- path shapes (anchored so /issues/{n}/labels never matches the bare /labels) ---
_ISSUES_CREATE = re.compile(r"^/repos/[^/]+/[^/]+/issues$")
_ISSUE_ITEM = re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+$")
_ISSUE_LABELS = re.compile(r"^/repos/[^/]+/[^/]+/issues/\d+/labels$")
_REPO_LABELS = re.compile(r"^(/repos/[^/]+/[^/]+)/labels$")
_REPO_MILESTONES = re.compile(r"^(/repos/[^/]+/[^/]+)/milestones$")


def inverse_of(
    method: str, path: str, body: dict[str, Any] | None, response: Any
) -> InverseOp | None:
    """Map a mutating (method, path, body, response) to the op that undoes it.

    Pure and exhaustive over the five known mutating shapes; returns None for reads
    and for any shape we don't know how to invert.
    """
    body = body or {}
    resp = response if isinstance(response, dict) else {}

    if method == "rest_post":
        # create issue -> close it (issues can't be hard-deleted)
        if _ISSUES_CREATE.match(path):
            number = resp.get("number")
            if number is None:
                return None
            return InverseOp(method="rest_patch", path=f"{path}/{number}", body={"state": "closed"})

        # add label(s) to an issue -> remove the label
        if _ISSUE_LABELS.match(path):
            labels = body.get("labels") or []
            if not labels:
                return None
            return InverseOp(method="rest_delete", path=f"{path}/{labels[0]}")

        # create a repo label -> delete it
        m = _REPO_LABELS.match(path)
        if m:
            name = body.get("name")
            if name is None:
                return None
            return InverseOp(method="rest_delete", path=f"{m.group(1)}/labels/{name}")

        # create a milestone -> delete it
        m = _REPO_MILESTONES.match(path)
        if m:
            number = resp.get("number")
            if number is None:
                return None
            return InverseOp(method="rest_delete", path=f"{m.group(1)}/milestones/{number}")

    # set an issue's milestone -> clear it
    if method == "rest_patch" and _ISSUE_ITEM.match(path) and "milestone" in body:
        return InverseOp(method="rest_patch", path=path, body={"milestone": None})

    return None


class GitHubError(Exception):
    """Raised on a >=400 response; carries status + body so the executor can detect
    discoverable failures (e.g. a 422 'label must exist') for the learned-rule path."""

    def __init__(self, status_code: int, body: str, op: str = "", path: str = ""):
        self.status_code = status_code
        self.body = body
        self.op = op
        self.path = path
        super().__init__(f"{op} {path} -> HTTP {status_code}: {body}")


class GitHub:
    """Thin httpx client. Counts every API call; records inverses for mutations."""

    REST_BASE = "https://api.github.com"

    def __init__(self, token: str, repo: str, client: httpx.Client | None = None):
        self.token = token
        self.repo = repo  # "owner/name"
        self.api_calls = 0
        # When set to a list, mutating primitives append their inverse to it. The
        # executor swaps this per step so it can persist inverses with the right seq.
        self.journal: list[InverseOp] | None = None
        self._client = client or httpx.Client(
            base_url=self.REST_BASE,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    def _request(self, op: str, http_method: str, path: str, json=None, params=None):
        self.api_calls += 1
        resp = self._client.request(http_method, path, json=json, params=params)
        if resp.status_code >= 400:
            raise GitHubError(resp.status_code, resp.text, op, path)
        data = resp.json() if resp.content else None
        if op in ("rest_post", "rest_patch", "rest_delete") and self.journal is not None:
            inv = inverse_of(op, path, json, data)
            if inv is not None:
                self.journal.append(inv)
        return data

    def rest_get(self, path: str, params: dict | None = None):
        return self._request("rest_get", "GET", path, params=params)

    def rest_post(self, path: str, json: dict | None = None):
        return self._request("rest_post", "POST", path, json=json)

    def rest_patch(self, path: str, json: dict | None = None):
        return self._request("rest_patch", "PATCH", path, json=json)

    def rest_delete(self, path: str, json: dict | None = None):
        return self._request("rest_delete", "DELETE", path, json=json)

    def graphql(self, query: str, variables: dict | None = None):
        self.api_calls += 1
        resp = self._client.post("/graphql", json={"query": query, "variables": variables or {}})
        if resp.status_code >= 400:
            raise GitHubError(resp.status_code, resp.text, "graphql", "/graphql")
        data = resp.json()
        if isinstance(data, dict) and data.get("errors"):
            raise GitHubError(200, str(data["errors"]), "graphql", "/graphql")
        return data

    def close(self):
        self._client.close()
