# syntax=docker/dockerfile:1.7

# CV Copilot application image. Wolfi is used instead of Debian/Alpine Python
# images so the production runtime can stay on Python 3.13 without inheriting
# the Perl/SQLite findings present in those image families.
FROM cgr.dev/chainguard/wolfi-base@sha256:65e1acb87a2bf356b92c5f70f3980f03b4bb51dfd483c834e01557525f15c1d9

LABEL org.opencontainers.image.title="CV Copilot application" \
      org.opencontainers.image.base.name="cgr.dev/chainguard/wolfi-base" \
      org.opencontainers.image.description="Gunicorn editor, API, renderer, and account-erasure worker"

ENV HOME=/app \
    LANG=C.UTF-8 \
    PATH=/usr/bin:/bin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# Direct runtime packages are version-pinned. Python pulls Wolfi's current
# patched sqlite-libs dependency; the build gate records and scans the complete
# transitive APK/Python closure. GDK-Pixbuf is retained for the renderer/image
# stack even though recent WeasyPrint paths primarily use Cairo and Pango.
RUN apk add --no-cache \
        python-3.13=3.13.14-r3 \
        py3.13-pip=26.1.2-r1 \
        cairo=1.18.4-r6 \
        pango=1.58.0-r2 \
        gdk-pixbuf=2.44.7-r0 \
        harfbuzz=14.2.1-r2 \
        fontconfig=2.18.2-r0 \
        font-liberation=2.1.5-r5 \
        ca-certificates=20260413-r0 \
        libffi=3.7.1-r0 \
        shared-mime-info=2.5.1-r0 \
    && addgroup -S -g 999 onepager \
    && adduser -S -D -H -h /app -s /sbin/nologin -u 999 -G onepager onepager \
    && python3.13 -c "import sqlite3, sys; assert sys.version_info[:2] == (3, 13); print(sqlite3.sqlite_version)"

WORKDIR /app

# Install Python dependencies before source so the hash-locked layer remains
# cacheable across source-only changes.
COPY requirements.txt requirements.lock ./
RUN python3.13 -m pip install \
        --break-system-packages \
        --require-hashes \
        -r requirements.lock \
    && python3.13 -m pip check

# UID/GID 999 intentionally matches the prior Debian image's `onepager`
# service account so the persistent photo volume remains writable on upgrade.
COPY --chown=onepager:onepager . .
RUN mkdir -p /app/output /app/design/photos \
    && chown -R onepager:onepager /app/output /app/design/photos

USER onepager:onepager

# Fontconfig and WeasyPrint share the noexec Compose /tmp mount for their
# disposable cache while the image root filesystem stays read-only.
ENV XDG_CACHE_HOME=/tmp/.cache

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python3.13", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read()"]

# Override with `python3.13 engine/build.py` for the CLI renderer or
# `python3.13 tools/editor/account_purge_worker.py` for the erasure worker.
CMD ["python3.13", "-m", "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--timeout", "120", "tools.editor.server:app"]
