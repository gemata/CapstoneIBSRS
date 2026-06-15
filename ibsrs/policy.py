from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "policy.yaml"


class Policy:
    """Dot-style read-only access over the policy YAML."""

    def __init__(self, data: dict):
        self._data = data

    def get(self, dotted: str, default=None):
        node = self._data
        for key in dotted.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def raw(self) -> dict:
        return self._data


def load_policy(path: str | Path | None = None) -> Policy:
    policy_path = Path(path) if path else DEFAULT_POLICY_PATH
    if not policy_path.exists():
        raise FileNotFoundError(f"Policy file not found: {policy_path}")
    data = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    return Policy(data)

