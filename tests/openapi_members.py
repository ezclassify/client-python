"""Read the vendored OpenAPI document the way the response-member tests need it.

The tests that hold the client to the contract's required members take those
members from ``vendor/spec/openapi.yaml`` and from nothing else, so a member
the client forgot to check is found by the test rather than by a list someone
kept in step by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

SPEC_PATH = Path(__file__).resolve().parents[1] / "vendor/spec/openapi.yaml"

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Operation:
    """One declared operation and the schema of its success body."""

    id: str
    method: str
    path: str
    success: dict[str, Any] | None


@dataclass(frozen=True)
class Member:
    """One required member present in a recorded body.

    ``tokens`` locate it from the body's root, as object keys and array
    indices; ``path`` spells the same location in JSONPath, the way the
    client's ``MalformedResponseError.path`` names it. ``nullable`` says
    whether the contract allows the member to be null.
    """

    tokens: tuple[str | int, ...]
    path: str
    nullable: bool

    def parent(self, root: Any) -> dict[str, Any]:
        node = root
        for token in self.tokens[:-1]:
            node = node[token]
        assert isinstance(node, dict)
        return node

    @property
    def name(self) -> str:
        name = self.tokens[-1]
        assert isinstance(name, str)
        return name


def segment(token: str | int) -> str:
    """Spell one JSONPath step: ``[2]``, ``.name``, or ``['odd key']``."""
    if isinstance(token, int):
        return f"[{token}]"
    if _IDENTIFIER.fullmatch(token):
        return f".{token}"
    return "['" + token.replace("\\", "\\\\").replace("'", "\\'") + "']"


class Spec:
    """The vendored contract's operations and component schemas."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.components: dict[str, Any] = document["components"]["schemas"]
        responses: dict[str, Any] = document["components"].get("responses", {})
        self.operations: dict[str, Operation] = {}
        for path, item in document["paths"].items():
            for method, operation in item.items():
                if method not in _HTTP_METHODS:
                    continue
                operation_id = operation["operationId"]
                assert operation_id not in self.operations, operation_id
                success = None
                for status, response in operation["responses"].items():
                    if not status.startswith("2"):
                        continue
                    if "$ref" in response:
                        response = responses[response["$ref"].rsplit("/", 1)[-1]]
                    success = (
                        response.get("content", {})
                        .get("application/json", {})
                        .get("schema")
                    )
                    break
                self.operations[operation_id] = Operation(
                    operation_id, method.upper(), path, success
                )
        assert self.operations, "no operations were read from the vendored contract"

    def operation(self, operation_id: str) -> Operation:
        return self.operations[operation_id]

    def resolve(self, schema: Any) -> Any:
        while isinstance(schema, dict) and "$ref" in schema:
            schema = self.components[schema["$ref"].rsplit("/", 1)[-1]]
        return schema

    def allows_null(self, schema: Any) -> bool:
        schema = self.resolve(schema)
        if not isinstance(schema, dict) or not schema:
            # An empty schema, or none at all, accepts any value.
            return True
        kind = schema.get("type")
        if kind == "null" or (isinstance(kind, list) and "null" in kind):
            return True
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if alternatives:
            return any(self.allows_null(item) for item in alternatives)
        return "const" in schema and schema["const"] is None

    def describes(self, schema: dict[str, Any], value: Any) -> bool:
        """Say whether ``value`` is the alternative ``schema`` names.

        The alternative a value satisfies by name: every member it requires
        is present and every constant it fixes is the one the value carries.
        """
        if not isinstance(value, dict):
            return False
        if any(name not in value for name in schema.get("required", [])):
            return False
        for name, member in schema.get("properties", {}).items():
            member = self.resolve(member)
            if isinstance(member, dict) and "const" in member and name in value:
                if value[name] != member["const"]:
                    return False
        return True

    def required_members(self, schema: Any, value: Any) -> list[Member]:
        """Every required member present in ``value``, at any depth."""
        found: list[Member] = []
        self._collect(schema, value, (), "$", found)
        return found

    def _collect(
        self,
        schema: Any,
        value: Any,
        tokens: tuple[str | int, ...],
        path: str,
        found: list[Member],
    ) -> None:
        schema = self.resolve(schema)
        if not isinstance(schema, dict) or value is None:
            return
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if alternatives:
            for alternative in (self.resolve(item) for item in alternatives):
                if isinstance(alternative, dict) and self.describes(alternative, value):
                    if isinstance(value, dict) or alternative.get("type") != "object":
                        self._collect(alternative, value, tokens, path, found)
                    return
            return
        if isinstance(value, dict):
            properties: dict[str, Any] = schema.get("properties", {})
            for name in schema.get("required", []):
                if name in value:
                    found.append(
                        Member(
                            tokens + (name,),
                            path + segment(name),
                            self.allows_null(properties.get(name)),
                        )
                    )
            additional = schema.get("additionalProperties")
            for name, member in value.items():
                child = properties.get(name)
                if child is None and isinstance(additional, dict):
                    child = additional
                self._collect(
                    child, member, tokens + (name,), path + segment(name), found
                )
        elif isinstance(value, list):
            for index, item in enumerate(value):
                self._collect(
                    schema.get("items"),
                    item,
                    tokens + (index,),
                    path + segment(index),
                    found,
                )


@cache
def vendored_spec() -> Spec:
    """The vendored contract, read once per test session."""
    return Spec(yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8")))
