"""Stage 112 frontend contracts for Tailor/Cover application convergence."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "tools" / "editor" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_tailor_and_cover_show_the_same_application_lifecycle() -> None:
    tailor = _read("tailor.html")
    cover = _read("cover-letters.html")

    for html in (tailor, cover):
        assert 'class="application-lifecycle"' in html
        for stage in ("job", "source-cv", "review", "cv-letter", "ready"):
            assert f'data-lifecycle-stage="{stage}"' in html
        for label in ("Job", "Source CV", "Review", "CV + letter", "Ready"):
            assert f'data-local-en="{label}"' in html
        assert 'data-local-aria-de="Bewerbungsablauf"' in html

    assert 'data-current-stage="review"' in tailor
    assert 'data-current-stage="cv-letter"' in cover
    assert 'data-lifecycle-stage="ready">\n              <span aria-disabled="true">' in tailor
    assert 'data-lifecycle-stage="ready">\n              <span aria-disabled="true">' in cover


def test_tailor_exposes_explicit_post_review_navigation_with_lineage() -> None:
    html = _read("tailor.html")
    js = _read("tailor.js")

    assert 'id="tl-next-actions"' in html
    assert 'id="tl-continue-cover"' in html
    assert 'id="tl-back-application"' in html
    assert 'data-local-en="Continue to cover letter"' in html
    assert 'data-local-en="Back to application"' in html
    assert 'url.searchParams.set("job_id", jobId)' in js
    assert 'url.searchParams.set("cv_id", cvId)' in js
    assert 'url.searchParams.set("source_cv_id", sourceCvId)' in js
    assert 'contextHref("/cover-letters"' in js
    assert 'renderNextActions(tailored)' in js


def test_cover_honors_cv_and_source_cv_query_context() -> None:
    html = _read("cover-letters.html")
    js = _read("cover-letters.js")

    assert 'id="cl-back-application"' in html
    assert 'data-local-en="Back to application"' in html
    assert 'data-local-de="Zurück zur Bewerbung"' in html
    assert 'data-local-aria-de="Zurück zur Bewerbungsbereitschaft"' in html
    assert 'requestedContextId("source_cv_id")' in js
    assert 'requestedContextId("cv_id") || requestedContextId("source_cv_id")' in js
    assert "state.requestedCvId" in js
    assert 'url.searchParams.set("cv_id", state.cvId)' in js
    assert 'url.searchParams.set("source_cv_id", state.sourceCvId)' in js
    assert 'contextHref("/tailor"' in js
    assert "function hasImmutableReviewEvidence(lineage)" in js
    assert 'lineage.document_role === "cover_letter"' in js
    assert "ownsCvId(lineage.source_cv_id)" in js
    assert "ownsCvId(lineage.tailored_cv_id)" in js
    assert "hasImmutableReviewEvidence(state.packetLineage)" in js
    assert "state.cvId !== sourceCvId" not in js
    assert 'var backApplication = $("#cl-back-application")' in js
    assert 'backApplication.href = contextHref("/jobs"' in js
    assert 'sourceCvId: sourceCvId' in js
    assert 'backApplication.hidden = false' in js
    assert 'backApplication.hidden = true' in js


def test_cover_body_first_editor_persists_the_complete_supported_envelope() -> None:
    html = _read("cover-letters.html")
    js = _read("cover-letters.js")

    assert html.index('id="cl-body"') < html.index('id="cl-envelope"')
    fields = (
        "recipient",
        "company",
        "role",
        "tone",
        "subject",
        "salutation",
        "closing",
        "attachments",
    )
    for field in fields:
        assert f'id="cl-envelope-{field}"' in html
        assert f'data-envelope-field="{field}"' in html
        assert f'"{field}"' in js

    assert "Object.assign(persistedPayload, envelopeValues())" in js
    assert "body: JSON.stringify(requestPayload)" in js
    assert 'data-local-en="Body and letter details save together.' in html
    assert "apply" not in html.split('id="cl-envelope-hint"', 1)[1].split("</p>", 1)[0].lower()


def test_cover_does_not_trigger_application_submission() -> None:
    js = _read("cover-letters.js")

    assert "/api/application" not in js
    assert "/apply" not in js
    assert "manual_open" not in js


def test_narrow_application_studio_has_one_user_scroll_container() -> None:
    css = _read("cover-letters.css")
    narrow = css.split("@media (max-width: 840px)", 1)[1].split("}", 3)
    joined = "}".join(narrow)

    assert ".cl-app" in joined
    assert "height: 100dvh" in joined
    assert "overflow: hidden" in joined
    assert ".cl-main" in joined
    assert "flex: 1 1 auto" in joined
    assert "min-height: 0" in joined
    assert "overflow-x: clip" in joined
    assert "overflow-y: auto" in joined
