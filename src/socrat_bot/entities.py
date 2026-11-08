"""Native Telegram entities; all wire offsets are UTF-16 code units.

Prefix-crossing style entities are clipped. Semantic entities (links, mentions,
custom emoji, etc.) crossing a prefix are dropped, never retargeted.
"""
import json
import re

STYLES = {"bold", "italic", "underline", "strikethrough", "spoiler", "code", "pre", "blockquote", "expandable_blockquote"}


def units(text):
    return len(text.encode("utf-16-le")) // 2


def normalize(text, entities=None):
    if not text or units(text) > 4096:
        raise ValueError("Text must contain 1–4096 UTF-16 units.")
    boundaries = {0}
    offset = 0
    for char in text:
        offset += units(char)
        boundaries.add(offset)
    result = []
    for entity in entities or []:
        value = entity.to_dict() if hasattr(entity, "to_dict") else dict(entity)
        value = json.loads(json.dumps(value, ensure_ascii=False))
        start, length = value.get("offset"), value.get("length")
        if type(start) is not int or type(length) is not int or length <= 0 or start not in boundaries or start + length not in boundaries:
            raise ValueError("Invalid UTF-16 entity boundary.")
        result.append(value)
    return result


def command_body(message, tokens=1):
    """Strip command and optional UUID, preserving body whitespace/newlines."""
    raw = message.text or ""
    match = re.match(r"\s*\S+" + r"\s+\S+" * (tokens - 1) + r"\s*", raw)
    end = match.end() if match else len(raw)
    body = raw[end:]
    cut = units(raw[:end])
    entities = []
    for entity in getattr(message, "entities", None) or []:
        value = entity.to_dict() if hasattr(entity, "to_dict") else dict(entity)
        start, stop = value["offset"], value["offset"] + value["length"]
        if stop <= cut:
            continue
        if start < cut and value["type"] not in STYLES:
            continue
        value["offset"] = max(start, cut) - cut
        value["length"] = stop - max(start, cut)
        entities.append(value)
    return body, normalize(body, entities) if body else []
