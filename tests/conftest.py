import httpx
import pytest

from sluice.api.app import create_app
from sluice.auth.keys import KeyStore
from sluice.config import Route, Settings
from sluice.limits.state import MemoryState
from sluice.providers.mock import MockProvider

ROUTE = Route(chain=[{"provider": "mock", "upstream_model": "mock-1", "retries": 0}])


def make_app(settings=None, providers=None, key_kw=None, state=None, **mock_kw):
    """Returns (app, raw_key). Default: one mock provider, in-memory sqlite + state."""
    settings = settings or Settings(providers={"mock": {"type": "mock"}}, routes={"m": ROUTE})
    providers = providers or {"mock": MockProvider(text="hello there world", **mock_kw)}
    keys = KeyStore(":memory:")
    raw, _ = keys.create("test", **(key_kw or {}))
    app = create_app(settings, providers, state=state or MemoryState(), keystore=keys)
    return app, raw


def client_for(app, raw) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"Authorization": f"Bearer {raw}"})


@pytest.fixture
def client_factory():
    def make(key_kw=None, **mock_kw) -> httpx.AsyncClient:
        app, raw = make_app(key_kw=key_kw, **mock_kw)
        return client_for(app, raw)

    return make
