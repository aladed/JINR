"""Parse structured playbook JSON from LLM raw output."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Extract first JSON object from LLM response text."""
    text = text.strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def normalize_playbook_dict(data: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    """Fill missing fields from inference defaults."""
    merged = dict(defaults)
    merged.update({k: v for k, v in data.items() if v is not None})
    if "actions" in data and isinstance(data["actions"], list):
        merged["actions"] = data["actions"]
    if "explanation" in data:
        merged["explanation"] = data["explanation"]
    return merged
