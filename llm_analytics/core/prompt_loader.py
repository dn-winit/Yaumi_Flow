"""
YAML prompt template loader -- reads and renders prompts from config/prompts/.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from llm_analytics.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class PromptLoader:
    """Loads YAML prompt templates and renders them with data."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._dir = Path((settings or get_settings()).prompts_dir)
        self._templates: Dict[str, Dict[str, str]] = {}
        self._load_all()

    def _load_all(self) -> None:
        for path in self._dir.glob("*.yaml"):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._templates[path.stem] = yaml.safe_load(f)
                logger.info("Loaded prompt: %s", path.stem)
            except Exception as exc:
                logger.error("Failed to load prompt %s: %s", path.name, exc)

    def get_system_prompt(self, template_name: str) -> str:
        tpl = self._templates.get(template_name, {})
        return tpl.get("system_prompt", "")

    def render(self, template_name: str, template_key: str, **kwargs: Any) -> str:
        """Render a prompt template with keyword arguments."""
        tpl = self._templates.get(template_name, {})
        raw = tpl.get(template_key, "")
        if not raw:
            logger.warning("Template %s.%s not found", template_name, template_key)
            return ""
        try:
            return raw.format(**kwargs)
        except KeyError as exc:
            logger.error("Missing template variable %s in %s.%s", exc, template_name, template_key)
            return raw

    def list_templates(self) -> list[str]:
        return list(self._templates.keys())

    def reload(self) -> None:
        self._templates.clear()
        self._load_all()
