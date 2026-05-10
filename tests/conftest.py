"""Pytest configuration shared across the test suite.

The HA test harness (``pytest-homeassistant-custom-component``) installs
``pytest_socket`` globally, which disables real socket use to keep HA
unit tests hermetic. Our library has its own e2e tests that legitimately
need to talk to a localhost ``MockPanel`` over a real TCP socket, so we
re-enable sockets by default and let the HA integration tests opt back
into the strict policy via the harness fixtures.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _enable_localhost_sockets(socket_enabled: pytest.FixtureRequest) -> None:  # type: ignore[valid-type]
    """Re-enable sockets for every test by default.

    ``socket_enabled`` is the standard fixture exported by ``pytest_socket``
    (and re-exported by the HA harness); requesting it via autouse undoes
    the harness's default ``disable_socket()`` for tests that need real
    networking. HA-side tests can override by explicitly using the
    ``socket_disabled`` fixture if they want hermetic behaviour.
    """
    return None
