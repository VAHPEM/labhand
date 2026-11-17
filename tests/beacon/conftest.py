import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from beacon.ledger import Ledger  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_api(monkeypatch):
    """Unit tests never call the real Claude API, even on a machine with a key in the environment."""
    for var in ("BEACON_API_KEY", "ANTHROPIC_API_KEY", "BEACON_API_KEY_FILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _not_inside_a_supervised_session(monkeypatch):
    """The suite may itself run inside a beacon-supervised session (a worker running pytest): the run it
    inherits is not the tests' run, and the CLI refuses session-starting commands under any of its markers
    (CLAUDECODE: pytest started from Claude Code's Bash tool)."""
    for var in ("BEACON_RUN", "BEACON_WATCHDOG", "CLAUDECODE"):
        monkeypatch.delenv(var, raising=False)


# The production default is no model calls at all (Ve, 2026-11-15: the orchestrator is beacon's lighthouse,
# decider and classifier). The model paths are still tested against mock transports, so the suite gives them
# the budgets they had; test_no_model_calls.py (marker real_defaults) checks the real defaults.
MODEL_BUDGETS_FOR_TESTS = {"max_lighthouse_calls": 20, "max_classifier_calls": 100, "max_decider_calls": 60,
                           "max_followup_calls": 100}


def pytest_configure(config):
    config.addinivalue_line("markers", "real_defaults: run with beacon's real DEFAULT_CONFIG (no model budgets)")


@pytest.fixture(autouse=True)
def _model_budgets_for_mocked_model_paths(request, monkeypatch):
    if request.node.get_closest_marker("real_defaults"):
        return
    from beacon import ledger
    for key, value in MODEL_BUDGETS_FOR_TESTS.items():
        monkeypatch.setitem(ledger.DEFAULT_CONFIG, key, value)


@pytest.fixture
def make_run(tmp_path):
    """make_run(problems=..., **config) -> Ledger in a fresh run directory."""
    made = []

    def _make(problems=None, **config):
        problems = problems or [{"title": "Solve challenge one"}, {"title": "Triage crash two"}]
        L = Ledger.create(str(tmp_path / f"run{len(made)}"), "test run", problems, config=config,
                          workdir=str(tmp_path))
        made.append(L)
        return L

    yield _make
    for L in made:
        L.close()


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """The one API retry waits a few seconds in real use; not in the tests."""
    from beacon import lighthouse
    monkeypatch.setattr(lighthouse, "_sleep", lambda s: None)
