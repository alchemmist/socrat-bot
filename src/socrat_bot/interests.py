"""Registry of selectable capabilities; add future topics without user columns."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Interest:
    key: str
    label: str
    approval_required: bool = False


INTERESTS = (Interest("vpn", "VPN"), Interest("alerts", "Alerts", True), Interest("llm", "LLM"))
BY_KEY = {item.key: item for item in INTERESTS}
