"""E2E-only pytest plumbing.

A single-retry guard for genuinely timing-flaky browser tests, scoped to this
directory (unit tests under ``tests/`` never see it). ``pytest-rerunfailures``
is intentionally NOT a dependency: the hook below re-executes a failed test
exactly once via ``_pytest.runner.runtestprotocol`` and only reports the
outcome that matters:

- first attempt passes  → reported normally, no retry happens;
- first attempt fails,
  retry passes          → only the retry's reports are logged (suite stays
                          green) plus a ``FLAKY-RETRY`` line on the terminal so
                          the recovered flake is visible, not silent;
- both attempts fail    → the retry's reports are logged normally AND the
                          first attempt's failure is logged too, so no failure
                          information is ever swallowed.

Fixtures are fully torn down and re-set-up for the retry (function scope;
module-scoped fixtures such as the shared browser are reused). Opt out per
test with ``@pytest.mark.no_retry``. Set ``ONEPAGER_E2E_RETRY=0`` to disable
the guard suite-wide for strict certification runs; pytest then uses its
normal single-attempt protocol and reports the first failure directly.
"""
from __future__ import annotations

import os

from _pytest.runner import runtestprotocol

_MAX_RERUNS = 1
_RERUN_FLAG = "_e2e_reruns_done"


def _failed(reports) -> bool:
    return any(r.failed for r in reports if r.when in ("setup", "call"))


def pytest_runtest_protocol(item, nextitem):
    if os.environ.get("ONEPAGER_E2E_RETRY") == "0":
        return None  # strict gate: delegate to pytest's single-attempt protocol
    if item.get_closest_marker("no_retry") is not None:
        return None  # default single-attempt handling
    if not hasattr(item, "_e2e_reruns_done"):
        item._e2e_reruns_done = 0  # noqa: SLF001 — per-item retry bookkeeping
    if item._e2e_reruns_done >= _MAX_RERUNS:
        return None

    reports = runtestprotocol(item, nextitem=nextitem, log=False)
    if not _failed(reports):
        for report in reports:
            item.ihook.pytest_runtest_logreport(report=report)
        return True

    item._e2e_reruns_done += 1
    terminal = item.config.pluginmanager.getplugin("terminalreporter")
    if terminal is not None:
        terminal.write_line(
            f"FLAKY-RETRY {item.nodeid} — first attempt failed; re-running once",
        )
    rerun_reports = runtestprotocol(item, nextitem=nextitem, log=True)
    if _failed(rerun_reports):
        # The flake was real (or the retry hit a different failure): surface
        # the first attempt's failure as well so both are diagnosable.
        for report in reports:
            if report.when in ("setup", "call"):
                item.ihook.pytest_runtest_logreport(report=report)
    return True


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_retry: opt an e2e test out of the single flaky-retry guard",
    )
