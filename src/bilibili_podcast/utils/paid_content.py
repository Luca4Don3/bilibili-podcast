PAID_FIELDS = (
    "is_upower_exclusive",
    "is_chargeable_season",
    "pay",
    "is_pay",
    "need_pay",
    "charge_paid",
    "is_charging_arc",
    "elec_arc_type",
)

PAID_TEXT_MARKERS = (
    "充电专属",
    "付费",
    "抢先看",
    "大会员专享",
)


def iter_paid_field_values(info: dict):
    stack = [info]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                if key in PAID_FIELDS:
                    yield value
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(value for value in item if isinstance(value, (dict, list)))


def is_paid_content(info: dict) -> bool:
    """Return True when Bilibili metadata marks a video as paid/charging-only."""
    for value in iter_paid_field_values(info):
        if isinstance(value, str):
            value = value.strip().lower()
            if value in ("", "0", "false", "none", "no"):
                continue
        if value not in (False, 0, None):
            return True
    browser_text = str(info.get("raw", {}).get("browser_text", ""))
    if browser_text and any(marker in browser_text for marker in PAID_TEXT_MARKERS):
        return True
    # Fallback: check title for paid text markers (space API may not return paid fields)
    title = str(info.get("title", ""))
    if any(marker in title for marker in PAID_TEXT_MARKERS):
        return True
    return False


def has_paid_state(info: dict) -> bool:
    return any(True for _ in iter_paid_field_values(info)) or bool(
        info.get("raw", {}).get("browser_text")
    )
