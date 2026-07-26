"""Static contracts for the production container security boundary."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WOLFI = (
    "cgr.dev/chainguard/wolfi-base@sha256:"
    "02dab76bd852a70556b5b2002195c8a5fdab77d323c433bf6642aab080489795"
)
GO_BUILDER = (
    "golang:1.26.5-alpine3.23@sha256:"
    "622e56dbc11a8cfe87cafa2331e9a201877271cbff918af53d3be315f3da88cc"
)
TRIVY_IMAGE = (
    "aquasec/trivy:0.72.0@sha256:"
    "cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f"
)
UPLOAD_ARTIFACT = (
    "actions/upload-artifact@"
    "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_application_image_uses_pinned_wolfi_python_and_renderer_stack():
    dockerfile = _read("Dockerfile")

    assert f"FROM {WOLFI}" in dockerfile
    assert "python-3.13=3.13.14-r3" in dockerfile
    assert "py3.13-pip=26.1.2-r1" in dockerfile
    for package in (
        "cairo=1.18.4-r6",
        "pango=1.58.0-r2",
        "gdk-pixbuf=2.44.7-r0",
        "harfbuzz=14.2.1-r2",
        "fontconfig=2.18.2-r0",
        "font-liberation=2.1.5-r5",
        "ca-certificates=20260413-r0",
        "libffi=3.7.1-r0",
        "shared-mime-info=2.5.1-r0",
    ):
        assert package in dockerfile

    assert "--require-hashes" in dockerfile
    assert "-r requirements.lock" in dockerfile
    assert "python3.13 -m pip check" in dockerfile
    assert "USER onepager:onepager" in dockerfile
    assert "XDG_CACHE_HOME=/tmp/.cache" in dockerfile
    assert "adduser -S -D -H" in dockerfile
    assert "-u 999" in dockerfile
    assert "COPY --chown=onepager:onepager . ." in dockerfile
    assert "python:3.13" not in dockerfile
    assert "apt-get" not in dockerfile


def test_caddy_is_source_built_with_pinned_go_caddy_and_grpc():
    dockerfile = _read("Dockerfile.caddy")

    assert f"FROM {GO_BUILDER} AS caddy-builder" in dockerfile
    assert dockerfile.count(f"FROM {WOLFI}") == 1
    assert "ARG CADDY_VERSION=v2.11.4" in dockerfile
    assert "ARG GRPC_VERSION=v1.82.1" in dockerfile
    assert '_ "github.com/caddyserver/caddy/v2/modules/standard"' in dockerfile
    assert '-require="github.com/caddyserver/caddy/v2@${CADDY_VERSION}"' in dockerfile
    assert '-require="google.golang.org/grpc@${GRPC_VERSION}"' in dockerfile
    assert "go version -m /tmp/caddy" in dockerfile
    assert "caddy-build-modules.txt" in dockerfile
    assert "caddy-build-info.txt" in dockerfile
    assert "USER nonroot:nonroot" in dockerfile
    assert "XDG_CONFIG_HOME=/config" in dockerfile
    assert "XDG_DATA_HOME=/data" in dockerfile
    assert "ca-certificates=20260413-r0" in dockerfile
    assert "curl" not in dockerfile.lower()


def test_compose_drops_privileges_and_builds_the_reviewed_caddy():
    compose = _read("docker-compose.yml")

    assert "dockerfile: Dockerfile.caddy" in compose
    assert "image: ${ONEPAGER_APP_IMAGE:-onepager:dev}" in compose
    assert "image: ${ONEPAGER_CADDY_IMAGE:-onepager-caddy:2.11.4}" in compose
    assert compose.count("pull_policy: never") == 3
    assert "caddy:2.10.2" not in compose
    assert compose.count("read_only: true") == 3
    assert compose.count("no-new-privileges:true") == 3
    assert compose.count("cap_drop:") == 3
    assert compose.count("- ALL") == 3
    assert compose.count("init: true") == 3
    assert 'user: "999:999"' in compose
    assert 'user: "65532:65532"' in compose
    assert "/tmp:rw,noexec,nosuid,nodev" in compose
    assert '"443:443/udp"' in compose


def test_release_compose_removes_builds_and_requires_immutable_images():
    release = _read("docker-compose.release.yml")

    assert release.count("build: !reset null") == 2
    assert release.count("${ONEPAGER_APP_IMAGE:?") == 2
    assert release.count("${ONEPAGER_CADDY_IMAGE:?") == 1
    assert "onepager:dev" not in release
    assert "onepager-caddy:2.11.4" not in release


def test_ci_and_deploy_fail_closed_on_container_cves_and_retain_reports():
    scanner = _read("tools/ci/scan_container_images.sh")

    assert TRIVY_IMAGE in scanner
    assert "set -euo pipefail" in scanner
    assert "docker save --output" in scanner
    assert "--input /input/image.tar" in scanner
    assert scanner.count("--scanners vuln") == 2
    assert scanner.count("--severity HIGH,CRITICAL") == 2
    assert "--exit-code 1" in scanner
    assert "--format json" in scanner
    assert "--format table" in scanner
    assert "scan_status=1" in scanner
    assert "/var/run/docker.sock" not in scanner

    for path in (".github/workflows/ci.yml", ".github/workflows/deploy.yml"):
        workflow = _read(path)
        assert "docker compose --profile prod build --pull app caddy" in workflow
        assert "Python 3.13.14" in workflow
        assert "github.com/caddyserver/caddy/v2 v2.11.4" in workflow
        assert "google.golang.org/grpc v1.82.1" in workflow
        assert "docker/scout-action@" not in workflow
        assert workflow.count("bash tools/ci/scan_container_images.sh") == 1
        assert "container-cve-reports-${{ github.sha }}" in workflow
        assert "path: trivy-reports/" in workflow
        assert "if-no-files-found: error" in workflow
        assert "retention-days: 30" in workflow
        assert UPLOAD_ARTIFACT in workflow


def test_ci_and_deploy_use_direct_locally_resolvable_image_ids():
    for path in (".github/workflows/ci.yml", ".github/workflows/deploy.yml"):
        workflow = _read(path)
        assert 'ONEPAGER_APP_IMAGE="$release_app_id"' in workflow
        assert 'ONEPAGER_CADDY_IMAGE="$release_caddy_id"' in workflow
        assert 'export ONEPAGER_APP_IMAGE="$release_app_id"' in workflow
        assert 'export ONEPAGER_CADDY_IMAGE="$release_caddy_id"' in workflow
        assert "onepager@$release_app_id" not in workflow
        assert "onepager-caddy@$release_caddy_id" not in workflow


def test_ci_always_retains_browser_failure_diagnostics():
    workflow = _read(".github/workflows/ci.yml")
    browser_job = workflow.split("  browser:", 1)[1]

    assert browser_job.count("if: ${{ always() }}") == 2
    assert "id: browser_tests" in browser_job
    assert "BROWSER_TEST_OUTCOME: ${{ steps.browser_tests.outcome }}" in browser_job
    assert 'status_file="$diagnostics_dir/status.txt"' in browser_job
    assert "browser_test_outcome=" in browser_job
    assert "browser_job_status=" in browser_job
    assert 'if [[ -f "$server_log" ]]' in browser_job
    assert 'if install -m 0600 "$server_log"' in browser_job
    assert "server_log=missing" in browser_job
    assert "actual_image_copy_failures=" in browser_job
    assert "exit 0" in browser_job
    assert '"$RUNNER_TEMP/cv-engine-browser.log"' in browser_job
    assert '"$diagnostics_dir/server.log"' in browser_job
    assert "tests/e2e/baselines/*.actual.png" in browser_job
    assert '"$diagnostics_dir/${actual##*/}"' in browser_job
    assert f"uses: {UPLOAD_ARTIFACT}" in browser_job
    assert "name: browser-diagnostics-${{ github.sha }}" in browser_job
    assert "path: browser-diagnostics/" in browser_job
    assert "if-no-files-found: error" in browser_job
    assert "retention-days: 14" in browser_job


def test_visual_regression_uses_reviewed_platform_specific_baselines():
    source = _read("tests/e2e/test_visual_regression.py")
    names = (
        "editor-dark-cards",
        "editor-topbar-dark-de",
        "editor-topbar-light-en",
        "jobs-chrome-light-de",
        "palette-open-light-en",
    )

    assert 'sys.platform.startswith("linux")' in source
    assert 'sys.platform == "darwin"' in source
    assert 'f"{name}.linux.png"' in source
    assert 'f"{name}.png"' in source
    assert "no reviewed visual-regression baseline for platform" in source
    assert "baseline_path = _baseline_path(name)" in source
    assert "TOLERANCE = 0.015" in source
    assert "PER_CHANNEL_SLACK = 12" in source
    for name in names:
        assert (ROOT / f"tests/e2e/baselines/{name}.png").is_file()
        assert (ROOT / f"tests/e2e/baselines/{name}.linux.png").is_file()


def test_release_security_docs_match_trivy_tar_input_contract():
    deploy = _read("DEPLOY.md")
    security = _read("SECURITY.md")

    for document in (deploy, security):
        assert "docker scout" not in document.lower()
        assert "Trivy" in document
        assert "scan time" in document
    assert TRIVY_IMAGE in deploy
    assert "bash tools/ci/scan_container_images.sh" in deploy
    assert "docker save" in deploy
    assert "--input /input/image.tar" in deploy
    assert "mount the Docker socket" in deploy
    assert "requires no scanner account" in deploy
    assert "fails closed" in deploy
    assert 'ONEPAGER_APP_IMAGE="$(docker image inspect' in deploy
    assert 'ONEPAGER_CADDY_IMAGE="$(docker image inspect' in deploy
    assert 'ONEPAGER_APP_IMAGE="onepager@$(docker image inspect' not in deploy
    assert "read-only `docker save` tar" in security
    assert "scanner/database" in security
    assert "fail closed" in security


def test_deploy_migrates_existing_caddy_volumes_before_nonroot_start():
    workflow = _read(".github/workflows/deploy.yml")

    assert "onepager_caddy_data" in workflow
    assert "onepager_caddy_config" in workflow
    assert "--user 0:0" in workflow
    assert "--cap-drop ALL" in workflow
    assert "--cap-add CHOWN" in workflow
    assert '"$EXPECTED_CADDY_IMAGE_ID"' in workflow
    assert "chown -R 65532:65532 /mnt" in workflow


def test_deploy_uses_the_scanned_immutable_images_without_rebuilding():
    workflow = _read(".github/workflows/deploy.yml")
    remote_deploy = workflow.split("- name: Deploy over SSH", 1)[1]

    assert "environment: production" in workflow.split("deploy:", 1)[0]
    assert "docker save" in workflow
    assert "onepager-images.tar.sha256" in workflow
    assert "onepager-images.manifest" in workflow
    assert "archive_sha256=" in workflow
    assert "app_id=" in workflow
    assert "caddy_id=" in workflow
    assert "retention-days: 1" in workflow
    assert (
        "actions/upload-artifact@"
        "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    ) in workflow
    assert (
        "actions/download-artifact@"
        "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
    ) in workflow
    assert "Runtime secret-like environment variable baked into" in workflow
    assert "Build-time credential material found in" in workflow
    assert "/app/.claude" in workflow
    assert "/app/artifacts" in workflow
    assert "find /app/design/photos -mindepth 1 -print -quit" in workflow
    assert '-type f -name "*.actual.png" -print -quit' in workflow
    assert "sha256sum --check onepager-images.tar.sha256" in workflow
    assert "docker load --input" in workflow
    assert 'export COMPOSE_FILE="docker-compose.yml:docker-compose.release.yml"' in workflow
    assert "'COMPOSE_FILE=\"docker-compose.yml:docker-compose.release.yml\"'" in workflow
    assert "Production Compose config retained a build fallback" in workflow
    assert 'ONEPAGER_APP_IMAGE=\\"$ONEPAGER_APP_IMAGE\\"' in workflow
    assert 'export ONEPAGER_APP_IMAGE="$EXPECTED_APP_IMAGE_ID"' in workflow
    assert 'export ONEPAGER_CADDY_IMAGE="$EXPECTED_CADDY_IMAGE_ID"' in workflow
    assert "onepager@$EXPECTED_APP_IMAGE_ID" not in workflow
    assert "onepager-caddy@$EXPECTED_CADDY_IMAGE_ID" not in workflow
    assert "up -d --no-build --pull never --remove-orphans" in remote_deploy
    assert "unset COMPOSE_FILE" in remote_deploy
    assert "docker compose -p onepager --profile prod --env-file .env build" not in remote_deploy
