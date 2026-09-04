"""consumable_manager 真环境测试（pytest-homeassistant-custom-component）。"""

from __future__ import annotations
import sys
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parent.parent   # tests_ha/ 的上级 = 集成目录
CUSTOM_COMPONENTS = INTEGRATION.parent
REPO_ROOT = CUSTOM_COMPONENTS.parent
for _p in (REPO_ROOT, CUSTOM_COMPONENTS, INTEGRATION, Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
pytest_plugins = ("pytest_homeassistant_custom_component",)

def tools_dir() -> Path:
    """tools/ 目录：优先集成内，否则仓库根（GitHub 根布局）。"""
    inner = INTEGRATION / "tools"
    return inner if inner.is_dir() else REPO_ROOT / "tools"
