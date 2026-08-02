"""Focused contracts for the Jobs/Application Studio frontend lane."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "tools/editor/static/jobs.html").read_text(encoding="utf-8")
JS = (ROOT / "tools/editor/static/jobs.js").read_text(encoding="utf-8")
CSS = (ROOT / "tools/editor/static/jobs.css").read_text(encoding="utf-8")


def _function_body(name: str, next_name: str) -> str:
    return JS.split(f"function {name}", 1)[1].split(f"function {next_name}", 1)[0]


def test_application_studio_is_collapsed_and_has_one_ordered_flow():
    assert '<details id="jb-add-details" class="jb-add-details">' in HTML
    assert '<details id="jb-add-details" class="jb-add-details" open>' not in HTML

    stages = [
        'data-flow-stage="job"',
        'data-flow-stage="source"',
        'data-flow-stage="review"',
        'data-flow-stage="packet"',
        'data-flow-stage="ready"',
    ]
    assert [HTML.index(stage) for stage in stages] == sorted(HTML.index(stage) for stage in stages)
    assert 'data-jb-copy="build_application">Build application<' in HTML
    assert 'id="jb-review"' in HTML
    review_tag = HTML.split('id="jb-review"', 1)[1].split(">", 1)[0]
    assert "hidden" in review_tag and 'aria-hidden="true"' in review_tag


def test_application_studio_reads_readiness_and_never_auto_opens():
    assert '"/apply/readiness"' in JS
    assert '"/apply/open"' in JS
    assert 'res.status === 202 && body.status === "manual_open_ready"' in JS
    assert "body.submitted === false" in JS
    assert "body.automation_performed === false" in JS

    manual_open = _function_body("confirmManualOpen", "syncDetailStarButton")
    assert "window.open" not in manual_open
    assert 'link.setAttribute("href", openUrl)' in manual_open
    assert "link.hidden = false" in manual_open
    assert "validManualOpenEvidence(res, body, readinessSnapshot, openUrl)" in manual_open
    assert 'invalidAcceptedResponse ? studioText("attempt_invalid")' in manual_open

    for confirmation in (
        "user_confirmed",
        "reviewed_packet_confirmed",
        "profile_answers_complete",
        "legal_answers_confirmed",
        "visa_answers_confirmed",
    ):
        assert f'data-apply-confirmation="{confirmation}"' in HTML


def test_application_studio_preserves_job_and_cv_context():
    assert 'params.get("job_id") || legacyId' in JS
    assert 'params.get("source_cv_id") || params.get("cv_id")' in JS
    assert 'tailor.href += "&cv_id=" + encodeURIComponent(cvId)' in JS
    assert 'cover.href += "&cv_id=" + encodeURIComponent(cvId)' in JS
    assert 'body: JSON.stringify({ source_cv_id: cvId })' in JS
    assert "function linkedDocumentHref(kind, docId, packet)" in JS
    assert 'params.set("job_id", jobId)' in JS
    assert 'params.set("cv_id", cvId)' in JS
    assert 'params.set("source_cv_id", sourceCvId)' in JS
    assert "open.href = linkedDocumentHref(kind, doc.id, packet)" in JS
    assert 'linkedDocumentHref("cover_letter", letterId, packet)' in JS


def test_packet_status_is_evidence_based_not_inferred_from_job_lifecycle():
    packet_state = _function_body("packetStateName", "packetUsableInLinkedRow")
    assert 'status === "applied_external"' not in packet_state
    assert 'job.status === "applied"' not in packet_state


def test_manual_open_contract_is_bound_to_current_readiness_identity_and_target():
    eligibility = _function_body("manualOpenEligibility", "confirmationInputs")
    url_check = _function_body("normalizedManualOpenUrl", "safeManualOpenUrl")
    evidence = _function_body("validManualOpenEvidence", "confirmManualOpen")
    packet_selection = _function_body("activePacketForSelectedSource", "selectedReadinessPacket")
    readiness_load = _function_body("loadApplicationReadiness", "renderApplicationFlow")

    assert 'String(data.job_id || "") !== String(state.jobId || "")' in eligibility
    assert 'data.target.method !== "manual_open"' in eligibility
    assert "data.requires_confirmation !== true" in eligibility
    assert "required.length !== rendered.length" in eligibility
    assert "data.actions.manual_open === true" in eligibility
    assert "data.ready === false && confirmationOnly" in eligibility
    assert "parsed.username || parsed.password || parsed.search || parsed.hash" in url_check
    assert "openUrl !== responseTargetUrl || openUrl !== expectedTargetUrl" in JS
    assert "packet.id === expectedPacket.id" in evidence
    assert "packet.job_id === state.jobId" in evidence
    assert "attempt.job_id === state.jobId" in evidence
    assert 'attempt.action === "manual_open"' in evidence
    assert 'attempt.status === "manual_open_ready"' in evidence
    assert "attempt.submitted === false" in evidence
    assert "attempt.automation_performed === false" in evidence
    assert "reviewedComplete[0] || active[0] || null" in packet_selection
    assert "resetManualConfirmationValues();" in readiness_load


def test_readiness_uses_one_short_live_status_instead_of_announcing_all_evidence():
    assert 'id="jb-readiness-body" class="jb-readiness-body" aria-busy="true"' in HTML
    assert 'id="jb-readiness-summary" class="jb-readiness-summary muted" role="status" aria-live="polite"' in HTML


def test_application_studio_has_local_english_and_german_copy():
    assert "var STUDIO_COPY =" in JS
    assert 'build_application: "Build application"' in JS
    assert 'build_application: "Bewerbung erstellen"' in JS
    assert 'manual_only: "Manual handoff only.' in JS
    assert 'manual_only: "Nur manuelle Übergabe.' in JS
    assert 'attempt_invalid: "Manual-open response did not match' in JS
    assert 'attempt_invalid: "Die Antwort zum manuellen Öffnen stimmt nicht' in JS


def test_mobile_source_cv_control_does_not_inherit_a_vertical_flex_basis():
    assert ".jb-oneclick-row .cl-select { flex: none; min-height: 36px; }" in CSS
