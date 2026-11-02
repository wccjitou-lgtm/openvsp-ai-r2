# -*- coding: utf-8 -*-
"""Download Python 3.13 embeddable and install AI dependencies into ai/python.

Usage (from repo root, with a host Python 3.13):
    python scripts\prepare_embedded_python.py
"""
from __future__ import annotations

import io
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "3.13.3"
URL = f"https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip"
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "ai" / "python"
REQUIREMENTS = ("numpy", "python-docx", "matplotlib", "pypdf", "openpyxl", "six")


def main() -> int:
    if sys.version_info[:2] != (3, 13):
        print("Host Python must be 3.13 (the OpenVSP API is cp313).")
        return 2
    TARGET.mkdir(parents=True, exist_ok=True)
    print("downloading", URL)
    data = urllib.request.urlopen(URL, timeout=180).read()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(TARGET)
    pth = TARGET / "python313._pth"
    pth.write_text(
        "python313.zip\n.\nLib\\site-packages\nimport site\n",
        encoding="utf-8",
    )
    site = TARGET / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
           "--no-warn-script-location", "--target", str(site), *REQUIREMENTS]
    print(" ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
