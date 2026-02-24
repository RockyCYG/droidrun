"""HarmonyStateProvider - builds UIState from HarmonyOS uitest layout dumps."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

from droidrun.tools.driver.base import DeviceDriver
from droidrun.tools.ui.provider import StateProvider
from droidrun.tools.ui.state import UIState

logger = logging.getLogger("droidrun")

_BOUNDS_KEYS = ("left", "top", "right", "bottom")
_CHILD_KEYS = (
    "children",
    "child",
    "nodes",
    "elements",
    "componentTree",
    "components",
    "subNodes",
)
_TEXT_KEYS = ("text", "label", "content", "description", "value", "hint", "title")
_TYPE_KEYS = ("type", "className", "componentType", "widgetType", "name")
_ID_KEYS = ("id", "resourceId", "componentId", "identifier", "key")


class HarmonyStateProvider(StateProvider):
    """Produces ``UIState`` from a HarmonyOS uitest layout dump."""

    supported = {"element_index", "convert_point"}

    def __init__(self, driver: DeviceDriver, use_normalized: bool = False) -> None:
        super().__init__(driver)
        self.use_normalized = use_normalized

    async def get_state(self) -> UIState:
        raw = await self.driver.get_ui_tree()
        layout = raw.get("layout", {})
        phone_state = raw.get("phone_state", {})

        elements = _parse_layout(layout)

        screen_width = int(raw.get("screen_width") or 0)
        screen_height = int(raw.get("screen_height") or 0)
        if screen_width <= 0 or screen_height <= 0:
            screen_width, screen_height = _infer_screen_size(elements)

        focused_text = ""
        focused = phone_state.get("focusedElement")
        if isinstance(focused, dict):
            focused_text = str(focused.get("text") or "")

        formatted_text = _format_elements(elements)

        return UIState(
            elements=elements,
            formatted_text=formatted_text,
            focused_text=focused_text,
            phone_state=phone_state,
            screen_width=screen_width,
            screen_height=screen_height,
            use_normalized=self.use_normalized,
        )


def _parse_layout(layout: Dict[str, Any]) -> List[Dict[str, Any]]:
    elements: List[Dict[str, Any]] = []
    seen: set[Tuple[int, int, int, int, str, str]] = set()

    for node in _iter_nodes(layout):
        if not isinstance(node, dict):
            continue

        bounds = _extract_bounds(node)
        if not bounds:
            continue

        left, top, right, bottom = bounds
        if right <= left or bottom <= top:
            continue

        class_name = _extract_first(node, _TYPE_KEYS)
        if class_name:
            class_name = class_name.split(".")[-1]
        resource_id = _extract_first(node, _ID_KEYS)
        text = _extract_first(node, _TEXT_KEYS)

        if not class_name and not resource_id and not text:
            continue

        signature = (left, top, right, bottom, class_name, text)
        if signature in seen:
            continue
        seen.add(signature)

        elements.append(
            {
                "index": len(elements) + 1,
                "resourceId": resource_id,
                "className": class_name,
                "text": text or resource_id or class_name,
                "bounds": f"{left},{top},{right},{bottom}",
                "children": [],
            }
        )

    return elements


def _iter_nodes(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for key in _CHILD_KEYS:
            value = obj.get(key)
            if value is not None:
                yield from _iter_nodes(value)
        for key, value in obj.items():
            if key not in _CHILD_KEYS:
                yield from _iter_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_nodes(item)


def _extract_first(node: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ""


def _extract_bounds(node: Dict[str, Any]) -> Tuple[int, int, int, int] | None:
    bounds = node.get("bounds")
    if isinstance(bounds, dict) and all(k in bounds for k in _BOUNDS_KEYS):
        return (
            int(bounds["left"]),
            int(bounds["top"]),
            int(bounds["right"]),
            int(bounds["bottom"]),
        )

    rect = node.get("rect")
    if isinstance(rect, dict) and "left" in rect and "top" in rect:
        left = int(rect.get("left", 0))
        top = int(rect.get("top", 0))
        width = int(rect.get("width", 0))
        height = int(rect.get("height", 0))
        return left, top, left + width, top + height

    raw = node.get("bounds") or node.get("bound") or node.get("frame")
    if isinstance(raw, str):
        nums = re.findall(r"-?\d+", raw)
        if len(nums) >= 4:
            x1, y1, x2, y2 = map(int, nums[:4])
            return x1, y1, x2, y2

    # Fallback coordinate quartet in separate fields.
    if all(k in node for k in ("x", "y", "width", "height")):
        x = int(node.get("x", 0))
        y = int(node.get("y", 0))
        w = int(node.get("width", 0))
        h = int(node.get("height", 0))
        return x, y, x + w, y + h

    return None


def _infer_screen_size(elements: List[Dict[str, Any]]) -> Tuple[int, int]:
    max_right = 0
    max_bottom = 0
    for el in elements:
        bounds = el.get("bounds", "")
        if not bounds:
            continue
        parts = bounds.split(",")
        if len(parts) != 4:
            continue
        max_right = max(max_right, int(parts[2]))
        max_bottom = max(max_bottom, int(parts[3]))
    return max_right or 1080, max_bottom or 2400


def _format_elements(elements: List[Dict[str, Any]]) -> str:
    schema = "'index. className: resourceId, text - bounds(x1,y1,x2,y2)'"
    if not elements:
        return f"Current Clickable UI elements:\n{schema}:\nNo UI elements found"

    lines = [f"Current Clickable UI elements:\n{schema}:"]
    for el in elements:
        idx = el.get("index", "")
        cls = el.get("className", "")
        resource_id = el.get("resourceId", "")
        text = el.get("text", "")
        bounds = el.get("bounds", "")

        details: List[str] = []
        if resource_id:
            details.append(f'"{resource_id}"')
        if text and text != resource_id:
            details.append(f'"{text}"')

        parts = [f"{idx}.", f"{cls}:"]
        if details:
            parts.append(", ".join(details))
        if bounds:
            parts.append(f"- ({bounds})")
        lines.append(" ".join(parts))

    return "\n".join(lines)
