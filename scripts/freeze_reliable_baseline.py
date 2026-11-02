# -*- coding: utf-8 -*-
"""M0 baseline manifest: hash app source, tests, docs and selected environment facts."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "validation" / "reliable_design_v2" / "M0"
PATHS = [
    ROOT / "ai" / "ai_design",
    ROOT / "ai" / "tests",
    ROOT / "ai" / "templates",
    ROOT / "docs" / "reliable_design_v1",
    ROOT / "mcp",
    ROOT / "scripts",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    files = []
    for base in PATHS:
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            if p.suffix.lower() in {".pyc", ".log"}:
                continue
            rel = p.relative_to(ROOT)
            files.append({"path": str(rel), "size": p.stat().st_size,
                          "sha256": sha256(p)})
    manifest = {
        "manifest_version": "1.0",
        "status": "draft_baseline_pending_review",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "root": str(ROOT),
        "environment": {
            "os": platform.platform(),
            "python_executable": sys.executable,
            "python_version": sys.version,
        },
        "openvsp_root": os.environ.get("OPENVSP_ROOT", ""),
        "openvsp_version": "3.51.3",
        "file_count": len(files),
        "files": files,
    }
    (OUT / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("manifest", OUT / "source_manifest.json", "files", len(files))


if __name__ == "__main__":
    main()
