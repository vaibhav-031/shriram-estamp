"""Test-wide safety net: no test may reach the network. config.yaml points at the real in-house APIs and .env holds
real keys, so any HTTP call that a test didn't replace with a fake fails loudly instead of going out."""
import pytest
import requests


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def blocked(*a, **kw):
        raise NetworkBlocked("tests must not make real HTTP calls – fake requests.post / the client instead")

    monkeypatch.setattr(requests, "post", blocked)
    monkeypatch.setattr(requests, "get", blocked)
    monkeypatch.setattr(requests.Session, "request", blocked)
