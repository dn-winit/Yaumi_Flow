"""
Provider-agnostic LLM client. Supports Groq, OpenAI, Anthropic via config.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from llm_analytics.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class LLMClient:
    """Sends messages to the configured LLM provider and returns parsed JSON."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._client: Any = None
        self.is_available = False
        self._init_client()

    def _init_client(self) -> None:
        if not self._s.api_key:
            logger.info("No LLM API key configured -- analysis features disabled")
            return

        provider = self._s.provider
        try:
            if provider == "groq":
                from groq import Groq
                self._client = Groq(api_key=self._s.api_key, timeout=self._s.timeout)
            elif provider == "openai":
                from openai import OpenAI
                self._client = OpenAI(api_key=self._s.api_key, timeout=self._s.timeout)
            elif provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=self._s.api_key)
            self.is_available = True
            logger.info("LLM client initialized: provider=%s, model=%s", provider, self._s.model)
        except Exception as exc:
            logger.warning("Failed to init LLM client (%s): %s", provider, exc)

    def chat(self, system_prompt: str, user_prompt: str, attempt: int = 0) -> Dict[str, Any]:
        """
        Send a chat request and return parsed JSON response.
        Raises RuntimeError on failure.
        """
        if not self.is_available:
            raise RuntimeError("LLM client not available -- check API key and provider")

        provider = self._s.provider

        if provider in ("groq", "openai"):
            return self._chat_openai_compat(system_prompt, user_prompt, attempt)
        elif provider == "anthropic":
            return self._chat_anthropic(system_prompt, user_prompt, attempt)
        else:
            raise RuntimeError(f"Unsupported provider: {provider}")

    def _chat_openai_compat(self, system: str, user: str, attempt: int) -> Dict[str, Any]:
        """OpenAI-compatible API (Groq, OpenAI)."""
        response = self._client.chat.completions.create(
            model=self._s.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._s.temperature,
            max_tokens=self._s.max_tokens,
            top_p=self._s.top_p,
            seed=self._s.seed + attempt,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        return self._parse_json(raw)

    def _chat_anthropic(self, system: str, user: str, attempt: int) -> Dict[str, Any]:
        """Anthropic Messages API."""
        response = self._client.messages.create(
            model=self._s.model,
            max_tokens=self._s.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=self._s.temperature,
            top_p=self._s.top_p,
        )
        raw = response.content[0].text
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> Dict[str, Any]:
        """Strip markdown fences and parse JSON."""
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)
        return json.loads(text)

    def health(self) -> Dict[str, Any]:
        return {
            "available": self.is_available,
            "provider": self._s.provider,
            "model": self._s.model,
        }
