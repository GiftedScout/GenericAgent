"""Deprecated shim: the qwen3 tunnel now lives in the generic ssh_tunnel module."""
from ssh_tunnel import ensure_tunnel, release_tunnel, close_tunnel
from ssh_tunnel import TUNNELS as _TUNNELS

_QWEN = "qwen3-27b"


def ensure_qwen3_tunnel():
    return ensure_tunnel(_QWEN)


def release_qwen3_tunnel():
    return release_tunnel(_QWEN)


def close_qwen3_tunnel():
    return close_tunnel(_QWEN)
