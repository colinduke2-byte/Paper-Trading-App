import pytest

from app.api import auth


@pytest.fixture(autouse=True)
def _no_access_settings(monkeypatch):
    """Tests start with LAN mode and the passcode off, whatever the developer's shell has set."""
    for key in auth.ACCESS_KEYS + ("PAPERDESK_ENV_FILE",):
        monkeypatch.delenv(key, raising=False)
