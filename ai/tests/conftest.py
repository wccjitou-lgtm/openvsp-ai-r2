"""pytest 公共配置：把 ai 根目录加入 sys.path。"""

from __future__ import annotations

import sys
from pathlib import Path

AI_ROOT = Path(__file__).resolve().parent.parent
if str(AI_ROOT) not in sys.path:
    sys.path.insert(0, str(AI_ROOT))

EXAMPLES_DIR = AI_ROOT / "examples"
TEMPLATE = AI_ROOT / "templates" / "design_report_template.docx"


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 需要真实运行 VSPAERO 的端到端测试")
