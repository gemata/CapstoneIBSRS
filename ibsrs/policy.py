
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve(
).parent.parent / "policy" / "policy.yaml"


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


def load_policy(path: Path | str | None = None) -> Policy:
    p = Path(path) if path else DEFAULT_POLICY_PATH
    with open(p, "r", encoding="utf-8") as fh:
        return Policy(yaml.safe_load(fh))
