from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel


def write_json(path: Path, data) -> None:
    if isinstance(data, BaseModel):
        data = data.model_dump()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AuditLog:
    """Append-only markdown audit trail (audit_log.md).

    Every agent step records what it read, what it decided and why, and
    what it wrote - the SOX traceability requirement.
    """

    def __init__(self, run_dir: Path, run_id: str):
        self.path = Path(run_dir) / "audit_log.md"
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                f"# IBSRS Audit Log\n\n"
                f"- **Run ID:** `{run_id}`\n"
                f"- **Started:** {utc_now_iso()}\n\n---\n",
                encoding="utf-8",
            )

    def section(self, agent: str, title: str) -> None:
        self._append(f"\n## {agent} - {title}\n\n")

    def step(self, message: str) -> None:
        self._append(f"- {message}\n")

    def note(self, markdown: str) -> None:
        """Append a raw markdown block (used for the AI Reasoning narrative)."""
        self._append(f"\n{markdown}\n")

    def decision(self, message: str, rationale: str) -> None:
        self._append(f"- **DECISION:** {message}\n  - *Rationale:* {rationale}\n")

    def artifact(self, name: str, path: Path) -> None:
        digest = file_sha256(path)[:16]
        self._append(f"- **WROTE** `{name}` (sha256:{digest})\n")

    def _append(self, text: str) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(text)
