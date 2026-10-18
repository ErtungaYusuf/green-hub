import os
import sys

import pytest

# Use the Green package installed, or the green repository next to this one.
try:
    import green  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "green"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from green import params  # noqa: E402


@pytest.fixture(autouse=True)
def easy_target(monkeypatch):
    monkeypatch.setattr(params, "INITIAL_TARGET", params.MAX_TARGET)    # every ticket is a block
    monkeypatch.setattr(params, "SHARES_PERCENT", 0)
