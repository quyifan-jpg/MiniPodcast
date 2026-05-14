"""
Test runner that blocks broken third-party pytest plugins before startup.

deepeval registers itself as a pytest11 entry point but its import chain
is broken on the current langchain_core version. Stubbing it out in
sys.modules before pytest loads entry points is the only reliable fix.

Usage:
    python3 run_tests.py [pytest args...]
    python3 run_tests.py tests/ -v
    python3 run_tests.py tests/test_rag_engine.py -v
"""
import sys
import types


def _make_stub(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    m.__loader__ = None
    m.__spec__ = None
    m.__path__ = []
    m.__package__ = name.split(".")[0]
    return m


# deepeval's pytest11 entry point is `deepeval.plugins.plugin`.
# Stub that module with an empty namespace so pluggy loads it cleanly
# (no hooks registered → deepeval is silently disabled).
for _name in [
    "deepeval",
    "deepeval.plugins",
    "deepeval.plugins.plugin",
]:
    sys.modules[_name] = _make_stub(_name)

import pytest

sys.exit(pytest.main(sys.argv[1:]))
