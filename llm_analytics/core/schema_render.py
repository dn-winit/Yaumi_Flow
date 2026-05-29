"""
Schema rendering for prompt injection.

The Pydantic response models are the single source of truth for what each
LLM artifact must look like. This module renders those models into a
compact human+LLM-readable schema description that gets appended to the
system prompt, so the model sees the EXACT contract Pydantic will validate
against. No drift possible: rename a field in schemas.py, the prompt
auto-updates on the next call.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def render_schema_for_prompt(model_cls: type[BaseModel]) -> str:
    """Return a compact JSON-Schema-style description for the model class.

    Format chosen for readability + token economy: one line per field with
    type + the Field(description=...) text inline. Verbose JSON Schema
    output (with $defs, additionalProperties, etc.) would burn ~5x more
    tokens for the same constraint information on these flat models.
    """
    schema = model_cls.model_json_schema()
    props: dict[str, Any] = schema.get("properties", {})

    lines = ["{"]
    field_lines = []
    for name, spec in props.items():
        type_hint = _type_hint(spec)
        desc = spec.get("description", "")
        field_lines.append(f'  "{name}": {type_hint}  // {desc}' if desc else f'  "{name}": {type_hint}')
    lines.append(",\n".join(field_lines))
    lines.append("}")
    return "\n".join(lines)


def _type_hint(spec: dict[str, Any]) -> str:
    """Compact type representation from a Pydantic JSON Schema property."""
    t = spec.get("type")
    if t == "array":
        items = spec.get("items", {})
        inner = _type_hint(items)
        return f"[{inner}, ...]"
    if t == "string":
        return '"string"'
    if t == "integer":
        return "0"
    if t == "number":
        return "0.0"
    if t == "boolean":
        return "true"
    # Fallback (rare on our flat narrative models)
    return json.dumps(spec)
