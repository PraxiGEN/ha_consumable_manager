"""consumable_manager 真环境测试（pytest-homeassistant-custom-component）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest_plugins = ("pytest_homeassistant_custom_component",)
