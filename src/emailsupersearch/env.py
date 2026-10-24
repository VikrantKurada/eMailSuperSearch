"""Zero-network environment: variables that stop dependencies phoning home."""

import os

ZERO_NETWORK_VARS: dict[str, str] = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_UPDATE_CHECK": "1",
    "ORT_DISABLE_TELEMETRY": "1",
}


def apply_zero_network_env() -> None:
    """Set each zero-network variable unless the user already set it."""
    for key, value in ZERO_NETWORK_VARS.items():
        os.environ.setdefault(key, value)
