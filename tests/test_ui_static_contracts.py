from __future__ import annotations

import hashlib
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_account_has_single_owned_logout_button_and_no_inline_duplicate():
    html = _read("tools/editor/static/account.html")
    css = _read("tools/editor/static/account.css")
    auth = _read("tools/editor/static/auth.js")
    # Plan 108 stage 2: the topbar logout lives in the serve-time chrome
    # params (server.py), not in the raw page file.
    server = _read("tools/editor/server.py")

    assert 'id="ac-logout"' in server
    assert 'id="ac-mobile-logout"' in html
    assert "ac-logout-inline" not in html
    assert "ac-logout-inline" not in server
    assert "@media (max-width: 840px)" in css
    assert 'document.getElementById("ac-logout")' in auth
    assert 'document.getElementById("ac-logout")) return;' in auth


def test_mobile_signout_uses_auth_api_instead_of_duplicate_button_priority():
    js = _read("tools/editor/static/command-palette.js")

    assert "window.OnePagerAuth.logout()" in js
    assert 'var signout = document.getElementById("logout-btn") || document.getElementById("ac-logout")' in js
    assert 'e.key === "Tab" && isOpen' in js
    assert "visibleFocusable" in js
    assert js.index("window.OnePagerAuth.logout()") < js.index('var signout = document.getElementById("logout-btn")')


def test_app_settings_entry_and_mobile_settings_contract():
    html = _read("tools/editor/static/index.html")
    app = _read("tools/editor/static/app.js")
    command = _read("tools/editor/static/command-palette.js")

    assert 'id="settings-btn"' in html
    assert 'aria-label="Settings & account"' in html
    assert 'title="Settings & account"' in html
    assert 'data-tooltip="Settings & account"' in html
    assert 'data-i18n-attr="aria-label:mobile.settings,title:mobile.settings,data-tooltip:mobile.settings"' in html
    assert 'id="theme-popover" class="popover" hidden role="dialog" aria-label="CV Appearance"' in html
    assert 'window.location.href = "/account#settings";' in app
    assert 'data-mobile-action="settings"' in command
    assert 'data-i18n="mobile.settings"' in command
    assert 't("mobile.settings", "Settings & account")' in command
    assert 'aria-current="page"' in command
    assert 'function settingsHref()' in command
    assert 'rect.bottom > 0 && rect.top < window.innerHeight' in command
    assert 'catalog.hidden = !(window.OnePagerDocumentActions || document.getElementById("cv-switcher"));' in command
    assert 'var catalogBtn = document.getElementById("cv-switcher"); if (catalogBtn) catalogBtn.click();' in command


def test_shared_subpage_chrome_localizes_nav_landmarks_and_settings_alias():
    # Plan 108 stage 2: the chrome markup lives ONLY in the shared fragment;
    # pages carry a serve-time placeholder instead of a copy.
    fragment = _read("tools/editor/static/_chrome.html")
    assert 'data-i18n-attr="aria-label:nav.applications"' in fragment
    assert 'data-i18n-attr="aria-label:nav.account"' in fragment
    for rel in (
        "tools/editor/static/account.html",
        "tools/editor/static/jobs.html",
        "tools/editor/static/cover-letters.html",
        "tools/editor/static/tailor.html",
    ):
        html = _read(rel)
        assert "__CHROME_TOPBAR__" in html
        assert 'class="cl-topbar"' not in html, f"{rel} re-duplicates the shared chrome"
    account = _read("tools/editor/static/account.js")
    assert "function syncSettingsRouteChrome()" in account
    assert 'link.setAttribute("href", "/settings")' in account


def test_auth_uses_local_vendor_and_tab_scoped_session_storage():
    auth = _read("tools/editor/static/auth.js")
    landing = _read("tools/editor/static/landing.js")
    vendor_path = REPO / "tools/editor/static/vendor/supabase-js-2.110.0.js"
    vendor_license_path = REPO / "tools/editor/static/vendor/supabase-js-2.110.0.LICENSE.txt"
    vendor = vendor_path.read_text(encoding="utf-8")
    vendor_license = vendor_license_path.read_text(encoding="utf-8")
    shells = [
        "tools/editor/static/index.html",
        "tools/editor/static/cover-letters.html",
        "tools/editor/static/tailor.html",
        "tools/editor/static/jobs.html",
        "tools/editor/static/account.html",
        "tools/editor/static/landing.html",
    ]

    assert "@supabase/supabase-js@2.110.0" in vendor
    assert hashlib.sha256(vendor_path.read_bytes()).hexdigest() == "fdd149b3183cbc9b80e622a85d8960267a2160cc94d978bc19367f35e4041b87"
    assert "@supabase/supabase-js 2.110.0" in vendor_license
    assert "MIT License" in vendor_license
    assert "Copyright (c) 2020 Supabase" in vendor_license
    for rel in shells:
        html = _read(rel)
        assert '/static/vendor/supabase-js-2.110.0.js' in html
        assert "supabase-js@2" not in html
    assert "storage: window.sessionStorage" in auth
    assert "window.sessionStorage.setItem(LOCAL_DEMO_SESSION_KEY" in auth
    assert "window.localStorage.setItem(LOCAL_DEMO_SESSION_KEY" not in auth
    assert 'emailRedirectTo: window.location.origin + "/app"' in auth
    assert 'redirectTo: window.location.origin + "/app"' in auth
    assert "storage: window.sessionStorage" in landing
    privacy = _read("tools/editor/static/privacy.html")
    assert "/static/auth.js" not in privacy
    assert "/static/landing.js" not in privacy
    assert "supabase-js" not in privacy


def test_document_catalog_modal_has_real_modal_and_safe_routing_contracts():
    html = _read("tools/editor/static/index.html")
    js = _read("tools/editor/static/app.js")

    assert 'id="cv-switcher-pop"' in html
    assert 'aria-modal="true"' in html
    assert 'id="cv-catalog-search"' in html
    assert 'data-i18n-attr="placeholder:catalog.search"' in html
    assert 'class="cv-catalog-filter-group" role="group" aria-label="Document type filters"' in html
    assert 'data-doc-count="all"' in html
    assert 'data-doc-count="cv"' in html
    assert 'data-doc-count="cover_letter"' in html
    catalog_block = html.split('id="cv-switcher-pop"', 1)[1].split('id="doc-actions-popover"', 1)[0]
    assert 'role="tab"' not in catalog_block
    assert "setCatalogModalInert(true)" in js
    assert "setCatalogModalInert(false)" in js
    assert "Promise.allSettled([fetch(\"/api/cvs\"), fetch(\"/api/documents\")])" in js
    assert "docSearchText(doc).indexOf(query) !== -1" in js
    assert "data-doc-count" in js
    assert "catalogSection(doc)" in js
    assert "catalogSearch.addEventListener(\"input\"" in js
    assert "const first = catalogSearch ||" in js
    assert 'url.origin === window.location.origin' in js
    assert 'url.pathname === "/cover-letters"' in js
    assert 'url.pathname === "/app"' in js
    assert "buildDocumentPdf(doc)" in js
    assert "deleteDocumentFromCatalog(doc)" in js
    assert "renameDocumentFromCatalog(doc)" in js
    assert "navigateFromCatalog(`/jobs?id=${encodeURIComponent(doc.job_id)}`)" in js
    assert "function packetLineage(doc)" in js
    assert "row.dataset.packetId = lineage.packet_id" in js
    assert "row.dataset.packetUsable = packetUsable(lineage)" in js
    assert "function normalizeAtsPacket(lineage, row)" in js
    assert "function atsPacketBlockers(lineage, row)" in js
    assert "function isUsableAtsPacket(lineage, row)" in js
    assert "lineage.document_role === \"tailored_cv\"" in js
    assert "packet.job_id !== row.job_id" in js
    assert "packet_prompt_version_stale" in js
    assert "detailPacket.id" in js and "detailPacket.packet_version" in js
    assert "function sidebarPacketState(lineage)" in js
    assert "a.dataset.packetState = sidebarPacketState(lineage)" in js
    assert "while (node && node.parentElement" in js
    assert "sibling !== node && !sibling.contains(pop)" in js
    assert "openPdfUrl(data.url, \"cover-letter.pdf\", win)" in js


def test_jobs_saved_filters_and_provenance_contract():
    html = _read("tools/editor/static/jobs.html")
    js = _read("tools/editor/static/jobs.js")

    assert "jb-search-sidebar" in html
    assert 'Jobs search and catalog filters' in html
    assert "jb-results-panel" in html
    for field in (
        "jb-search-mode",
        "jb-filter-family",
        "jb-filter-country",
        "jb-filter-city",
        "jb-filter-region",
        "jb-filter-provider",
        "jb-filter-verification",
        "jb-filter-remote",
        "jb-filter-seniority",
        "jb-filter-freshness",
        "jb-filter-min-years",
        "jb-filter-rank-bucket",
    ):
        assert f'id="{field}"' in html
    assert 'class="jb-saved-filters"' in html
    assert "state.savedFilters" in js
    assert "function syncSavedFilters" in js
    assert "function experienceFit" in js
    assert "function yearsLabel" in js
    assert 't("jobs.years"' in js
    assert "job.job_family" in js
    assert "job.source_city" in js
    assert "job.match_reasons" in js
    assert 'value="all" data-i18n="jobs.import.source.all"' in html
    assert "searchMode" in js
    assert "minYears" in js
    assert "rankBucket" in js
    assert "defaults.ranking" in js
    assert "filters.titles" in js
    assert "defaultVisibleTitle" in js
    assert "defaults.source" in js
    assert 'family: cleanFilterValue("#jb-filter-family", 160)' in js
    assert 'search_mode: enumFilterValue("#jb-search-mode", JOB_SEARCH_MODES, "flexible")' in js
    assert 'city: cleanFilterValue("#jb-filter-city", 200)' in js
    assert 'min_years: normalizeMinYears(cleanFilterValue("#jb-filter-min-years", 8))' in js
    assert 'rank_bucket: cleanFilterValue("#jb-filter-rank-bucket", 80)' in js
    assert "requiredYears != null && requiredYears < minYears" in js
    assert 'state.sort === "shortlist_rank"' in js
    assert "function shortlistRank" in js
    assert "priority_rank" in js
    assert "rank_bucket" in js
    assert 'f.freshness === "fresh" && fresh !== "fresh"' in js
    assert 'id="jb-import-freshness"' in html
    assert "function verificationLabel" in js
    assert 't("jobs.verification." + raw' in js
    assert "safeMetaValue" in js
    assert "function renderDetailProvenance(job)" in js
    assert "jb-provenance-grid" in js
    assert "jobs.provenance.source" in js
    assert "source_metadata" in js


def test_jobs_repository_star_and_downstream_job_picker_contracts():
    jobs_html = _read("tools/editor/static/jobs.html")
    jobs = _read("tools/editor/static/jobs.js")
    tailor_html = _read("tools/editor/static/tailor.html")
    tailor = _read("tools/editor/static/tailor.js")
    cover_html = _read("tools/editor/static/cover-letters.html")
    cover = _read("tools/editor/static/cover-letters.js")

    assert 'data-act="star"' in jobs
    assert "function toggleStarredJob" in jobs
    assert "var next = !isJobStarred(job)" in jobs
    assert 'JSON.stringify({ "starred": next })' in jobs
    assert 'job.source_metadata && job.source_metadata.starred === true' in jobs
    assert 'id="jb-star"' in jobs_html
    assert 'class="cl-btn jb-star-btn"' in jobs_html
    assert 'class="jb-downstream-label"' in jobs_html
    assert 'id="jb-tailor-link"' in jobs_html
    assert 'id="jb-cover-link"' in jobs_html
    assert "function syncDownstreamJobLinks" in jobs
    assert '"/tailor?job_id=" + encodeURIComponent(id)' in jobs
    assert '"/cover-letters?job_id=" + encodeURIComponent(id)' in jobs
    assert 'var title = $("#jb-title");' in jobs
    assert "function restoreStatusSelect()" in jobs
    assert "isJobStarred(a) !== isJobStarred(b)" in jobs
    assert "starReqs" in jobs
    assert "if (state.starReqs[jid]) return;" in jobs
    assert 'disabled aria-busy="true"' in jobs
    assert 'star.setAttribute("aria-busy", "true")' in jobs

    assert 'id="tl-job"' in tailor_html
    assert 'data-i18n="tailor.field.saved_job"' in tailor_html
    assert "function loadSavedJobs" in tailor
    assert "function populateJobSelect" in tailor
    assert "state.jobs" in tailor
    assert "state.autoJdJobId === state.jobId" in tailor
    assert '"tl-job"' in tailor.split("function setReviewApplyControlsDisabled", 1)[1].split("function restoreReviewApplyControls", 1)[0]
    assert "function syncJobIdUrl" in tailor
    assert 'url.searchParams.set("job_id", jobId)' in tailor
    assert 'url.searchParams.delete("job_id")' in tailor
    assert "syncJobIdUrl(state.jobId);" in tailor
    assert "populateJobSelect();" in tailor
    assert "$(\"#tl-job\").addEventListener(\"change\"" in tailor
    assert "selectJobId(" in tailor
    assert "return { job_id: state.jobId };" in tailor

    assert 'id="cl-job"' in cover_html
    assert 'data-i18n="cl.field.saved_job"' in cover_html
    assert "function loadSavedJobs" in cover
    assert "function populateJobSelect" in cover
    assert "state.autoJdJobId === state.jobId" in cover
    assert "function selectedJobPayload" in cover
    assert "function syncJobIdUrl" in cover
    assert "generationReq" in cover
    assert "token !== state.generationReq || selectionToken !== state.selectionReq" in cover
    assert "$(\"#cl-job\").addEventListener(\"change\"" in cover
    assert "body.job_id = state.jobId;" in cover
    assert "delete body.jd_text;" in cover


def test_template_modal_is_preview_led_and_accent_visible_in_rendered_css():
    app = _read("tools/editor/static/app.js")
    css = _read("tools/editor/static/style.css")
    render_css = _read("engine/render/css_base.py")
    server = _read("tools/editor/server.py")
    render_preview = _read("tools/editor/render_preview.py")

    assert "function templatePreviewUrl" in app
    assert "const getAccent" in app
    assert "tpl.default_density" in app
    assert "cvModel.density = tpl.default_density" in app
    assert "e.source !== previewFrame.contentWindow" in app
    assert "ArrowRight" in app and "ArrowLeft" in app and "Home" in app and "End" in app
    assert "accent=${encodeURIComponent(getAccent())}" in app
    assert "&thumb=1" in app
    assert "accent_override=accent or None" in server
    assert "thumbnail=thumbnail" in server
    assert "accent_override: str | None = None" in render_preview
    assert "_THUMBNAIL_EMBED_OVERRIDES" in render_preview
    assert 'data-preview-thumb="{str(thumbnail).lower()}"' in render_preview
    assert 'class="template-preview-frame"' in app
    assert 'data-preview-template="' in app
    assert 'data-template-preview-url="' in app
    assert 'fetch(url, { headers: { Accept: "text/html" } })' in app
    assert "frame.srcdoc = html" in app
    assert 'role="radio"' in app
    assert 'role", "radiogroup"' in app
    assert "function templateField" in app
    assert "templates.${field}." in app
    assert 'window.addEventListener("cv:i18n-changed", refreshTemplateI18n)' in app
    assert "template-card-chips" in app
    assert "template-card-meta" not in app.split("function renderTemplateGallery", 1)[1].split("function setTemplateModalInert", 1)[0]
    assert ".template-preview-frame" in css
    assert ".template-card-chips" in css
    assert "grid-template-columns: repeat(auto-fit" in css.split(".template-modal-gallery", 1)[1]

    assert "--accent-rail" in render_css
    assert ".template-sidebar .page::before" in render_css
    assert "background: var(--accent-rail)" in render_css
    assert ".template-minimal .section-title" in render_css
    assert "color: var(--accent)" in render_css.split(".template-minimal .section-title", 1)[1].split("}", 1)[0]


def test_jobs_packet_revalidation_and_review_gate_contract():
    html = _read("tools/editor/static/jobs.html")
    js = _read("tools/editor/static/jobs.js")

    for field in (
        "jb-source-stale-state",
        "jb-source-revalidate",
        "jb-source-revalidation-status",
        "jb-full-jd-gate",
        "jb-full-jd-status",
        "jb-source-full-jd",
        "jb-experience-fit",
        "jb-packet-state",
        "jb-review-lineage",
        "jb-review-before-after",
        "jb-post-tailor-ats",
        "jb-review-years-fit",
        "jb-cover-grounding-status",
        "jb-approve-cover-review",
    ):
        assert f'id="{field}"' in html
    for token in (
        "function renderSourceStaleness",
        '"/source/revalidate"',
        "full_jd_text",
        "requires_full_jd_review",
        "function renderExperienceFitEvidence",
        "ranking_contribution",
        "function packetStateName",
        "function renderPacketState",
        "data-packet-state",
        "packet.status",
        "stale",
        "regenerated",
        "document_links",
        "function renderReviewLineage",
        "source_hash",
        "source_revision",
        "review_id",
        "tailored_hash",
        "jd_hash",
        "cover_letter_review_status",
        "cover_letter_hash",
        "confirm-cover-letter-review",
        "pendingPacket",
        "row.dataset.packetUsable",
        "Stale packet",
        "Archived packet",
        "row.dataset.scoreSystemId",
        't("jobs.packet.packet", "Packet") + version',
        '"/packets/" + encodeURIComponent',
        '"/archive"',
        '"/regenerate"',
    ):
        assert token in js


def test_cover_letters_packet_lineage_contract():
    html = _read("tools/editor/static/cover-letters.html")
    js = _read("tools/editor/static/cover-letters.js")

    for field in (
        "cl-packet-lineage",
        "cl-packet-state",
        "cl-packet-version",
        "cl-packet-id",
        "cl-packet-job",
        "cl-packet-hashes",
        "cl-packet-review",
    ):
        assert f'id="{field}"' in html
    for token in (
        "function renderPacketLineage",
        "packet_lineage",
        "data-packet-state",
        "cover_letter_hash",
        "stale_reasons",
        "cover_letter_review_status",
        "renderPacketLineage(res.body.packet_lineage",
    ):
        assert token in js


def test_frontend_async_mutations_preserve_server_truth_and_request_context():
    jobs = _read("tools/editor/static/jobs.js")
    tailor = _read("tools/editor/static/tailor.js")
    cover = _read("tools/editor/static/cover-letters.js")
    app = _read("tools/editor/static/app.js")

    # Successful manual-open evidence survives only the automatic readiness
    # refresh; explicit refreshes still clear it.
    assert "function loadApplicationReadiness(options)" in jobs
    assert "options.preserveManualOpenLink === true" in jobs
    assert "function readinessMatchesAcceptedHandoff(data, evidence)" in jobs
    assert "acceptedEvidence: {" in jobs

    # An incomplete two-phase packet can be reconstructed from persisted job
    # detail, and status mutations are physically serialized in the UI.
    assert "function pendingCoverReviewFromPackets" in jobs
    assert 'packetStateName(row, job) === "review_applied"' in jobs
    assert "packet_id: packet.id" in jobs
    assert "statusInFlight: null" in jobs
    assert "state.statusInFlight = request" in jobs
    assert 'select.setAttribute("aria-busy", "true")' in jobs
    assert "state.statusInFlight !== request" in jobs

    # Non-2xx JSON cannot replace a previously loaded catalog with a fake empty
    # collection.
    assert "res.status !== 200 || !res.body || !Array.isArray(res.body.jobs)" in jobs
    assert "res.status !== 200 || !res.body || !Array.isArray(res.body.cvs)" in jobs

    # Profile changes invalidate both late ATS responses and already-rendered
    # evidence.
    assert "function invalidateProfileState" in tailor
    assert '$("#tl-profile").addEventListener("change", invalidateProfileState);' in tailor
    profile_invalidation = tailor.split("function invalidateProfileState", 1)[1].split("function compareProfiles", 1)[0]
    for token in ("state.reqToken++", "resetActionControls()", "clearReview()", "clearReportState()"):
        assert token in profile_invalidation

    # Generation output is bound to the dispatched inputs, and all save callers
    # share one queued CAS chain.
    assert "function generationRequestSnapshot" in cover
    assert "JSON.stringify(requestSnapshot.payload)" in cover
    assert "generatedEnvelopeDefaults(res.body, requestSnapshot)" in cover
    assert '$("#cl-tone").addEventListener("change", invalidateGenerationInputs);' in cover
    assert '$("#cl-recipient").addEventListener("input", invalidateGenerationInputs);' in cover
    assert "saveInFlight: null" in cover
    assert "function runSaveQueue" in cover
    assert "if (state.saveInFlight)" in cover
    assert "state.saveRequested = true" in cover

    # Review completion and committed partial-success markers are fail-closed.
    immutable = cover.split("function hasImmutableReviewEvidence", 1)[1].split("function syncLifecycleLinks", 1)[0]
    for token in (
        'packetState(lineage) === "packet_created"',
        'lineage.review_status === "reviewed"',
        'lineage.cover_letter_review_status === "confirmed"',
        "lineage.cover_letter_review_confirmed_at",
    ):
        assert token in immutable
    assert "res.body.document_updated === true" in cover
    assert "res.body.document_deleted === true" in cover
    assert app.count("data.document_deleted === true") >= 2


def test_account_job_profile_defaults_are_sanitized_browser_state():
    html = _read("tools/editor/static/account.html")
    js = _read("tools/editor/static/account.js")

    for field in (
        "ac-job-profile-family",
        "ac-job-profile-titles",
        "ac-job-profile-mode",
        "ac-job-profile-cities",
        "ac-job-profile-min-years",
        "ac-job-profile-ranking",
    ):
        assert f'id="{field}"' in html
    assert 'var JOB_SEARCH_MODES = ["flexible", "strict"];' in js
    assert 'var JOB_SOURCES = ["", "linkedin", "manual", "all"];' in js
    assert 'value="" data-i18n="account.job_sources.source.none"' in html
    assert "function splitList" in js
    assert "function normalizeMinYears" in js
    assert "function parseRanking" in js
    assert "profileSecretShaped(part) || profileSecretShaped(rawKey)" in js
    assert 'schema: "onepager.job_profile.v1"' in js
    assert 'id="ac-export-job-profile"' in html
    assert 'id="ac-import-job-profile"' in html
    assert 'type="file"' not in html.split('id="job-sources"', 1)[1].split("<!-- Profile", 1)[0]
    assert "function exportJobProfile" in js
    assert "compactJobProfile(collectJobSourceDefaults())" in js
    assert "function normalizeJobProfile" in js
    assert "if (!JOB_PROFILE_FIELDS[key]) return;" in js
    assert "function profileSecretShaped" in js
    for token in ("refresh", "id", "api", "password", "secret"):
        assert token in js
    assert r"token\s*" in js
    assert "authorization\\s*[:=]" in js
    assert "bearer\\s+" in js
    assert "cookie\\s*[:=]" in js
    assert "secret\\s*[:=]" in js
    assert "key\\s*[:=]" in js
    assert "secret[_-]?key" in js
    assert "cleanProfileText" in js
    assert "profileSecretShaped(item)" in js
    assert "safeLocalSet(JOB_SOURCE_DEFAULTS_KEY, JSON.stringify(payload));" in js
    assert "function importJobProfileText" in js
    assert "compactJobProfile(normalizeJobProfile(parsed))" in js
    assert "onepager-job-profile.json" in js
    assert "JOB_PROFILE_SCHEMA" in js
    jobs = _read("tools/editor/static/jobs.js")
    assert "function normalizeJobProfile" in jobs
    assert "function profileSecretShaped" in jobs
    assert "key\\s*[:=]" in jobs
    assert "secret[_-]?key" in jobs
    assert "function cleanProfileText" in jobs
    assert "function splitProfileList" in jobs
    assert "safeLocalSet(JOB_SOURCE_DEFAULTS_KEY, compactJson)" in jobs
    assert "profileSecretShaped(item)" in jobs
    assert "profileSecretShaped(safeKey)" in jobs
    assert "sanitizeRanking(defaults.ranking)" in jobs
    assert "jobDefaultsApplied" in jobs
    assert "if (state.jobDefaultsApplied) return;" in jobs
    assert "cleanFilterValue(\"#jb-import-title\", 300)" in jobs
    assert "enumFilterValue(\"#jb-import-source\", JOB_SOURCES, \"\")" in jobs
    assert 'role="button"' not in jobs
    assert '" tabindex="0"' not in jobs
    assert "jb-card-detail" in jobs


def test_tailor_preserves_linked_job_context_and_profile_defaults():
    app = _read("tools/editor/static/app.js")
    html = _read("tools/editor/static/tailor.html")
    tailor = _read("tools/editor/static/tailor.js")

    assert "function updateTailorLinks" in app
    assert 'requestedContextId("job_id")' in app
    assert 'requestedContextId("source_cv_id")' in app
    assert "const contextJobId = (activeJob && activeJob.id) || applicationContextJobId" in app
    assert 'params.set("job_id", contextJobId)' in app
    assert 'params.set("source_cv_id", sourceCvId)' in app
    assert 'params.set("cv_id", activeCvId)' in app
    assert 'href === "/tailor"' in app
    assert 'id="tl-selected-job-context"' in html
    assert 'id="tl-review-lineage"' in html
    assert "function renderSelectedJobContext(job)" in tailor
    assert "function renderReviewLineage(review)" in tailor
    assert "opt.dataset.jobId = cv.job_id" in tailor
    assert "function loadLinkedJobDescription" in tailor
    assert "jobReq" in tailor
    assert "var jid = state.jobId" in tailor
    assert 'fetch("/api/jobs/" + encodeURIComponent(jid))' in tailor
    assert "token !== state.jobReq || state.jobId !== jid" in tailor
    assert "jd.value && jd.value.trim() && state.autoJdJobId === jid" in tailor
    assert "state.autoJdJobId = jid" in tailor
    assert "state.autoJdText = job.jd_text" in tailor
    assert "jdDirty" in tailor
    # Plan 106 rewrote the payload rule: {job_id} is sent only while the
    # auto-loaded JD is attached and untouched; any manual edit falls back to
    # raw text. (The old "if (state.jdDirty) return payload;" contract line
    # was stale against the shipped 106 source — plan 107, F18.)
    assert "state.jobId && state.autoJdJobId === state.jobId && state.autoJdText && !state.jdDirty" in tailor
    assert "state.jdDirty = true" in tailor
    assert "state.jobReq++" in tailor
    assert "state.jdDirty = false" in tailor
    assert "var previousJobId = state.jobId" in tailor
    assert "state.jobId && state.jobId !== previousJobId" in tailor
    assert "state.autoJdJobId = null" in tailor
    assert "state.autoJdText = \"\"" in tailor
    assert "applyReviewInFlight" in tailor
    assert "setReviewApplyControlsDisabled(true)" in tailor
    assert "function t(key, fallback)" in tailor
    assert 't("tailor.apply.busy"' in tailor
    assert 't("tailor.apply.copy"' in tailor
    assert 't("tailor.score.button"' in tailor
    assert 't("tailor.review.button"' in tailor
    assert 't("tailor.profile.systems"' in tailor
    assert 'data-i18n="tailor.form.heading"' in html
    assert 'data-i18n="tailor.gaps.heading"' in html
    for locked_id in ("tl-cv", "tl-profile", "tl-jd", "tl-jd-url"):
        assert f'"{locked_id}"' in tailor
    assert "function restoreReviewApplyControls" in tailor
    assert "renderSelectedJobContext(job)" in tailor
    assert "review.job_id || state.jobId" not in tailor
    assert 't("tailor.lineage.job", "Job") + " " + (review.job_id || "")' in tailor
    assert "function selectedJobPayload" in tailor
    assert "stored || defaultProfile" in tailor
    assert "window.localStorage.setItem(ATS_PROFILE_KEY, sel.value);" in tailor
    assert "cover_letter_review_confirmed" not in tailor
    assert "function invalidateJobDraftState" in tailor
    assert '("#tl-jd").addEventListener("input", function () { invalidateJobDraftState(); })' in tailor
    assert '("#tl-jd-url").addEventListener("input", function () { invalidateJobDraftState({ clearAutoJdText: true }); })' in tailor
    assert "if (options.clearAutoJdText)" in tailor


def test_cover_letter_job_context_is_hydrated_and_generation_fails_closed():
    cover = _read("tools/editor/static/cover-letters.js")

    assert "var jobChanged = nextJobId !== state.jobId" in cover
    assert "if (jobChanged && state.jobId) loadSelectedJobDescription()" in cover
    assert "linkedJobId !== state.jobId) selectJobId(linkedJobId)" in cover
    assert "state.autoJdJobId === state.jobId && state.autoJdText && !state.jdDirty" in cover
    assert 't("cl.feedback.job_not_ready"' in cover


def test_workflow_browser_helpers_do_not_wait_for_unreachable_network_idle():
    workflow = _read("tests/e2e/test_workflow_contracts.py")
    layout_helpers = _read("tests/e2e/layout_helpers.py")

    assert 'wait_for_load_state("networkidle")' not in workflow
    assert "wait_for_network_settle(page)" in workflow
    assert 'headers: { Authorization: "Bearer " + token }' in layout_helpers
    assert "status = page.evaluate(" in layout_helpers


def test_command_palette_indexes_aliases_and_subpage_actions():
    command = _read("tools/editor/static/command-palette.js")
    jobs = _read("tools/editor/static/jobs.js")
    cover = _read("tools/editor/static/cover-letters.js")
    tailor = _read("tools/editor/static/tailor.js")

    assert 'aliases: ["tailor", "tailoring", "ats", "score", "review", "prüfung"]' in command
    assert "(action.aliases || []).join(\" \")" in command
    assert "action.labelKey" in command
    assert 'id: "jobs-add"' in jobs
    assert 'id: "jobs-import-preview"' in jobs
    assert 'id: "letters-generate"' in cover
    assert 'id: "letters-preview"' in cover
    assert 'id: "letters-download"' in cover
    assert 'run: togglePreview' in cover
    assert "state.previewWanted" in cover
    assert 'id: "tailor-score"' in tailor
    assert 'id: "tailor-review"' in tailor
    assert 'id: "tailor-optimize"' in tailor
    assert 'id: "tailor-build-pdf"' in tailor
    assert "relabelActionControls()" in tailor
    assert 't("jobs.review.unknown"' in tailor


def test_editor_collapse_controls_are_state_aware_and_storage_resilient():
    app = _read("tools/editor/static/app.js")
    assert "const collapsedMemory = {};" in app
    assert "collapsedMemory[storageKey] = next;" in app
    assert 'if (viewMode !== "form") return;' in app
    assert "btn.hidden = !active;" in app
    assert "available: () => viewMode === \"form\" && collapsibleSections().length > 0 && !allSectionsCollapsed()" in app
    assert "available: () => viewMode === \"form\" && collapsedSummary().collapsed > 0" in app


def test_editor_split_stack_uses_measured_space_not_hard_viewport_breakpoint():
    app = _read("tools/editor/static/app.js")
    assert 'matchMedia("(max-width: 920px)")' not in app
    assert "const stack = total <= Math.max(stackMin, editorMin + previewMin + sepW);" in app


def test_jobs_and_cover_letters_ignore_stale_async_responses():
    jobs = _read("tools/editor/static/jobs.js")
    cover = _read("tools/editor/static/cover-letters.js")
    jobs_html = _read("tools/editor/static/jobs.html")
    jobs_css = _read("tools/editor/static/jobs.css")

    for marker in ("packetReq", "packetActionReq", "sourceReq", "navReq", "importReq", "importPreviewReq", "localSearchReq", "importMutationReq", "applyReviewInFlight", "fullJdDrafts"):
        assert marker in jobs
    assert "token !== state.packetReq || navToken !== state.navReq || state.jobId !== jid" in jobs
    assert "token !== state.sourceReq || navToken !== state.navReq || state.jobId !== jid" in jobs
    assert "navToken === state.navReq && state.jobId === jid" in jobs
    assert "state.packetReview.job_id && state.packetReview.job_id !== jid" in jobs
    assert "function resetJobActionControls" in jobs
    assert "function disableJobActionControls" in jobs
    assert "function setPacketCreationControlsDisabled" in jobs
    assert "importToken !== state.importReq" in jobs
    assert "runId !== state.importRunId" in jobs
    assert "token !== state.packetActionReq || navToken !== state.navReq || state.jobId !== jid" in jobs
    assert "state.fullJdDrafts[state.jobId] = e.target.value" in jobs
    assert "j.id === jid" in jobs
    assert "token !== state.importPreviewReq" in jobs
    assert "token !== state.localSearchReq" in jobs
    assert "token !== state.importMutationReq" in jobs
    assert "function resetImportPreviewState" in jobs
    assert "state.importMutationReq++" in jobs
    for key in (
        "jobs.import.feedback.paste_first",
        "jobs.import.feedback.previewing",
        "jobs.import.feedback.preview_failed",
        "jobs.import.feedback.preview_ready",
        "jobs.import.feedback.network_error",
        "jobs.import.feedback.save_failed",
        "jobs.import.feedback.local_search_failed",
        "jobs.import.feedback.local_preview_failed",
    ):
        assert key in jobs
    assert 'for="jb-source-full-jd"' in jobs_html
    assert 'data-i18n="jobs.source.full_jd_label"' in jobs_html
    assert 'data-i18n="jobs.filter.rank.apply_first"' in jobs_html
    assert ".jb-oneclick-row { display: flex; gap: 10px; align-items: stretch; min-width: 0; flex-wrap: wrap; }" in jobs_css
    assert "flex-wrap: wrap;" in jobs_css.split(".jb-packet-actions", 1)[1].split("}", 1)[0]
    assert 'document.querySelector("#jb-source-cv")' not in jobs
    assert '"#jb-source-cv").addEventListener("change"' in jobs

    assert "previewReq" in cover
    assert "saveReq" in cover
    assert "downloadReq" in cover
    assert "function clearPreviewFrame" in cover
    assert "function togglePreview" in cover
    assert "function syncPreviewToggle" in cover
    assert "function resetGenerateButton" in cover
    assert "var letterId = state.letterId" in cover
    assert "var bodySnapshot = $(\"#cl-body\") ? $(\"#cl-body\").value : \"\";" in cover
    assert "$(\"#cl-body\").value !== bodySnapshot" in cover
    assert "if (state.dirty)" in cover
    assert "if (state.dirty && state.letterId)" in cover
    assert "return saveEdits().then(function (saved)" in cover
    assert "token !== state.saveReq || state.letterId !== letterId" in cover
    assert "token !== state.downloadReq || state.letterId !== letterId" in cover
    assert "function pdfRequestStale" in cover
    assert "guard.token !== state.downloadReq" in cover
    assert "guard.letterId !== state.letterId" in cover
    assert "var saved = await saveEdits();" in cover
    assert "if (pdfRequestStale(guard)) {" in cover
    assert "var pdfTab = reservePdfWindow();" in cover
    assert "function reservePdfWindow()" in cover
    assert "if (state.letterId === doc.id)" in cover
    assert "await downloadPdf();" in cover
    assert 'openPdf("/api/cover-letter/" + encodeURIComponent(doc.id) + "/build", null, { token: token });' in cover
    assert 'openPdf("/api/cover-letter/" + encodeURIComponent(letterId) + "/build", null, { token: token, letterId: letterId }, pdfTab);' in cover
    assert "saveBtn.disabled = false" in cover
    assert "downloadBtn.disabled = false" in cover
    assert "previewBtn.disabled = false" in cover
    assert "$(\"#cl-body\").disabled = false" in cover
    assert "if (res.status === 201) loadLibrary()" in cover
    cover_html = _read("tools/editor/static/cover-letters.html")
    assert 'id="cl-preview-toggle" type="button" class="cl-btn" hidden aria-controls="cl-preview" aria-expanded="false"' in cover_html
    assert 'id="cl-body" class="cl-body" name="cover_letter_body" autocomplete="off" spellcheck="true" disabled' in cover_html

    tailor = _read("tools/editor/static/tailor.js")
    assert "function clearReportState" in tailor
    assert 't("tailor.optimize.button"' in tailor
    assert "clearReportState()" in tailor
    assert "if (state.jobId) return;" not in tailor

    server = _read("tools/editor/server.py")
    assert 'cl_service.delete_document(str(result["tailored_cv_id"]), "cv")' in server
    assert 'tailor_svc.release_review_apply(str(result.get("review_id") or ""))' in server
    assert 'cl_service.delete_document(str(result["id"]), "cover_letter")' in server


def test_document_catalog_limit_rename_and_degraded_fallback_contracts():
    html = _read("tools/editor/static/index.html")
    js = _read("tools/editor/static/app.js")

    assert 'id="cv-catalog-limit-note"' in html
    assert 'input.id = "rename-dialog-input"' in js
    assert 'input.setAttribute("aria-labelledby", labelId)' not in js
    assert "function focusCatalogRow" in js
    assert "let cvOk = false" in js
    assert "let docOk = false" in js
    assert "!cvOk && !docOk" in js


def test_plan107_polish_contracts():
    """Plan 107: pins the review-wave fixes so they can't silently regress.

    F1/F3 saved-job pre-flight + empty hints, F2 URL capability gating,
    F4/F5 thumbnail states + cache, F6 aria/chip parity, F7 star glyph +
    filled state, F9/F16 palette commands.
    """
    app = _read("tools/editor/static/app.js")
    icons = _read("tools/editor/static/icons.js")
    jobs_css = _read("tools/editor/static/jobs.css")
    tailor = _read("tools/editor/static/tailor.js")
    tailor_html = _read("tools/editor/static/tailor.html")
    cover = _read("tools/editor/static/cover-letters.js")
    cover_html = _read("tools/editor/static/cover-letters.html")

    # F5: the gallery only renders (and fetches) while the modal is open, and
    # thumbnail HTML is cached by preview URL with in-flight dedup.
    assert "const templatePreviewCache = new Map();" in app
    assert "templatePreviewCache.set(url, entry)" in app
    assert "window.setTimeout(renderTemplateGallery, 150)" in app
    # F4: visible loading + error states on the preview shell.
    assert 'shell.setAttribute("data-preview-loading", "")' in app
    assert 'shell.setAttribute("data-preview-error", tr("templates.preview_error"' in app
    # F6: the aria description mirrors the visible chips — the divergent
    # "One-page {value}" phrasing is gone.
    assert "one_page_risk_chip" not in app
    # F9/F16: Template + CV Appearance are reachable from the palette.
    assert '"act-template"' in app
    assert '"act-appearance"' in app
    # F7: the star glyph exists and the starred state fills it.
    assert '"star":' in icons
    assert '.jb-card-star[aria-pressed="true"] svg' in jobs_css
    # F1/F3: both saved-job selectors pre-flight missing-full-JD jobs and
    # explain an empty repository.
    for src in (tailor, cover):
        assert "function jobNeedsFullJd(job)" in src
        assert "function blockOnFullJdReview()" in src
        assert "jobs.selector.empty" in src
        assert "jobs.selector.needs_full_jd" in src
    # F2: tailor gates its URL field on the server capability signal.
    assert "function syncUrlFetchCapability()" in tailor
    assert 'id="tl-job-empty"' in tailor_html
    assert 'id="tl-url-disabled"' in tailor_html
    assert 'id="cl-job-empty"' in cover_html
    assert 'id="cl-job-warning"' in cover_html


def test_plan108_topbar_container_ladder_and_ghost_labels():
    """Plan 108 stage 1: the editor topbar collapses on WORKSPACE CONTAINER
    width (one measurement basis — the sidebar steals workspace width without
    changing the viewport), and swappable chrome labels reserve their widest
    translation via an invisible [data-width-ghost] instead of hand-measured
    pixel min-widths. This is the architectural fix for the clipped
    "PDF erstellen" defect class (plans 019/093 lineage)."""
    css = _read("tools/editor/static/style.css")
    html = _read("tools/editor/static/index.html")
    i18n = _read("tools/editor/i18n.py")

    # The viewport-based chrome breakpoints are gone — container-measured now.
    for gone in ("1500px", "1320px", "1240px", "1180px"):
        assert f"@media (max-width: {gone})" not in css, gone
    for tier in ("1380px", "1180px", "1120px", "880px", "820px", "780px", "760px", "600px", "360px"):
        assert f"@container workspace (max-width: {tier})" in css, tier

    # The hand-measured DE width reservations are dead...
    assert "min-width: 118px" not in css
    assert "min-width: 124px" not in css
    # ...replaced by the ghost mechanism.
    assert "content: attr(data-width-ghost)" in css
    assert ".btn-label::after" in css

    # Each ghost is pinned to the German dictionary string so a translation
    # change cannot silently reintroduce label clipping.
    assert '"app.save": {"en": "Save", "de": "Speichern"}' in i18n
    assert '"app.build": {"en": "Build PDF", "de": "PDF erstellen"}' in i18n
    assert '"templates.title": {"en": "Templates", "de": "Vorlagen"}' in i18n
    assert 'data-width-ghost="Speichern"' in html
    assert 'data-width-ghost="PDF erstellen"' in html
    assert 'data-width-ghost="Vorlagen"' in html

    # The sidebar itself stays viewport-measured (it lives OUTSIDE the
    # workspace container) — that split is intentional.
    assert "@media (max-width: 880px)" in css
    assert "@media (max-width: 1100px) { :root { --sidebar-w: 220px; } }" in css


def test_plan108_stage3_token_scales_and_elevation():
    """Plan 108 stage 3: geometry/space scales exist, elevation tokens carry
    the dark-mode hairline ring, focus language is unified, and the subpage
    chrome shares the editor's surface (no grey/white bar drift)."""
    css = _read("tools/editor/static/style.css")
    chrome_css = _read("tools/editor/static/cover-letters.css")

    for token in ("--radius-xs:", "--radius-sm:", "--radius-md:", "--radius-lg:",
                  "--radius-pill:", "--space-1:", "--space-2:", "--space-3:",
                  "--space-4:", "--space-6:", "--space-8:"):
        assert token in css, token

    # Dark elevation reads through a hairline ring (shadows vanish on
    # near-black) — the ring layer must exist in the dark shadow set.
    dark_block = css.split(':root[data-theme="dark"]', 1)[1]
    assert "--shadow-sm:      0 0 0 1px rgba(230, 232, 239, 0.04)" in dark_block
    assert "--shadow-popover: 0 0 0 1px rgba(230, 232, 239, 0.08)" in dark_block

    # Unified focus ring covers links and fields, not just buttons.
    assert "a:focus-visible,\ninput:focus-visible,\nselect:focus-visible,\ntextarea:focus-visible" in css

    # Radii are normalised onto the scale in app chrome CSS.
    for rel in ("tools/editor/static/style.css", "tools/editor/static/cover-letters.css",
                "tools/editor/static/jobs.css", "tools/editor/static/tailor.css",
                "tools/editor/static/account.css"):
        page_css = _read(rel)
        for odd in ("border-radius: 3px", "border-radius: 5px",
                    "border-radius: 7px", "border-radius: 10px"):
            assert odd not in page_css, f"{rel} still has {odd}"

    # One chrome surface: the subpage topbar uses the editor surface token.
    topbar_block = chrome_css.split(".cl-topbar {", 1)[1].split("}", 1)[0]
    assert "background: var(--bg-surface)" in topbar_block


def test_plan108_stage4_journey_wow_contracts():
    """Plan 108 stage 4: page continuity, action-success morphs, theme flip,
    palette recents + match highlighting — all motion reduced-motion-gated,
    all failure paths named (recents degrade to an absent group)."""
    css = _read("tools/editor/static/style.css")
    app = _read("tools/editor/static/app.js")
    palette = _read("tools/editor/static/command-palette.js")
    theme_init = _read("tools/editor/static/theme-init.js")
    html = _read("tools/editor/static/index.html")
    i18n = _read("tools/editor/i18n.py")

    # Cross-document view transitions: CSS-only, feature-detected, motion-gated.
    assert "@view-transition" in css
    assert "navigation: auto" in css
    vt_region = css.split("@view-transition", 1)[0]
    assert vt_region.rstrip().endswith("(prefers-reduced-motion: no-preference) {"), (
        "view transitions must be gated on prefers-reduced-motion"
    )

    # Save/Build success morph: check span ships in markup, JS flashes it,
    # CSS swaps the icon; the pop animation is stilled under reduced motion.
    assert html.count("data-action-success") == 2
    assert "function flashActionSuccess(btn)" in app
    assert 'flashActionSuccess($("#save-btn"))' in app
    assert "if (builtOk) flashActionSuccess(btn);" in app
    assert "#save-btn.is-success [data-action-success]" in css
    assert ".build-btn.is-success [data-action-success]" in css
    assert "success-pop" in css

    # Theme-toggle micro-flip is driven by the shared theme module (one
    # place, every page) and animated only under no-preference.
    assert 'btn.classList.add("is-switching")' in theme_init
    assert "#theme-toggle.is-switching [data-icon-theme]" in css

    # Palette: recent-documents group + match highlighting; fetch failure is
    # a named degradation (group absent, console.debug), never a broken UI.
    assert '"palette.group.recent"' in i18n
    assert '"de": "Zuletzt verwendete Dokumente"' in i18n
    assert 'key: "recent"' in palette
    assert "function refreshRecentDocuments()" in palette
    assert "refreshRecentDocuments();" in palette
    assert "function highlightLabel(label, q)" in palette
    assert 'cmdk recent documents unavailable' in palette
    assert "mark.cmdk-match" in css
