"""Contracts for the read-only release schema probe."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools" / "editor"))

import schema_compatibility_probe as probe_mod  # noqa: E402


_COLUMN_ARGUMENT_METHODS = frozenset(
    {
        "contained_by",
        "contains",
        "eq",
        "filter",
        "gt",
        "gte",
        "ilike",
        "in_",
        "is_",
        "like",
        "lt",
        "lte",
        "neq",
        "order",
    }
)
_WRITE_METHODS = frozenset({"insert", "update", "upsert"})


def _module_string_bindings(tree: ast.Module) -> dict[str, set[str]]:
    bindings: dict[str, set[str]] = {}
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            bindings[target.id] = {value.value}
    return bindings


def _scope_nodes(scope: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Return one function's nodes without leaking bindings from nested defs."""
    nodes: list[ast.AST] = []
    pending = list(scope.body)
    while pending:
        node = pending.pop()
        nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            pending.append(child)
    return nodes


def _string_values(
    expression: ast.AST | None,
    bindings: dict[str, set[str]],
) -> set[str]:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return {expression.value}
    if isinstance(expression, ast.Name):
        return set(bindings.get(expression.id, set()))
    if isinstance(expression, (ast.List, ast.Set, ast.Tuple)):
        return {
            value
            for item in expression.elts
            for value in _string_values(item, bindings)
        }
    return set()


def _local_string_bindings(
    nodes: list[ast.AST],
    module_bindings: dict[str, set[str]],
) -> dict[str, set[str]]:
    bindings = {name: set(values) for name, values in module_bindings.items()}
    changed = True
    while changed:
        changed = False
        for node in nodes:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                target, value = node.target, node.iter
            if not isinstance(target, ast.Name):
                continue
            values = _string_values(value, bindings)
            before = len(bindings.get(target.id, set()))
            bindings.setdefault(target.id, set()).update(values)
            changed = changed or len(bindings[target.id]) != before
    return bindings


def _relation_for_expression(
    expression: ast.AST | None,
    *,
    strings: dict[str, set[str]],
    relation_bindings: dict[str, set[str]],
    table_helpers: dict[str, set[str]],
) -> set[str]:
    if isinstance(expression, ast.Name):
        return set(relation_bindings.get(expression.id, set()))
    if not isinstance(expression, ast.Call):
        return set()
    if isinstance(expression.func, ast.Name):
        return set(table_helpers.get(expression.func.id, set()))
    if not isinstance(expression.func, ast.Attribute):
        return set()
    if expression.func.attr == "table" and expression.args:
        return _string_values(expression.args[0], strings) & set(
            probe_mod.REQUIRED_RELATIONS
        )
    return _relation_for_expression(
        expression.func.value,
        strings=strings,
        relation_bindings=relation_bindings,
        table_helpers=table_helpers,
    )


def _table_helpers(
    tree: ast.Module,
    strings: dict[str, set[str]],
) -> dict[str, set[str]]:
    helpers: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for function in (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            relations = {
                relation
                for node in _scope_nodes(function)
                if isinstance(node, ast.Return)
                for relation in _relation_for_expression(
                    node.value,
                    strings=strings,
                    relation_bindings={},
                    table_helpers=helpers,
                )
            }
            before = len(helpers.get(function.name, set()))
            helpers.setdefault(function.name, set()).update(relations)
            changed = changed or len(helpers[function.name]) != before
    return helpers


def _relation_bindings(
    nodes: list[ast.AST],
    *,
    strings: dict[str, set[str]],
    table_helpers: dict[str, set[str]],
) -> dict[str, set[str]]:
    bindings: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for node in nodes:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if not isinstance(target, ast.Name):
                continue
            relations = _relation_for_expression(
                value,
                strings=strings,
                relation_bindings=bindings,
                table_helpers=table_helpers,
            )
            before = len(bindings.get(target.id, set()))
            bindings.setdefault(target.id, set()).update(relations)
            changed = changed or len(bindings[target.id]) != before
    return bindings


def _mapping_keys(
    expression: ast.AST | None,
    bindings: dict[str, set[str]],
) -> set[str]:
    if isinstance(expression, ast.Name):
        return set(bindings.get(expression.id, set()))
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name):
        if expression.func.id == "dict" and expression.args:
            return _mapping_keys(expression.args[0], bindings)
    if not isinstance(expression, ast.Dict):
        return set()
    columns: set[str] = set()
    for key, value in zip(expression.keys, expression.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            columns.add(key.value)
        elif key is None:
            columns.update(_mapping_keys(value, bindings))
    return columns


def _mapping_bindings(nodes: list[ast.AST]) -> dict[str, set[str]]:
    bindings: dict[str, set[str]] = {}
    changed = True
    while changed:
        changed = False
        for node in nodes:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if isinstance(target, ast.Name):
                columns = _mapping_keys(value, bindings)
                before = len(bindings.get(target.id, set()))
                bindings.setdefault(target.id, set()).update(columns)
                changed = changed or len(bindings[target.id]) != before
            elif (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and isinstance(target.slice, ast.Constant)
                and isinstance(target.slice.value, str)
            ):
                name = target.value.id
                before = len(bindings.get(name, set()))
                bindings.setdefault(name, set()).add(target.slice.value)
                changed = changed or len(bindings[name]) != before
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and isinstance(node.func.value, ast.Name)
                and node.args
            ):
                name = node.func.value.id
                before = len(bindings.get(name, set()))
                bindings.setdefault(name, set()).update(
                    _mapping_keys(node.args[0], bindings)
                )
                changed = changed or len(bindings[name]) != before
    return bindings


def _selected_columns(raw: str) -> set[str]:
    columns = {part.strip() for part in raw.split(",")}
    return {
        column
        for column in columns
        if column != "*" and column.isidentifier() and column.islower()
    }


def _literal_runtime_relation_columns() -> dict[str, set[str]]:
    columns = {name: set() for name in probe_mod.REQUIRED_RELATIONS}
    for path in (REPO / "tools" / "editor").glob("*.py"):
        if path.name == "schema_compatibility_probe.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_strings = _module_string_bindings(tree)
        helpers = _table_helpers(tree, module_strings)
        for scope in ast.walk(tree):
            if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nodes = _scope_nodes(scope)
            strings = _local_string_bindings(nodes, module_strings)
            relation_bindings = _relation_bindings(
                nodes,
                strings=strings,
                table_helpers=helpers,
            )
            mappings = _mapping_bindings(nodes)
            for node in nodes:
                if not isinstance(node, ast.Call) or not isinstance(
                    node.func, ast.Attribute
                ):
                    continue
                relations = _relation_for_expression(
                    node.func.value,
                    strings=strings,
                    relation_bindings=relation_bindings,
                    table_helpers=helpers,
                )
                if not relations:
                    continue
                method = node.func.attr
                used: set[str] = set()
                if method == "select" and node.args:
                    used.update(
                        column
                        for raw in _string_values(node.args[0], strings)
                        for column in _selected_columns(raw)
                    )
                elif method in _COLUMN_ARGUMENT_METHODS and node.args:
                    used.update(_string_values(node.args[0], strings))
                elif method in _WRITE_METHODS and node.args:
                    used.update(_mapping_keys(node.args[0], mappings))
                if method == "upsert":
                    for keyword in node.keywords:
                        if keyword.arg == "on_conflict":
                            used.update(
                                column.strip()
                                for raw in _string_values(keyword.value, strings)
                                for column in raw.split(",")
                                if column.strip()
                            )
                for relation in relations:
                    columns[relation].update(used)
    return columns


def _openapi_document() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    paths: dict[str, Any] = {}
    for name, arguments in probe_mod.REQUIRED_RPCS.items():
        definition_name = f"rpc.{name}"
        definitions[definition_name] = {
            "type": "object",
            "properties": {argument: {"type": "string"} for argument in arguments},
        }
        paths[f"/rpc/{name}"] = {
            "post": {
                "parameters": [
                    {
                        "in": "body",
                        "schema": {"$ref": f"#/definitions/{definition_name}"},
                    }
                ]
            }
        }
    for name, columns in probe_mod.REQUIRED_RELATIONS.items():
        definitions[name] = {
            "type": "object",
            "properties": {column: {"type": "string"} for column in columns},
        }
    return {"swagger": "2.0", "paths": paths, "definitions": definitions}


def _release_evidence(**overrides: object) -> dict[str, object]:
    evidence: dict[str, object] = {
        "contract_id": probe_mod.SCHEMA_CONTRACT_ID,
        "contract_version": probe_mod.RELEASE_CONTRACT_VERSION,
        "worker_contract_version": 2,
        "worker_contract_ok": True,
        "authenticated_rpc_execute_ok": True,
        "evidence_backed_job_status_ok": True,
        "compatible": True,
    }
    evidence.update(overrides)
    return evidence


class _Response:
    def __init__(self, document: object, *, status: int = 200):
        self.status = status
        self._payload = json.dumps(document).encode("utf-8")

    def getheader(self, name: str) -> str | None:
        if name == "Content-Length":
            return str(len(self._payload))
        return None

    def read(self, limit: int) -> bytes:
        return self._payload[:limit]


class _Connection:
    def __init__(self, responses: _Response | list[_Response]):
        self.responses = (
            list(responses) if isinstance(responses, list) else [responses]
        )
        self.response = self.responses[0]
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def request(
        self,
        method: str,
        path: str,
        *,
        body: object,
        headers: dict[str, str],
    ) -> None:
        self.requests.append(
            {"method": method, "path": path, "body": body, "headers": headers}
        )

    def getresponse(self) -> _Response:
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_contract_descriptor_is_stable_and_content_addressed():
    first = probe_mod.contract_descriptor()
    second = probe_mod.contract_descriptor()

    assert first == second
    assert first.startswith(f"{probe_mod.SCHEMA_CONTRACT_ID}:")
    assert probe_mod._DESCRIPTOR_RE.fullmatch(first)


def test_contract_covers_every_literal_runtime_rpc_call():
    runtime_rpcs: set[str] = set()
    runtime_signatures: dict[str, set[frozenset[str]]] = {}
    for path in (REPO / "tools" / "editor").glob("*.py"):
        if path.name == "schema_compatibility_probe.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "rpc"
                or len(node.args) < 2
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
            ):
                continue
            name = node.args[0].value
            runtime_rpcs.add(name)
            arguments = node.args[1]
            if not isinstance(arguments, ast.Dict):
                continue
            keys = {
                key.value
                for key in arguments.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if len(keys) == len(arguments.keys):
                runtime_signatures.setdefault(name, set()).add(frozenset(keys))

    assert runtime_rpcs
    assert runtime_rpcs | {probe_mod.RELEASE_CONTRACT_RPC} == (
        probe_mod.REQUIRED_RPCS.keys()
    )
    assert runtime_signatures.keys() == runtime_rpcs
    for name, signatures in runtime_signatures.items():
        assert signatures == {probe_mod.REQUIRED_RPCS[name]}


def test_contract_covers_every_literal_runtime_relation_column():
    runtime_columns = _literal_runtime_relation_columns()

    assert any(runtime_columns.values())
    for relation, columns in runtime_columns.items():
        assert columns <= probe_mod.REQUIRED_RELATIONS[relation], (
            relation,
            sorted(columns - probe_mod.REQUIRED_RELATIONS[relation]),
        )

    # Claim state is consumed atomically by RPCs rather than selected directly,
    # but it is still part of the forward-only cleanup worker schema boundary.
    assert {
        "attempt_count",
        "last_attempt_at",
        "claim_token",
        "claimed_at",
        "created_at",
    } <= probe_mod.REQUIRED_RELATIONS["pdf_artifact_cleanup_queue"]


def test_probe_uses_openapi_get_and_read_only_release_contract_rpc():
    connection = _Connection(
        [_Response(_openapi_document()), _Response([_release_evidence()])]
    )

    probe_mod.probe(
        {
            "SUPABASE_URL": "https://project.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-token",
        },
        connection_factory=lambda _parsed: connection,
    )

    assert connection.closed is True
    assert len(connection.requests) == 2
    openapi_request, evidence_request = connection.requests
    assert openapi_request["method"] == "GET"
    assert openapi_request["path"] == "/rest/v1/"
    assert openapi_request["body"] is None
    assert evidence_request["method"] == "POST"
    assert evidence_request["path"] == "/rest/v1/rpc/release_schema_contract"
    assert evidence_request["body"] == "{}"
    for request in connection.requests:
        assert (
            request["headers"]["Authorization"]
            == "Bearer service-role-test-token"
        )
        assert request["headers"]["apikey"] == "service-role-test-token"


@pytest.mark.parametrize("surface", ["rpc", "argument", "relation", "column"])
def test_probe_rejects_every_partial_schema_shape(surface: str):
    document = _openapi_document()
    if surface == "rpc":
        document["paths"].pop("/rpc/finalize_application_apply_action")
    elif surface == "argument":
        definition = document["definitions"][
            "rpc.finalize_application_apply_action"
        ]
        definition["properties"].pop("p_expected")
    elif surface == "relation":
        document["definitions"].pop("pdf_artifact_cleanup_queue")
    else:
        document["definitions"]["pdf_artifacts"]["properties"].pop(
            "generation_id"
        )

    with pytest.raises(probe_mod.SchemaCompatibilityError):
        probe_mod.validate_openapi_contract(document)


def test_probe_rejects_additional_rpc_arguments():
    document = _openapi_document()
    definition = document["definitions"]["rpc.cancel_account_deletion"]
    definition["properties"]["p_unexpected_required_argument"] = {"type": "text"}

    with pytest.raises(probe_mod.SchemaCompatibilityError):
        probe_mod.validate_openapi_contract(document)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_id", "stale-contract"),
        ("contract_version", 2),
        ("worker_contract_version", 3),
        ("worker_contract_ok", False),
        ("authenticated_rpc_execute_ok", False),
        ("evidence_backed_job_status_ok", False),
        ("compatible", False),
    ],
)
def test_probe_rejects_stale_release_contract_evidence(
    field: str, value: object
):
    with pytest.raises(probe_mod.SchemaCompatibilityError):
        probe_mod.validate_release_contract(_release_evidence(**{field: value}))


def test_probe_requires_exact_release_contract_evidence_shape():
    evidence = _release_evidence()
    evidence["unexpected"] = True

    with pytest.raises(probe_mod.SchemaCompatibilityError):
        probe_mod.validate_release_contract(evidence)


def test_probe_accepts_openapi_three_request_bodies_and_schemas():
    swagger = _openapi_document()
    document: dict[str, Any] = {
        "openapi": "3.0.0",
        "paths": {},
        "components": {"schemas": deepcopy(swagger["definitions"])},
    }
    for path, value in swagger["paths"].items():
        reference = value["post"]["parameters"][0]["schema"]["$ref"].replace(
            "#/definitions/", "#/components/schemas/"
        )
        document["paths"][path] = {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {"schema": {"$ref": reference}}
                    }
                }
            }
        }

    probe_mod.validate_openapi_contract(document)


def test_fetch_rejects_redirects_oversized_documents_and_insecure_remote_http():
    redirected = _Connection(_Response({}, status=302))
    with pytest.raises(probe_mod.SchemaCompatibilityError):
        probe_mod.probe(
            {
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-token",
            },
            connection_factory=lambda _parsed: redirected,
        )

    oversized = _Connection(_Response(_openapi_document()))
    oversized.response._payload = b"x" * (probe_mod._MAX_OPENAPI_BYTES + 1)
    with pytest.raises(probe_mod.SchemaCompatibilityError):
        probe_mod.probe(
            {
                "SUPABASE_URL": "https://project.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-token",
            },
            connection_factory=lambda _parsed: oversized,
        )

    # A FRESH connection primed with a perfectly good response: the only reason
    # this can fail is the origin guard itself. Reusing an exhausted connection
    # here would make the assertion pass off an IndexError that the probe
    # launders into the same exception type, so deleting the guard would not
    # turn this test red.
    plaintext = _Connection(_Response(_openapi_document()))
    with pytest.raises(
        probe_mod.SchemaCompatibilityError, match="strict HTTPS origin"
    ):
        probe_mod.probe(
            {
                "SUPABASE_URL": "http://project.supabase.co",
                "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-token",
            },
            connection_factory=lambda _parsed: plaintext,
        )
    # The service-role key must never reach the wire over plaintext HTTP.
    assert plaintext.requests == []

    # Loopback stays exempt so a local Supabase stack is still probeable.
    loopback = _Connection(
        [_Response(_openapi_document()), _Response([_release_evidence()])]
    )
    probe_mod.probe(
        {
            "SUPABASE_URL": "http://127.0.0.1:54321",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-test-token",
        },
        connection_factory=lambda _parsed: loopback,
    )
    assert loopback.requests, "loopback probe should have issued a request"


def test_describe_mode_requires_no_credentials_or_network(capsys):
    assert probe_mod.main(["--describe"]) == 0
    assert capsys.readouterr().out.strip() == probe_mod.contract_descriptor()
