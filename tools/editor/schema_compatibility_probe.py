"""Read-only production schema compatibility probe.

The deployment workflow runs this module from both the candidate image and a
potential rollback image.  Compatibility requires an identical, versioned
contract descriptor and a successful inspection of PostgREST's role-scoped
OpenAPI document. The probe issues one GET plus one service-role-only, read-only
contract RPC that verifies authenticated grants and the worker contract. It
never writes tenant data.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
from typing import Any, Mapping, Sequence
from urllib.parse import SplitResult, urlsplit


_LOG = logging.getLogger("schema_compatibility_probe")
SCHEMA_CONTRACT_ID = "onepager-20260801170000-v1"
RELEASE_CONTRACT_RPC = "release_schema_contract"
RELEASE_CONTRACT_VERSION = 1
_MAX_OPENAPI_BYTES = 4 * 1024 * 1024
_MAX_EVIDENCE_BYTES = 16 * 1024
_DESCRIPTOR_RE = re.compile(r"[a-z0-9._-]+:[0-9a-f]{64}\Z")

# Every service-role RPC required by the candidate application and its workers.
# Parameter names are part of the contract because PostgREST resolves functions
# by their exposed argument shape.
REQUIRED_RPCS: dict[str, frozenset[str]] = {
    "account_deletion_claim_is_current": frozenset(
        {"p_owner_id", "p_claim_token"}
    ),
    "account_deletion_worker_contract_version": frozenset(),
    "authorize_document_export": frozenset(
        {"p_owner_id", "p_document_id", "p_document_revision"}
    ),
    "claim_accounts_for_deletion": frozenset(
        {"p_batch_size", "p_lease_minutes"}
    ),
    "claim_pdf_artifact_cleanup": frozenset(
        {"p_owner_id", "p_cleanup_id", "p_claim_token", "p_force"}
    ),
    "cancel_account_deletion": frozenset(),
    "complete_pdf_artifact_cleanup": frozenset(
        {"p_owner_id", "p_cleanup_id", "p_claim_token"}
    ),
    "confirm_application_packet_cover_review": frozenset(
        {
            "p_owner_id",
            "p_job_id",
            "p_packet_id",
            "p_cover_letter_hash",
            "p_now",
        }
    ),
    "consume_managed_credit": frozenset({"p_kind"}),
    "delete_document_safely": frozenset(
        {"p_document_id", "p_document_type"}
    ),
    "delete_saved_job_safely": frozenset({"p_job_id"}),
    "detach_owner_cv_pdf_artifacts": frozenset({"p_owner_id"}),
    "detach_pdf_artifacts": frozenset({"p_owner_id", "p_document_id"}),
    "evict_broken_pdf_artifact": frozenset(
        {
            "p_owner_id",
            "p_document_id",
            "p_document_revision",
            "p_artifact_id",
        }
    ),
    "finalize_application_apply_action": frozenset(
        {
            "p_owner_id",
            "p_job_id",
            "p_packet_id",
            "p_action",
            "p_status",
            "p_reason_code",
            "p_target_url",
            "p_target_host",
            "p_target_classification",
            "p_expected",
            "p_readiness",
            "p_confirmations",
            "p_metadata",
        }
    ),
    "insert_document_if_account_active": frozenset(
        {
            "p_type",
            "p_name",
            "p_yaml_blob",
            "p_job_id",
            "p_accent",
            "p_font",
            "p_density",
            "p_summary_company",
            "p_summary_role",
        }
    ),
    "mark_first_cv_seen": frozenset(),
    "merge_incomplete_application_packet": frozenset(
        {"p_owner_id", "p_payload", "p_now"}
    ),
    "publish_pdf_artifact": frozenset(
        {
            "p_owner_id",
            "p_document_id",
            "p_document_revision",
            "p_content_hash",
            "p_generation_id",
            "p_page_count",
            "p_dependency_document_id",
            "p_dependency_revision",
        }
    ),
    "record_pdf_artifact_cleanup_failure": frozenset(
        {"p_owner_id", "p_cleanup_id", "p_claim_token"}
    ),
    "record_byo_usage": frozenset({"p_kind", "p_provider"}),
    RELEASE_CONTRACT_RPC: frozenset(),
    "register_pdf_upload_candidate": frozenset(
        {
            "p_owner_id",
            "p_document_id",
            "p_document_revision",
            "p_content_hash",
            "p_generation_id",
            "p_dependency_document_id",
            "p_dependency_revision",
        }
    ),
    "replace_active_application_packet": frozenset(
        {"p_owner_id", "p_payload", "p_now"}
    ),
    "request_account_deletion_with_artifact_detach": frozenset(),
    "resolve_pdf_artifact": frozenset(
        {
            "p_owner_id",
            "p_document_id",
            "p_document_revision",
            "p_content_hash",
            "p_dependency_document_id",
            "p_dependency_revision",
        }
    ),
    "revalidate_saved_job_source": frozenset(
        {
            "p_owner_id",
            "p_job_id",
            "p_mark_stale",
            "p_full_jd_text",
            "p_jd_json",
            "p_content_hash",
            "p_experience_requirements",
            "p_now",
        }
    ),
    "save_import_result_as_job": frozenset(
        {"p_owner_id", "p_run_id", "p_result_id", "p_job_payload", "p_now"}
    ),
    "seed_first_cv_template": frozenset(
        {"p_name", "p_yaml_blob", "p_accent", "p_font", "p_density"}
    ),
    "set_saved_job_star": frozenset(
        {"p_owner_id", "p_job_id", "p_starred", "p_now"}
    ),
    "update_document_with_artifact_detach": frozenset(
        {
            "p_owner_id",
            "p_document_id",
            "p_document_type",
            "p_expected_revision",
            "p_yaml_blob",
            "p_updated_at",
            "p_accent",
            "p_font",
            "p_density",
            "p_summary_company",
            "p_summary_role",
        }
    ),
}

# Direct read/write surfaces used by the service-role application.  These
# checks catch a partially applied migration even when function names exist.
REQUIRED_RELATIONS: dict[str, frozenset[str]] = {
    "application_apply_attempts": frozenset(
        {
            "id",
            "owner_id",
            "job_id",
            "packet_id",
            "action",
            "status",
            "reason_code",
            "target_url",
            "target_host",
            "target_classification",
            "submitted",
            "automation_performed",
            "readiness",
            "confirmations",
            "metadata",
            "created_at",
            "updated_at",
        }
    ),
    "application_packets": frozenset(
        {
            "id",
            "owner_id",
            "job_id",
            "source_cv_id",
            "tailored_cv_id",
            "cover_letter_id",
            "status",
            "review_status",
            "provider",
            "model",
            "prompt_version",
            "score_system_id",
            "score_snapshot",
            "source_cv_hash",
            "job_content_hash",
            "tailored_cv_hash",
            "cover_letter_hash",
            "input_hash",
            "output_hash",
            "packet_version",
            "profile_answers_complete",
            "legal_answers_confirmed",
            "visa_answers_confirmed",
            "cover_letter_review_status",
            "cover_letter_review_confirmed_at",
            "cover_letter_review_id",
            "archived_at",
            "created_at",
            "updated_at",
        }
    ),
    "documents": frozenset(
        {
            "id",
            "owner_id",
            "type",
            "name",
            "slug",
            "yaml_blob",
            "accent",
            "font",
            "density",
            "summary_company",
            "summary_role",
            "job_id",
            "deleted_at",
            "last_used_at",
            "created_at",
            "updated_at",
        }
    ),
    "job_import_results": frozenset(
        {
            "id",
            "owner_id",
            "import_run_id",
            "source_provider",
            "source_type",
            "source_external_id",
            "title",
            "company",
            "jd_text",
            "jd_json",
            "source_url",
            "source_metadata",
            "source_country",
            "source_region",
            "source_location",
            "remote_mode",
            "salary_text",
            "seniority",
            "verification_status",
            "content_hash",
            "experience_requirements",
            "last_seen_at",
            "expires_at",
            "selected",
            "saved_job_id",
            "created_at",
            "updated_at",
        }
    ),
    "job_import_runs": frozenset(
        {
            "id",
            "owner_id",
            "provider",
            "query",
            "location",
            "status",
            "started_at",
            "finished_at",
            "requested_limit",
            "imported_count",
            "skipped_count",
            "error",
            "metadata",
        }
    ),
    "pdf_artifact_cleanup_queue": frozenset(
        {
            "id",
            "owner_id",
            "document_id",
            "r2_key",
            "content_hash",
            "generation_id",
            "attempt_count",
            "last_attempt_at",
            "not_before",
            "claim_token",
            "claimed_at",
            "created_at",
        }
    ),
    "pdf_artifacts": frozenset(
        {
            "id",
            "owner_id",
            "document_id",
            "r2_key",
            "content_hash",
            "generation_id",
            "page_count",
            "stale",
        }
    ),
    "profiles": frozenset(
        {
            "id",
            "email",
            "display_name",
            "default_language",
            "managed_credits_remaining",
            "deletion_requested_at",
            "preferred_provider",
            "preferred_model",
            "first_cv_seeded_at",
            "updated_at",
        }
    ),
    "saved_jobs": frozenset(
        {
            "id",
            "owner_id",
            "import_run_id",
            "source_provider",
            "source_type",
            "source_external_id",
            "title",
            "company",
            "jd_text",
            "jd_json",
            "source_url",
            "source_metadata",
            "source_country",
            "source_region",
            "source_location",
            "remote_mode",
            "salary_text",
            "seniority",
            "verification_status",
            "content_hash",
            "experience_requirements",
            "last_seen_at",
            "expires_at",
            "status",
            "field",
            "deadline",
            "confidence",
            "why_fit",
            "source_fetched_at",
            "created_at",
            "updated_at",
        }
    ),
    "tailor_review_states": frozenset(
        {
            "id",
            "owner_id",
            "job_id",
            "cv_id",
            "source_hash",
            "jd_hash",
            "tailored_hash",
            "state_digest",
            "state_ciphertext",
            "applied_at",
            "expires_at",
            "created_at",
            "updated_at",
        }
    ),
    "usage_events": frozenset(
        {"id", "owner_id", "kind", "provider", "used_managed_credit"}
    ),
    "user_llm_keys": frozenset(
        {
            "id",
            "owner_id",
            "provider",
            "ciphertext",
            "nonce",
            "wrapped_dek",
            "dek_nonce",
            "last4",
            "created_at",
            "updated_at",
        }
    ),
}


class SchemaCompatibilityError(RuntimeError):
    """The candidate image and current database contract do not match."""


def _contract_manifest() -> dict[str, Any]:
    return {
        "contract_id": SCHEMA_CONTRACT_ID,
        "rpcs": {
            name: sorted(arguments) for name, arguments in sorted(REQUIRED_RPCS.items())
        },
        "relations": {
            name: sorted(columns)
            for name, columns in sorted(REQUIRED_RELATIONS.items())
        },
    }


def contract_descriptor() -> str:
    encoded = json.dumps(
        _contract_manifest(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    descriptor = f"{SCHEMA_CONTRACT_ID}:{hashlib.sha256(encoded).hexdigest()}"
    if not _DESCRIPTOR_RE.fullmatch(descriptor):
        raise SchemaCompatibilityError("invalid schema contract descriptor")
    return descriptor


def _validated_supabase_origin(value: object) -> SplitResult:
    if not isinstance(value, str) or not value.strip():
        raise SchemaCompatibilityError("SUPABASE_URL is required")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise SchemaCompatibilityError("SUPABASE_URL is invalid") from exc
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in {"https", "http"}
        or (parsed.scheme == "http" and not loopback)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise SchemaCompatibilityError("SUPABASE_URL must be a strict HTTPS origin")
    return parsed


def _fetch_openapi(
    environ: Mapping[str, str],
    *,
    connection_factory: Any | None = None,
) -> tuple[dict[str, Any], object]:
    parsed = _validated_supabase_origin(environ.get("SUPABASE_URL"))
    service_key = (environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not service_key or any(char.isspace() for char in service_key):
        raise SchemaCompatibilityError("SUPABASE_SERVICE_ROLE_KEY is required")

    if connection_factory is None:
        connection_type = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(parsed.hostname, parsed.port, timeout=10)
    else:
        connection = connection_factory(parsed)

    def read_json_response(
        response: Any,
        *,
        maximum_bytes: int,
        failure: str,
    ) -> object:
        if response.status != 200:
            raise SchemaCompatibilityError(failure)
        content_length = response.getheader("Content-Length")
        if content_length is not None:
            try:
                parsed_length = int(content_length)
            except ValueError as exc:
                raise SchemaCompatibilityError(failure) from exc
            if not 0 <= parsed_length <= maximum_bytes:
                raise SchemaCompatibilityError(failure)
        payload = response.read(maximum_bytes + 1)
        if len(payload) > maximum_bytes:
            raise SchemaCompatibilityError(failure)
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaCompatibilityError(failure) from exc

    try:
        connection.request(
            "GET",
            "/rest/v1/",
            body=None,
            headers={
                "Accept": "application/openapi+json, application/json",
                "Authorization": f"Bearer {service_key}",
                "apikey": service_key,
                "User-Agent": "onepager-schema-compatibility-probe/1",
            },
        )
        document = read_json_response(
            connection.getresponse(),
            maximum_bytes=_MAX_OPENAPI_BYTES,
            failure="PostgREST schema inspection failed",
        )
        connection.request(
            "POST",
            f"/rest/v1/rpc/{RELEASE_CONTRACT_RPC}",
            body="{}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {service_key}",
                "apikey": service_key,
                "Content-Length": "2",
                "Content-Type": "application/json",
                "User-Agent": "onepager-schema-compatibility-probe/1",
            },
        )
        release_evidence = read_json_response(
            connection.getresponse(),
            maximum_bytes=_MAX_EVIDENCE_BYTES,
            failure="release schema contract inspection failed",
        )
    except SchemaCompatibilityError:
        raise
    except Exception as exc:  # noqa: BLE001 - redact transport/provider details
        raise SchemaCompatibilityError("PostgREST schema inspection failed") from exc
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - connection teardown cannot expose details
            pass
    if not isinstance(document, dict):
        raise SchemaCompatibilityError("PostgREST schema document is invalid")
    return document, release_evidence


def _resolve_reference(document: Mapping[str, Any], value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        return {}
    reference = value.get("$ref")
    if not isinstance(reference, str):
        return value
    if not reference.startswith("#/"):
        return {}
    current: object = document
    for component in reference[2:].split("/"):
        key = component.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or key not in current:
            return {}
        current = current[key]
    return current if isinstance(current, dict) else {}


def _schema_properties(
    document: Mapping[str, Any], schema: object
) -> frozenset[str]:
    resolved = _resolve_reference(document, schema)
    properties: set[str] = set()
    raw_properties = resolved.get("properties")
    if isinstance(raw_properties, dict):
        properties.update(str(name) for name in raw_properties)
    all_of = resolved.get("allOf")
    if isinstance(all_of, list):
        for item in all_of:
            properties.update(_schema_properties(document, item))
    return frozenset(properties)


def _operation_properties(
    document: Mapping[str, Any], operation: Mapping[str, Any]
) -> frozenset[str]:
    properties: set[str] = set()
    parameters = operation.get("parameters")
    if isinstance(parameters, list):
        for parameter in parameters:
            if not isinstance(parameter, dict):
                continue
            properties.update(_schema_properties(document, parameter.get("schema")))
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        content = request_body.get("content")
        if isinstance(content, dict):
            for media in content.values():
                if isinstance(media, dict):
                    properties.update(
                        _schema_properties(document, media.get("schema"))
                    )
    return frozenset(properties)


def _schema_catalog(document: Mapping[str, Any]) -> Mapping[str, Any]:
    definitions = document.get("definitions")
    if isinstance(definitions, dict):
        return definitions
    components = document.get("components")
    if isinstance(components, dict) and isinstance(components.get("schemas"), dict):
        return components["schemas"]
    return {}


def validate_openapi_contract(document: Mapping[str, Any]) -> None:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise SchemaCompatibilityError("PostgREST RPC catalog is missing")
    for name, required_arguments in REQUIRED_RPCS.items():
        path = paths.get(f"/rpc/{name}")
        operation = path.get("post") if isinstance(path, dict) else None
        if not isinstance(operation, dict):
            raise SchemaCompatibilityError(f"required RPC is missing: {name}")
        available_arguments = _operation_properties(document, operation)
        if available_arguments != required_arguments:
            raise SchemaCompatibilityError(f"required RPC signature is stale: {name}")

    schemas = _schema_catalog(document)
    for relation, required_columns in REQUIRED_RELATIONS.items():
        available_columns = _schema_properties(document, schemas.get(relation))
        if not required_columns.issubset(available_columns):
            raise SchemaCompatibilityError(
                f"required relation shape is stale: {relation}"
            )


def validate_release_contract(evidence: object) -> None:
    if isinstance(evidence, list) and len(evidence) == 1:
        evidence = evidence[0]
    required_keys = {
        "contract_id",
        "contract_version",
        "worker_contract_version",
        "worker_contract_ok",
        "authenticated_rpc_execute_ok",
        "evidence_backed_job_status_ok",
        "compatible",
    }
    if not isinstance(evidence, dict) or set(evidence) != required_keys:
        raise SchemaCompatibilityError("release schema contract evidence is invalid")
    if (
        evidence["contract_id"] != SCHEMA_CONTRACT_ID
        or type(evidence["contract_version"]) is not int
        or evidence["contract_version"] != RELEASE_CONTRACT_VERSION
        or type(evidence["worker_contract_version"]) is not int
        or evidence["worker_contract_version"] != 2
        or evidence["worker_contract_ok"] is not True
        or evidence["authenticated_rpc_execute_ok"] is not True
        or evidence["evidence_backed_job_status_ok"] is not True
        or evidence["compatible"] is not True
    ):
        raise SchemaCompatibilityError("release schema contract evidence is stale")


def probe(
    environ: Mapping[str, str] | None = None,
    *,
    connection_factory: Any | None = None,
) -> None:
    env = os.environ if environ is None else environ
    document, release_evidence = _fetch_openapi(
        env, connection_factory=connection_factory
    )
    validate_openapi_contract(document)
    validate_release_contract(release_evidence)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(os.sys.argv[1:] if argv is None else argv)
    if args == ["--describe"]:
        print(contract_descriptor())
        return 0
    if args != ["--probe"]:
        _LOG.critical("schema_compatibility_failed error_type=ArgumentError")
        return 2
    try:
        probe()
    except Exception as exc:  # noqa: BLE001 - logs never include endpoint or key
        _LOG.critical(
            "schema_compatibility_failed error_type=%s",
            type(exc).__name__,
        )
        return 2
    _LOG.info("schema_compatibility_ok contract=%s", SCHEMA_CONTRACT_ID)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
