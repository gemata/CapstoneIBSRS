from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ibsrs.pipeline import ARTIFACTS, RUNS_DIR  # noqa: E402

problems: list[str] = []


def check(run: str, ok: bool, msg: str) -> None:
    if not ok:
        problems.append(f"[{run}] {msg}")


def j(d: Path, name: str):
    return json.loads((d / name).read_text(encoding="utf-8"))
