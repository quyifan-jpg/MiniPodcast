"""
Root conftest.py — blocks broken third-party pytest plugins from loading.

deepeval registers itself as a pytest11 entry point but its import chain
breaks on the current langchain_core version (missing langchain_v1 tracer).
Explicitly collecting and unregistering it before any test collection runs.
"""


def pytest_configure(config):
    # Block deepeval's pytest plugin — it fails to import due to a
    # langchain_core compatibility issue (no langchain_v1 tracer module).
    config.pluginmanager.set_blocked("deepeval")
