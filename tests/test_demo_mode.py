"""DEV-ONLY demo-mode tests (the ``ONEPAGER_DEMO_MODE`` flag).

Demo mode is a LOCAL convenience that (1) surfaces a one-click demo-login
button and a gated ``/auth/demo-login`` endpoint, and (2) swaps the real
provider-agnostic LLM client for a deterministic, offline stub that needs no
API key and consumes no managed credits.

The whole feature is gated behind a single env flag that DEFAULTS OFF. These
tests prove BOTH directions:

  * WITH ``ONEPAGER_DEMO_MODE=1``:
      - ``/auth/config`` reports ``demo_mode: true``;
      - ``/auth/demo-login`` is wired + gated and returns a session (the GoTrue
        grant is monkeypatched — no network);
      - ``make_client`` / ``resolve_llm`` yield the ``StubLLMClient`` and bypass
        the key/credit policy;
      - a cover letter generated with the STUB AS THE REAL LLM (nothing mocked
        in the LLM path) PASSES the anti-fabrication + cliché + word-cap guards
        (201, not 422);
      - the tailoring stub passes the anti-fabrication + 1-page guards.

  * WITHOUT the flag (default / production):
      - ``/auth/config`` reports ``demo_mode: false``;
      - ``/auth/demo-login`` → 404 (does not functionally exist);
      - ``make_client`` returns the real ``AnthropicClient`` (not the stub);
      - the AI path still requires a key and 503s with a blank operator key.

The DB-backed routes use the in-memory ``fake_supabase`` fixture from conftest.
The guard pipeline tests MOCK NOTHING in the LLM — the stub IS the LLM.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid as _uuid
from pathlib import Path

import pytest
import yaml as _yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "engine"))
sys.path.insert(0, str(REPO / "tools" / "editor"))

FAKE_OWNER = "00000000-0000-0000-0000-000000000001"

# A canonical CV — the SOLE source of candidate facts the stub may use.
GROUNDED_CV = {
    "name": "Tim Loehr",
    "accent": "#1f6f43",
    "font": "sans",
    "contact": [{"network": "mail", "username": "tim@example.com"}],
    "experience": [
        {
            "role": "Data Engineer",
            "company": "Meta",
            "location": "Warsaw",
            "start": "2023",
            "end": "Present",
            "bullets": ["Cut pipeline latency by 40% across the ETL stack."],
            "stack": "Python, Spark, Airflow",
        },
        {
            "role": "Data Scientist",
            "company": "Red Bull",
            "location": "Salzburg",
            "start": "2021",
            "end": "2023",
            "bullets": ["Built ML models for anomaly detection in Python."],
        },
    ],
    "skills": [{"label": "Data", "items": "Python, SQL, PyTorch, Spark"}],
}

JD_TEXT = (
    "Title: Senior Data Engineer\n"
    "Company: Acme GmbH\n"
    "You must have experience with Spark and Airflow. 5+ years required.\n"
    "Responsibilities: build ETL pipelines and data models."
)


def _seed_cv(store, name="My CV", cv_model=None):
    """Insert a CV document into the fake store; return its id."""
    blob = _yaml.safe_dump(cv_model or GROUNDED_CV, sort_keys=False, allow_unicode=True)
    rows = store.tables.setdefault("documents", [])
    doc_id = str(_uuid.uuid4())
    rows.append(
        {
            "id": doc_id,
            "owner_id": store.owner_id,
            "type": "cv",
            "name": name,
            "yaml_blob": blob,
            "accent": "#1f6f43",
            "font": "sans",
            "deleted_at": None,
            "updated_at": 1,
        }
    )
    return doc_id


# ===========================================================================
#  1. The gating helper — the safety contract (defaults OFF).
# ===========================================================================
class TestDemoModeFlag:
    def test_defaults_off(self, monkeypatch):
        from llm.client import demo_mode_enabled

        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        assert demo_mode_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "Yes", "yes"])
    def test_truthy_values_enable(self, monkeypatch, val):
        from llm.client import demo_mode_enabled

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", val)
        assert demo_mode_enabled() is True

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "nope"])
    def test_falsy_values_stay_off(self, monkeypatch, val):
        from llm.client import demo_mode_enabled

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", val)
        assert demo_mode_enabled() is False

    def test_public_base_url_disables_and_refuses_demo_mode(self, monkeypatch):
        from demo_policy import (
            assert_demo_mode_configuration_safe,
            demo_mode_enabled,
        )

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://cv.example.com")
        assert demo_mode_enabled() is False
        with pytest.raises(RuntimeError, match="non-loopback origin"):
            assert_demo_mode_configuration_safe()

    def test_server_import_refuses_public_demo_configuration(self):
        env = os.environ.copy()
        env["ONEPAGER_DEMO_MODE"] = "1"
        env["PUBLIC_BASE_URL"] = "https://cv.example.com"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys;"
                    "sys.path[:0]=['tools/editor','engine'];"
                    "import server"
                ),
            ],
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0
        assert "Refusing unsafe demo mode" in result.stderr

    @pytest.mark.parametrize(
        ("host", "peer", "allowed"),
        [
            ("127.0.0.1:5567", "127.0.0.1", True),
            ("localhost:5567", "::1", True),
            ("cv.example.com", "127.0.0.1", False),
            ("localhost:5567", "203.0.113.8", False),
        ],
    )
    def test_request_requires_loopback_host_and_peer(self, host, peer, allowed):
        from demo_policy import demo_request_is_loopback

        assert demo_request_is_loopback(host, peer) is allowed

    @pytest.mark.parametrize(
        "header",
        (
            "Forwarded",
            "Via",
            "CF-Connecting-IP",
            "X-Forwarded-For",
            "X-Forwarded-Host",
            "X-Forwarded-Proto",
            "X-Real-IP",
        ),
    )
    def test_forwarding_headers_disqualify_loopback_demo_request(self, header):
        from demo_policy import demo_request_is_loopback

        assert demo_request_is_loopback(
            "localhost:5567",
            "127.0.0.1",
            {header: "for=203.0.113.8"},
        ) is False


class TestDemoReadFallbacks:
    def test_key_status_degrades_to_safe_empty_state(self, server_module, client, bypass_auth, monkeypatch):
        import llm_gateway

        monkeypatch.setattr(llm_gateway, "key_status_all", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        res = client.get("/api/key")

        assert res.status_code == 200
        body = res.get_json()
        assert body["degraded"] is True
        assert body["managed_credits_remaining"] == 0
        assert body["providers"]["anthropic"]["connected"] is False
        assert body["providers"]["gemini"]["connected"] is False

    def test_demo_cv_list_falls_back_to_local_yaml_when_store_unavailable(
        self, server_module, client, bypass_auth, monkeypatch
    ):
        import auth as auth_mod

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setattr(auth_mod, "user_supabase", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        res = client.get("/api/cvs")

        assert res.status_code == 200
        body = res.get_json()
        assert body["count"] == 1
        assert body["cvs"][0]["id"] == server_module.LOCAL_DEMO_CV_ID
        assert body["cvs"][0]["name"].startswith("Local ")

    def test_demo_cv_read_falls_back_to_local_yaml_for_stale_uuid(
        self, server_module, client, bypass_auth, monkeypatch
    ):
        import auth as auth_mod

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setattr(auth_mod, "user_supabase", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        res = client.get("/api/cv?id=623f6855-503f-4374-8db8-299da3ede264")

        assert res.status_code == 200
        assert res.content_type.startswith("text/plain")
        assert "experience:" in res.get_data(as_text=True)

    def test_demo_cv_list_id_round_trips_to_read(
        self, server_module, client, bypass_auth, monkeypatch
    ):
        import auth as auth_mod

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setattr(auth_mod, "user_supabase", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        cv_id = client.get("/api/cvs").get_json()["cvs"][0]["id"]
        res = client.get(f"/api/cv?id={cv_id}")

        assert cv_id == server_module.LOCAL_DEMO_CV_ID
        assert res.status_code == 200
        assert "experience:" in res.get_data(as_text=True)

    def test_demo_cv_list_keeps_real_rows_when_marker_rpc_is_missing(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        cv_id = _seed_cv(fake_supabase, name="Real CV")
        original_rpc = fake_supabase.rpc

        class MissingRpc:
            def execute(self):
                raise RuntimeError("mark_first_cv_seen missing")

        def rpc(name, params=None):
            if name == "mark_first_cv_seen":
                return MissingRpc()
            return original_rpc(name, params)

        monkeypatch.setattr(fake_supabase, "rpc", rpc)

        res = client.get("/api/cvs")

        assert res.status_code == 200
        body = res.get_json()
        assert body["count"] == 1
        assert body["cvs"][0]["id"] == cv_id
        assert body["cvs"][0]["id"] != server_module.LOCAL_DEMO_CV_ID

    def test_demo_cv_read_keeps_real_active_row_when_marker_rpc_is_missing(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        cv_id = _seed_cv(fake_supabase, name="Readable CV")
        original_rpc = fake_supabase.rpc

        class MissingRpc:
            def execute(self):
                raise RuntimeError("mark_first_cv_seen missing")

        def rpc(name, params=None):
            if name == "mark_first_cv_seen":
                return MissingRpc()
            return original_rpc(name, params)

        monkeypatch.setattr(fake_supabase, "rpc", rpc)

        res = client.get("/api/cv")

        assert res.status_code == 200
        assert cv_id != server_module.LOCAL_DEMO_CV_ID
        assert "Tim Loehr" in res.get_data(as_text=True)

    def test_demo_cv_read_keeps_malformed_id_as_400(self, client, bypass_auth, monkeypatch):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")

        res = client.get("/api/cv?id=not-a-uuid")

        assert res.status_code == 400

    def test_demo_cv_read_does_not_fallback_when_deletion_pending(
        self, client, fake_supabase, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": fake_supabase.owner_id, "managed_credits_remaining": 1, "deletion_requested_at": None}
        )
        fake_supabase.tables.setdefault("documents", []).append(
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "owner_id": fake_supabase.owner_id,
                "type": "cv",
                "name": "Pending Delete CV",
                "yaml_blob": "name: Pending\n",
                "deleted_at": None,
            }
        )
        assert client.post("/api/account/delete", json={"confirm": "DELETE"}).status_code == 200

        res = client.get("/api/cv")

        assert res.status_code == 404
        assert "Pending" not in res.get_data(as_text=True)

    def test_demo_document_and_cover_lists_fall_back_when_store_unavailable(
        self, server_module, client, bypass_auth, monkeypatch
    ):
        import cover_letter_service as cl_service

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setattr(cl_service, "list_documents", lambda _kind: (_ for _ in ()).throw(RuntimeError("db down")))

        docs = client.get("/api/documents").get_json()
        letters = client.get("/api/cover-letters").get_json()

        assert docs["degraded"] is True
        assert docs["count"] == 1
        assert docs["documents"][0]["type"] == "cv"
        assert letters["count"] == 0
        assert letters["cover_letters"] == []

    def test_demo_document_list_does_not_fallback_when_deletion_pending(
        self, client, fake_supabase, monkeypatch
    ):
        import cover_letter_service as cl_service

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": fake_supabase.owner_id, "managed_credits_remaining": 1, "deletion_requested_at": "now"}
        )
        monkeypatch.setattr(cl_service, "list_documents", lambda _kind: (_ for _ in ()).throw(RuntimeError("db down")))

        res = client.get("/api/documents")

        assert res.status_code == 409
        assert "deletion" in res.get_json()["error"]

    def test_demo_jobs_and_profile_fall_back_when_store_unavailable(
        self, client, bypass_auth, monkeypatch
    ):
        import account_service
        import jobs_service as jobs_svc

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setattr(jobs_svc, "list_jobs", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        monkeypatch.setattr(jobs_svc, "job_detail", lambda _id: (_ for _ in ()).throw(RuntimeError("db down")))
        monkeypatch.setattr(account_service, "get_profile", lambda: (_ for _ in ()).throw(RuntimeError("db down")))

        jobs = client.get("/api/jobs").get_json()
        detail = client.get("/api/jobs/623f6855-503f-4374-8db8-299da3ede264").get_json()
        profile = client.get("/api/account/profile").get_json()

        assert jobs == {"jobs": [], "count": 0}
        assert detail["degraded"] is True
        assert detail["job"]["id"] == "623f6855-503f-4374-8db8-299da3ede264"
        assert profile["degraded"] is True
        assert profile["managed_credits_remaining"] == 0


# ===========================================================================
#  2. make_client / resolve_llm wiring (both directions).
# ===========================================================================
class TestClientFactory:
    def test_make_client_returns_stub_when_on(self, monkeypatch):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        from llm.client import StubLLMClient, make_client

        # Even with a blank/garbage key + provider, demo mode wins.
        client = make_client("anthropic", "")
        assert isinstance(client, StubLLMClient)

    def test_make_client_returns_anthropic_when_off(self, monkeypatch):
        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        from llm.client import AnthropicClient, StubLLMClient, make_client

        client = make_client("anthropic", "sk-ant-something")
        assert isinstance(client, AnthropicClient)
        assert not isinstance(client, StubLLMClient)

    def test_resolve_llm_bypasses_key_and_credit_when_on(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Demo mode: resolve_llm returns a sentinel key, used_managed=False,
        with NO BYO key and NO managed credits and NO operator key — and never
        raises LLMUnavailable."""
        import llm_gateway
        from llm.client import DEMO_STUB_API_KEY

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # No profile row → zero managed credits; no stored BYO key.
        with server_module.app.test_request_context("/api/extract", method="POST"):
            from flask import g

            g.owner_id = FAKE_OWNER
            g.access_token = "stub-token"
            key, used_managed = llm_gateway.resolve_llm("extract")
        assert key == DEMO_STUB_API_KEY
        assert used_managed is False

    def test_resolve_llm_503s_with_blank_key_when_off(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Production parity: flag off + managed credits but a blank operator
        key → LLMUnavailable(503), unchanged from today."""
        import llm_gateway

        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        fake_supabase.tables.setdefault("profiles", []).append(
            {"id": FAKE_OWNER, "managed_credits_remaining": 3}
        )
        with server_module.app.test_request_context("/api/extract", method="POST"):
            from flask import g

            g.owner_id = FAKE_OWNER
            g.access_token = "stub-token"
            with pytest.raises(llm_gateway.LLMUnavailable) as exc:
                llm_gateway.resolve_llm("extract")
        assert exc.value.status == 503
        # The blank-key 503 must NOT have spent a credit.
        assert fake_supabase.tables["profiles"][0]["managed_credits_remaining"] == 3


# ===========================================================================
#  3. The StubLLMClient transforms (pure, deterministic, guard-grounded).
# ===========================================================================
class TestStubTransforms:
    def _cover_letter_user(self):
        from cover_letter import jd, prompts

        jd_json = jd.parse_jd(JD_TEXT)
        _system, user = prompts.build_cover_letter_messages(
            cv=GROUNDED_CV, jd_json=jd_json
        )
        return prompts.COVER_LETTER_SYSTEM_PROMPT, user

    def test_cover_letter_stub_passes_all_guards(self):
        """The headline: the stub's letter — fed through the REAL guards with
        nothing mocked — passes anti-fabrication + cliché + word cap."""
        from cover_letter import guards, jd
        from llm.client import StubLLMClient

        system, user = self._cover_letter_user()
        letter = StubLLMClient().complete(system=system, user=user)
        assert letter.strip()

        jd_json = jd.parse_jd(JD_TEXT)
        # run_guards raises GroundingError / WordCapError on a violation.
        res = guards.run_guards(letter, GROUNDED_CV, jd_context=jd_json)
        assert res.text
        assert res.word_count > 0
        # No ungrounded proper noun / number slipped in.
        assert guards.cross_reference_facts(letter, GROUNDED_CV, jd_context=jd_json) == []

    def test_cover_letter_stub_grounded_with_thin_inputs(self):
        """A CV with no experience + an empty JD must NOT make the stub
        fabricate — the (short) letter still passes the grounding guard."""
        from cover_letter import guards, prompts

        thin_cv = {"name": "Tim Loehr"}
        system, user = (
            prompts.COVER_LETTER_SYSTEM_PROMPT,
            prompts.build_cover_letter_messages(cv=thin_cv, jd_json={})[1],
        )
        from llm.client import StubLLMClient

        letter = StubLLMClient().complete(system=system, user=user)
        # No proper nouns beyond the (grounded) name; no numbers.
        assert guards.cross_reference_facts(letter, thin_cv) == []

    def test_tailor_stub_is_noop_json(self):
        """The tailoring stub returns parseable JSON with an empty suggestion
        list, so the deterministic applier leaves the CV unchanged."""
        import json

        from llm.client import StubLLMClient
        from tailor.apply import apply_suggestions

        raw = StubLLMClient().complete(
            system="You are a CV tailoring engine for a one-page resume editor.",
            user="{}",
        )
        parsed = json.loads(raw)
        assert parsed["suggestions"] == []
        new_cv, applied, _skipped = apply_suggestions(GROUNDED_CV, parsed["suggestions"])
        assert applied == []
        assert new_cv == GROUNDED_CV  # unchanged → guards pass trivially

    def test_extract_stub_returns_valid_cv_yaml(self):
        """The extract stub returns schema-valid YAML derived from the input,
        never fabricating beyond the pasted text."""
        from llm.client import StubLLMClient

        raw = StubLLMClient().complete(
            system="You are a CV/resume data extractor.",
            user="Jane Q. Public\nSoftware Engineer at SomeCorp\n",
        )
        parsed = _yaml.safe_load(raw)
        assert isinstance(parsed, dict)
        assert parsed["name"] == "Jane Q. Public"
        assert parsed.get("experience")

    def test_polish_stub_returns_input_unchanged(self):
        """Polish is a no-fabrication no-op: the input bullet comes back as-is."""
        from llm.client import StubLLMClient

        bullet = "Built ETL pipelines in Python and Spark."
        out = StubLLMClient().complete(
            system="You rewrite resume bullet points so they're tighter.",
            user=bullet,
        )
        assert out == bullet


# ===========================================================================
#  4. /auth/config — the demo_mode flag both ways.
# ===========================================================================
class TestAuthConfig:
    def test_config_reports_demo_mode_true_when_on(self, client, monkeypatch):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        res = client.get("/auth/config")
        assert res.status_code == 200
        body = res.get_json()
        assert body["demo_mode"] is True
        # The demo CREDENTIALS are never leaked through this route.
        text = res.get_data(as_text=True)
        assert "DEMO_PASSWORD" not in text

    def test_config_reports_demo_mode_false_when_off(self, client, monkeypatch):
        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        res = client.get("/auth/config")
        assert res.status_code == 200
        assert res.get_json()["demo_mode"] is False

    def test_public_request_is_refused_when_demo_flag_is_set(self, client, monkeypatch):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        res = client.get("/auth/config", base_url="https://cv.example.com")
        assert res.status_code == 503
        assert res.get_json()["code"] == "unsafe_demo_mode"

    def test_public_config_is_refused_even_over_test_loopback(self, client, monkeypatch):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setenv("PUBLIC_BASE_URL", "https://cv.example.com")
        res = client.get("/auth/config")
        assert res.status_code == 503
        assert res.get_json()["code"] == "unsafe_demo_mode"


# ===========================================================================
#  5. /auth/demo-login — gated route (404 off; session on, grant mocked).
# ===========================================================================
class TestDemoLoginRoute:
    def test_demo_login_404_when_off(self, server_module, client, monkeypatch):
        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        res = client.post("/auth/demo-login")
        assert res.status_code == 404

    def test_demo_login_returns_session_when_on(self, server_module, client, monkeypatch):
        """The route is wired + gated: with demo mode on it returns the
        {access_token, refresh_token} shape. The GoTrue grant is monkeypatched
        — NO network is hit in the test."""
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        # Supabase must look "configured" for the route to attempt the grant.
        import auth as auth_mod

        monkeypatch.setattr(auth_mod, "SUPABASE_URL", "https://demo.supabase.co")
        monkeypatch.setattr(auth_mod, "SUPABASE_PUBLISHABLE_KEY", "pk_test_123")
        monkeypatch.setenv("DEMO_EMAIL", "explicit-demo@example.test")
        monkeypatch.setenv("DEMO_PASSWORD", "explicit-test-password")

        seen = {}

        def _fake_grant(url, pub, email, password):
            seen["url"] = url
            seen["pub"] = pub
            seen["email"] = email
            seen["password"] = password
            return {
                "access_token": "demo-access",
                "refresh_token": "demo-refresh",
                "token_type": "bearer",
            }

        monkeypatch.setattr(server_module, "_demo_password_grant", _fake_grant)

        res = client.post("/auth/demo-login")
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        # ONLY the two tokens are returned — Supabase's exact shape.
        assert body == {"access_token": "demo-access", "refresh_token": "demo-refresh"}
        # The grant used only explicitly configured credentials, never values
        # embedded in source or echoed to the client.
        assert seen["url"] == "https://demo.supabase.co"
        assert seen["pub"] == "pk_test_123"
        assert seen["email"] == "explicit-demo@example.test"
        assert seen["password"] == "explicit-test-password"

    def test_configured_supabase_without_explicit_credentials_stays_local(
        self, server_module, client, monkeypatch
    ):
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.delenv("DEMO_EMAIL", raising=False)
        monkeypatch.delenv("DEMO_PASSWORD", raising=False)
        import auth as auth_mod

        monkeypatch.setattr(auth_mod, "SUPABASE_URL", "https://demo.supabase.co")
        monkeypatch.setattr(auth_mod, "SUPABASE_PUBLISHABLE_KEY", "pk_test_123")
        monkeypatch.setattr(
            server_module,
            "_demo_password_grant",
            lambda *_args: pytest.fail("remote grant must not run without explicit credentials"),
        )

        res = client.post("/auth/demo-login")
        assert res.status_code == 200
        assert res.get_json()["local_demo"] is True

    def test_demo_password_grant_builds_token_url_and_returns_json(self, server_module, monkeypatch):
        """The real grant helper hits ``/auth/v1/token?grant_type=password`` and
        parses the JSON, with ``urlopen`` stubbed so NO network is touched."""
        import json as _json
        import urllib.request

        captured = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return _json.dumps(
                    {"access_token": "a", "refresh_token": "r"}
                ).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["apikey"] = req.headers.get("Apikey")
            captured["method"] = req.get_method()
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
        out = server_module._demo_password_grant(
            "https://demo.supabase.co", "pk_test_123", "demo@onepager.test", "pw"
        )
        assert out == {"access_token": "a", "refresh_token": "r"}
        assert captured["url"] == "https://demo.supabase.co/auth/v1/token?grant_type=password"
        assert captured["apikey"] == "pk_test_123"
        assert captured["method"] == "POST"

    def test_demo_login_uses_local_session_when_grant_fails(self, server_module, client, monkeypatch):
        """A failed grant falls back to a local token that is demo-mode gated."""
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        import auth as auth_mod

        monkeypatch.setattr(auth_mod, "SUPABASE_URL", "https://demo.supabase.co")
        monkeypatch.setattr(auth_mod, "SUPABASE_PUBLISHABLE_KEY", "pk_test_123")
        monkeypatch.setenv("DEMO_EMAIL", "explicit-demo@example.test")
        monkeypatch.setenv("DEMO_PASSWORD", "explicit-test-password")

        def _boom(url, pub, email, password):
            raise RuntimeError("400 invalid grant")

        monkeypatch.setattr(server_module, "_demo_password_grant", _boom)
        res = client.post("/auth/demo-login")
        assert res.status_code == 200
        body = res.get_json()
        assert body["local_demo"] is True
        assert body["access_token"]
        assert len(body["refresh_token"]) >= 32
        assert auth_mod.verify_token(body["access_token"])["sub"] == auth_mod.LOCAL_DEMO_OWNER_ID
        # The exception text / creds never leak into the response.
        assert "invalid grant" not in res.get_data(as_text=True)

    def test_local_session_fallback_skips_real_demo_seeding(self, server_module, client, monkeypatch):
        import auth as auth_mod

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.setattr(auth_mod, "SUPABASE_URL", "https://demo.supabase.co")
        monkeypatch.setattr(auth_mod, "SUPABASE_PUBLISHABLE_KEY", "pk_test_123")
        monkeypatch.setenv("DEMO_EMAIL", "explicit-demo@example.test")
        monkeypatch.setenv("DEMO_PASSWORD", "explicit-test-password")
        monkeypatch.setattr(
            server_module,
            "_demo_password_grant",
            lambda *_args: (_ for _ in ()).throw(RuntimeError("grant failed")),
        )
        seed_calls = []
        monkeypatch.setattr(
            server_module.demo_seed,
            "seed_demo_documents",
            lambda: seed_calls.append("seed"),
        )

        response = client.post("/auth/demo-login")

        assert response.status_code == 200
        assert response.get_json()["local_demo"] is True
        assert seed_calls == []

    def test_local_demo_token_rejected_when_demo_mode_off(self, server_module, monkeypatch):
        import auth as auth_mod

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        token = auth_mod.make_local_demo_access_token()
        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)

        with pytest.raises(auth_mod.AuthError):
            auth_mod.verify_token(token)


# ===========================================================================
#  6. End-to-end through the real services — STUB IS THE LLM (nothing mocked).
# ===========================================================================
class TestEndToEndDemoGeneration:
    def test_cover_letter_generation_passes_guards_with_stub(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Demo mode generates a real, guard-passing cover letter with NO key,
        NO managed credit, and NOTHING mocked in the LLM path."""
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cv_id = _seed_cv(fake_supabase)
        # Deliberately NO profile row → zero managed credits. Demo mode must
        # still succeed (it bypasses the credit gate) without spending any.

        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": JD_TEXT, "tone": "formal"},
        )
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        assert body["body"].strip()
        # Demo mode never consumes a managed credit.
        assert body["used_managed_credit"] is False
        assert body["company"] == "Acme GmbH"
        # It was actually stored.
        docs = [
            d for d in fake_supabase.tables.get("documents", []) if d["type"] == "cover_letter"
        ]
        assert len(docs) == 1
        # No credit row was created / decremented behind our back.
        assert fake_supabase.tables.get("profiles", []) == []

    def test_tailor_passes_guards_with_stub(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Demo mode tailoring (no-op stub) passes the anti-fabrication + 1-page
        guards and creates the tailored copy, with nothing mocked in the LLM."""
        pytest.importorskip("weasyprint")
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cv_id = _seed_cv(fake_supabase)

        res = client.post(
            f"/api/cv/{cv_id}/tailor",
            json={"jd_text": JD_TEXT},
        )
        # 201 (created) — both guards passed; not 422 (fab) and not 409 (overflow).
        assert res.status_code == 201, res.get_data(as_text=True)
        body = res.get_json()
        assert body.get("tailored_cv_id")
        assert body["used_managed_credit"] is False
        # A NEW cv document exists (duplicate-then-tailor) and the original survives.
        cv_docs = [d for d in fake_supabase.tables.get("documents", []) if d["type"] == "cv"]
        assert len(cv_docs) == 2


# ===========================================================================
#  7. Production parity — AI path still gated when the flag is OFF.
# ===========================================================================
class TestProductionUnchangedWhenOff:
    def test_cover_letter_needs_key_when_off(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """Flag off: no key + no credits → 402 needs_key (today's behaviour),
        proving demo mode did not loosen the gate."""
        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cv_id = _seed_cv(fake_supabase)
        # No profile → zero managed credits, no BYO key.
        res = client.post(
            "/api/cover-letter/generate",
            json={"cv_id": cv_id, "jd_text": JD_TEXT},
        )
        assert res.status_code == 402, res.get_data(as_text=True)
        assert res.get_json()["code"] == "needs_key"


# ===========================================================================
#  8. Demo content seeding — the demo user lands on a WORKING product.
# ===========================================================================
class TestDemoSeeding:
    """``demo_seed.seed_demo_documents`` + the demo-login wiring.

    The demo user must get exactly ONE fully-populated sample CV and ONE sample
    cover letter, seeded idempotently (repeat logins don't duplicate / clobber),
    and seeding must do NOTHING when demo mode is off. The in-memory
    ``fake_supabase`` store stands in for the demo user's RLS-scoped rows.
    """

    def _seed_ctx(self, server_module, monkeypatch):
        """Run the seed under a demo-user request context. Returns the result."""
        import demo_seed

        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        with server_module.app.test_request_context("/auth/demo-login", method="POST"):
            from flask import g

            g.owner_id = FAKE_OWNER
            g.access_token = "demo-token"
            return demo_seed.seed_demo_documents()

    def _docs(self, store, doc_type):
        return [
            d
            for d in store.tables.get("documents", [])
            if d.get("type") == doc_type and d.get("deleted_at") is None
        ]

    def test_seed_creates_one_cv_and_one_cover_letter(
        self, server_module, fake_supabase, monkeypatch
    ):
        import demo_seed

        result = self._seed_ctx(server_module, monkeypatch)
        assert result["seeded"] is True
        assert result["error"] is None

        cvs = self._docs(fake_supabase, "cv")
        letters = self._docs(fake_supabase, "cover_letter")
        assert len(cvs) == 1
        assert len(letters) == 1
        assert cvs[0]["name"] == demo_seed.DEMO_CV_NAME
        assert letters[0]["name"] == demo_seed.DEMO_COVER_LETTER_NAME
        # The returned ids match the persisted rows.
        assert result["cv_id"] == cvs[0]["id"]
        assert result["cover_letter_id"] == letters[0]["id"]

    def test_seeded_cv_is_non_empty_jordan_avery(
        self, server_module, fake_supabase, monkeypatch
    ):
        """The seeded CV is the real example (Jordan Avery) — not an empty
        skeleton: it has the name + real experience + skills."""
        self._seed_ctx(server_module, monkeypatch)
        cv = self._docs(fake_supabase, "cv")[0]
        model = _yaml.safe_load(cv["yaml_blob"])
        assert model["name"] == "Jordan Avery"
        assert isinstance(model.get("experience"), list) and len(model["experience"]) >= 2
        assert isinstance(model.get("skills"), list) and model["skills"]
        # The skeleton's tell-tale placeholder must NOT be present.
        assert model["name"] != "Your Name"

    def test_seeded_cover_letter_is_cv_grounded(
        self, server_module, fake_supabase, monkeypatch
    ):
        """The letter references the CV (company/role/body) and links the CV id
        so the build route can resolve the signer."""
        import cover_letter_service as cl_service

        result = self._seed_ctx(server_module, monkeypatch)
        letter = self._docs(fake_supabase, "cover_letter")[0]
        blob = cl_service.parse_blob(letter["yaml_blob"])
        assert blob["company"] == "Globex"
        assert blob["role"] == "Senior Data Engineer"
        assert blob["body"] and "Globex" in blob["body"]
        assert blob["source_cv_id"] == result["cv_id"]

    def test_seed_is_idempotent(self, server_module, fake_supabase, monkeypatch):
        """Running the seed twice (≈ a second demo login) leaves exactly one of
        each — no duplicates."""
        self._seed_ctx(server_module, monkeypatch)
        self._seed_ctx(server_module, monkeypatch)
        assert len(self._docs(fake_supabase, "cv")) == 1
        assert len(self._docs(fake_supabase, "cover_letter")) == 1

    def test_seed_does_not_clobber_tester_edits(
        self, server_module, fake_supabase, monkeypatch
    ):
        """A re-seed must not overwrite an edit the tester made to the seeded
        CV (same name still resolves to the SAME row, untouched)."""
        self._seed_ctx(server_module, monkeypatch)
        cv = self._docs(fake_supabase, "cv")[0]
        cv["yaml_blob"] = "name: Edited By Tester\n"
        original_id = cv["id"]
        self._seed_ctx(server_module, monkeypatch)
        cvs = self._docs(fake_supabase, "cv")
        assert len(cvs) == 1
        assert cvs[0]["id"] == original_id
        assert "Edited By Tester" in cvs[0]["yaml_blob"]

    def test_seed_is_noop_when_demo_mode_off(
        self, server_module, fake_supabase, monkeypatch
    ):
        """Gating: with the flag OFF, seeding writes nothing and reports
        not-seeded — a stray direct call can't pollute a production context."""
        import demo_seed

        monkeypatch.delenv("ONEPAGER_DEMO_MODE", raising=False)
        with server_module.app.test_request_context("/auth/demo-login", method="POST"):
            from flask import g

            g.owner_id = FAKE_OWNER
            g.access_token = "demo-token"
            result = demo_seed.seed_demo_documents()
        assert result["seeded"] is False
        assert result["cv_id"] is None
        assert result["cover_letter_id"] is None
        assert fake_supabase.tables.get("documents", []) == []

    def test_seeded_content_visible_via_api(
        self, server_module, client, fake_supabase, monkeypatch
    ):
        """After seeding, the same RLS-scoped routes the UI calls surface the
        content: /api/cvs lists the populated Sample CV and /api/cover-letters
        lists the sample letter."""
        self._seed_ctx(server_module, monkeypatch)
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")

        import demo_seed

        cvs_res = client.get("/api/cvs")
        assert cvs_res.status_code == 200
        cv_names = [c["name"] for c in cvs_res.get_json()["cvs"]]
        assert demo_seed.DEMO_CV_NAME in cv_names
        # Exactly the one seeded CV — no extra skeleton was auto-seeded on top.
        assert cv_names.count(demo_seed.DEMO_CV_NAME) == 1
        assert len(cv_names) == 1

        cl_res = client.get("/api/cover-letters")
        assert cl_res.status_code == 200
        cl_body = cl_res.get_json()
        assert cl_body["count"] == 1
        assert cl_body["cover_letters"][0]["name"] == demo_seed.DEMO_COVER_LETTER_NAME
        assert cl_body["cover_letters"][0]["company"] == "Globex"

    def test_demo_login_seeds_documents(self, server_module, client, fake_supabase, monkeypatch):
        """End-to-end through the route: a successful demo login (grant mocked,
        NO network) seeds the demo user's CV + cover letter as a side effect."""
        monkeypatch.setenv("ONEPAGER_DEMO_MODE", "1")
        import auth as auth_mod

        monkeypatch.setattr(auth_mod, "SUPABASE_URL", "https://demo.supabase.co")
        monkeypatch.setattr(auth_mod, "SUPABASE_PUBLISHABLE_KEY", "pk_test_123")
        monkeypatch.setenv("DEMO_EMAIL", "explicit-demo@example.test")
        monkeypatch.setenv("DEMO_PASSWORD", "explicit-test-password")
        monkeypatch.setattr(
            server_module,
            "_demo_password_grant",
            lambda *a, **k: {"access_token": "demo-access", "refresh_token": "demo-refresh"},
        )

        res = client.post("/auth/demo-login")
        assert res.status_code == 200, res.get_data(as_text=True)

        import demo_seed

        cvs = [d for d in fake_supabase.tables.get("documents", []) if d.get("type") == "cv"]
        letters = [
            d for d in fake_supabase.tables.get("documents", []) if d.get("type") == "cover_letter"
        ]
        assert len(cvs) == 1 and cvs[0]["name"] == demo_seed.DEMO_CV_NAME
        assert len(letters) == 1 and letters[0]["name"] == demo_seed.DEMO_COVER_LETTER_NAME
        # A second login must not duplicate.
        client.post("/auth/demo-login")
        cvs2 = [d for d in fake_supabase.tables.get("documents", []) if d.get("type") == "cv"]
        assert len(cvs2) == 1
