"""Run Store 独立运行目录与指纹回归测试。"""
from __future__ import annotations

from pathlib import Path

from ai_design.run_store import RunStore, fingerprint, sha256_file


def test_run_store_creates_unique_dirs_and_manifest(tmp_path: Path):
    store = RunStore(tmp_path / "job")
    first_id, first_dir = store.new_run("vspaero")
    second_id, second_dir = store.new_run("vspaero")
    assert first_id != second_id
    assert first_dir != second_dir
    assert first_dir.is_dir() and second_dir.is_dir()
    store.write_json(first_dir, "input.json", {"a": 1})
    store.write_manifest(first_dir, {"run_id": first_id, "status": "completed"})
    manifest = (first_dir / "manifest.json").read_text(encoding="utf-8")
    assert first_id in manifest
    assert "completed" in manifest


def test_fingerprint_is_deterministic_and_sha256_available(tmp_path: Path):
    p = tmp_path / "x.txt"
    p.write_text("abc", encoding="utf-8")
    assert fingerprint({"b": 2, "a": 1}) == fingerprint({"a": 1, "b": 2})
    assert sha256_file(p) is not None
    assert len(sha256_file(p)) == 64
