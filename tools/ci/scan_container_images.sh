#!/usr/bin/env bash
#
# Scan locally built release images without a Docker Hub account or any
# vendor entitlement. The scanner binary is immutable; its vulnerability
# database is refreshed at runtime so the verdict reflects current advisories.

set -euo pipefail

readonly TRIVY_IMAGE="aquasec/trivy:0.72.0@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f"
readonly REPORT_DIR="${TRIVY_REPORT_DIR:-trivy-reports}"
readonly CACHE_PARENT="${RUNNER_TEMP:-/tmp}"
readonly CACHE_DIR="${TRIVY_CACHE_DIR:-${CACHE_PARENT}/cv-engine-trivy-cache}"

images=(
  "onepager:dev"
  "onepager-caddy:2.11.4"
)

install -d -m 0700 "$REPORT_DIR" "$CACHE_DIR"
report_dir_abs="$(cd "$REPORT_DIR" && pwd -P)"
cache_dir_abs="$(cd "$CACHE_DIR" && pwd -P)"
scan_dir="$(mktemp -d "${CACHE_PARENT}/cv-engine-trivy-input.XXXXXX")"

cleanup() {
  rm -rf -- "$scan_dir"
}
trap cleanup EXIT

printf '%s\n' \
  "scanner=$TRIVY_IMAGE" \
  "database=runtime-current" \
  "severity_gate=HIGH,CRITICAL" \
  > "$REPORT_DIR/scan-metadata.txt"

scan_status=0
for image in "${images[@]}"; do
  slug="${image//\//-}"
  slug="${slug//:/-}"
  archive_path="$scan_dir/${slug}.tar"

  image_id="$(docker image inspect "$image" --format '{{.Id}}')" || {
    printf 'Cannot resolve local image %s\n' "$image" >&2
    scan_status=1
    continue
  }
  printf '%s\n' \
    "image=$image" \
    "image_id=$image_id" \
    > "$REPORT_DIR/${slug}.metadata.txt"

  if ! docker save --output "$archive_path" "$image"; then
    printf 'Cannot export local image %s for scanning\n' "$image" >&2
    scan_status=1
    continue
  fi

  if ! docker run --rm --read-only \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev \
    -v "$archive_path:/input/image.tar:ro" \
    -v "$cache_dir_abs:/root/.cache/trivy" \
    -v "$report_dir_abs:/reports" \
    "$TRIVY_IMAGE" image \
    --scanners vuln \
    --severity HIGH,CRITICAL \
    --exit-code 0 \
    --format json \
    --output "/reports/${slug}.json" \
    --input /input/image.tar; then
    printf 'Trivy could not produce the JSON report for %s\n' "$image" >&2
    scan_status=1
    continue
  fi

  if ! docker run --rm --read-only \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev \
    -v "$archive_path:/input/image.tar:ro" \
    -v "$cache_dir_abs:/root/.cache/trivy" \
    "$TRIVY_IMAGE" image \
    --scanners vuln \
    --severity HIGH,CRITICAL \
    --exit-code 1 \
    --format table \
    --input /input/image.tar \
    | tee "$REPORT_DIR/${slug}.txt"; then
    scan_status=1
  fi
done

docker run --rm --read-only \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev \
  -v "$cache_dir_abs:/root/.cache/trivy:ro" \
  "$TRIVY_IMAGE" --version \
  >> "$REPORT_DIR/scan-metadata.txt" 2>&1 || scan_status=1

exit "$scan_status"
