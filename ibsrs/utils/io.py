from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_json(path: Path, data: BaseModel | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, list):
        payload = [
            item.model_dump() if isinstance(item, BaseModel) else item
            for item in data
        ]
    elif isinstance(data, BaseModel):
        payload = data.model_dump()
    else:
        payload = data
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class AuditLog:
    def __init__(self, run_dir: Path, run_id: str):
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self._lines: list[str] = [f"# Audit Log — {run_id}", ""]

    def section(self, agent: str, title: str) -> None:
        self._lines.extend(["", f"## {agent}: {title}", ""])

    def step(self, msg: str) -> None:
        self._lines.append(f"- {msg}")

    def decision(self, msg: str, rationale: str) -> None:
        self._lines.append(f"- **Decision:** {msg}")
        self._lines.append(f"  - *Rationale:* {rationale}")

    def artifact(self, name: str, path: Path) -> None:
        self._lines.append(f"- Wrote artifact `{name}` → `{path}`")

    def close(self) -> None:
        out = self.run_dir / "audit_log.md"
        out.write_text("\n".join(self._lines) + "\n", encoding="utf-8")
