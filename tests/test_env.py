import os

from emailsupersearch.env import ZERO_NETWORK_VARS, apply_zero_network_env


def test_zero_network_env_applied(monkeypatch):
    for k in ZERO_NETWORK_VARS: monkeypatch.delenv(k, raising=False)
    apply_zero_network_env()
    assert all(os.environ[k] == v for k, v in ZERO_NETWORK_VARS.items())


def test_zero_network_env_does_not_override_explicit_value(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0"); apply_zero_network_env()
    assert os.environ["HF_HUB_OFFLINE"] == "0"
