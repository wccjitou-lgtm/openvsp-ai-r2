# -*- coding: utf-8 -*-
"""独立分析运行仓库：run 目录、输入指纹和 manifest。"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def sha256_file(path: str | Path) -> str | None:
    p = Path(path)
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RunStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def new_run(self, kind: str = "analysis") -> tuple[str, Path]:
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
        run_id = f"{kind}-{stamp}-{uuid.uuid4().hex[:8]}"
        run_dir = self.root / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        return run_id, run_dir

    @staticmethod
    def write_json(run_dir: str | Path, name: str, payload: Any) -> Path:
        path = Path(run_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                   default=str), encoding="utf-8")
        return path

    @staticmethod
    def file_records(paths: list[str | Path]) -> list[dict[str, Any]]:
        records = []
        for raw in paths:
            p = Path(raw)
            records.append({"path": str(p), "size": p.stat().st_size if p.is_file() else None,
                            "sha256": sha256_file(p)})
        return records

    def write_manifest(self, run_dir: str | Path, payload: dict[str, Any]) -> Path:
        payload = dict(payload)
        payload.setdefault("manifest_version", "1.0")
        payload.setdefault("created_at", datetime.now().astimezone().isoformat(timespec="seconds"))
        return self.write_json(run_dir, "manifest.json", payload)


__all__ = ["RunStore", "sha256_file", "fingerprint"]
