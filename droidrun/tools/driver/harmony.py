"""HarmonyDriver - hdc/uitest based device driver for HarmonyOS.

This driver avoids third-party SDK dependencies and communicates through
command-line tools only:
- hdc shell uitest: touch/key/text/screenshot/layout
- hdc shell aa: app launch
- hdc shell bm: bundle install/list
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shlex
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from droidrun.tools.driver.base import DeviceDriver

logger = logging.getLogger("droidrun")


class HarmonyDriver(DeviceDriver):
    """Raw HarmonyOS device I/O via hdc + uitest."""

    supported = {
        "tap",
        "swipe",
        "input_text",
        "press_key",
        "start_app",
        "screenshot",
        "get_ui_tree",
        "get_date",
        "get_apps",
        "list_packages",
        "install_app",
        "drag",
    }

    def __init__(self, serial: str | None = None) -> None:
        self._serial = serial
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            return

        if shutil.which("hdc") is None:
            raise ConnectionError("hdc not found in PATH")

        if not self._serial:
            targets = await self._list_targets()
            if not targets:
                raise ConnectionError("No connected HarmonyOS devices found via hdc")
            self._serial = targets[0]

        await self._shell("echo hdc_ok")
        await self._safe_shell("uitest start-daemon")
        self._connected = True

    async def ensure_connected(self) -> None:
        if not self._connected:
            await self.connect()

    async def tap(self, x: int, y: int) -> None:
        await self.ensure_connected()
        await self._shell(f"uitest uiInput click {int(x)} {int(y)}")

    async def swipe(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: float = 1000,
    ) -> None:
        await self.ensure_connected()
        if int(x1) == int(x2) and int(y1) == int(y2):
            await self._shell(f"uitest uiInput longClick {int(x1)} {int(y1)}")
            await asyncio.sleep(max(duration_ms, 300) / 1000)
            return

        velocity = self._duration_to_velocity(x1, y1, x2, y2, duration_ms)
        await self._shell(
            f"uitest uiInput swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {velocity}"
        )
        await asyncio.sleep(max(duration_ms, 100) / 1000)

    async def input_text(self, text: str, clear: bool = False) -> bool:
        await self.ensure_connected()
        try:
            if clear:
                # Ctrl + A, then Delete (best effort).
                await self._shell("uitest uiInput keyEvent 2072 2017")
                await self._shell("uitest uiInput keyEvent 2055")

            escaped = shlex.quote(text)
            await self._shell(f"uitest uiInput text {escaped}")
            return True
        except Exception as e:
            logger.warning(f"Harmony input_text failed: {e}")
            return False

    async def press_key(self, keycode: int) -> None:
        await self.ensure_connected()
        harmony_key = {
            3: "Home",  # Android HOME -> Harmony Home
            4: "Back",  # Android BACK -> Harmony Back
            66: "2054",  # Android ENTER -> Harmony KEYCODE_ENTER
        }.get(keycode, str(keycode))
        await self._shell(f"uitest uiInput keyEvent {harmony_key}")

    async def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration: float = 3.0,
    ) -> None:
        await self.ensure_connected()
        velocity = self._duration_to_velocity(x1, y1, x2, y2, duration * 1000)
        await self._shell(
            f"uitest uiInput drag {int(x1)} {int(y1)} {int(x2)} {int(y2)} {velocity}"
        )
        await asyncio.sleep(max(duration, 0.2))

    async def start_app(self, package: str, activity: Optional[str] = None) -> str:
        await self.ensure_connected()
        try:
            if activity:
                output = await self._shell(
                    f"aa start -a {shlex.quote(activity)} -b {shlex.quote(package)}"
                )
                return output.strip() or f"App started: {package}/{activity}"

            # Try implicit launch first.
            output = await self._shell(f"aa start -b {shlex.quote(package)}")
            if self._looks_like_aa_start_success(output):
                return output.strip() or f"App started: {package}"

            # Fallback: resolve explicit ability from bm dump metadata.
            resolved = await self._resolve_launch_ability(package)
            if resolved:
                module_name, ability_name = resolved
                cmd = (
                    f"aa start -a {shlex.quote(ability_name)} -b {shlex.quote(package)}"
                )
                if module_name:
                    cmd += f" -m {shlex.quote(module_name)}"
                output2 = await self._shell(cmd)
                if self._looks_like_aa_start_success(output2):
                    return output2.strip() or f"App started: {package}/{ability_name}"

            return output.strip() or f"Failed to start app {package}"
        except Exception as e:
            return f"Failed to start app {package}: {e}"

    async def install_app(self, path: str, **kwargs) -> str:
        await self.ensure_connected()
        if not os.path.exists(path):
            return f"Failed to install app: file not found at {path}"

        remote = f"/data/local/tmp/{uuid.uuid4().hex}_{os.path.basename(path)}"
        reinstall = kwargs.get("reinstall", False)
        try:
            await self._run_checked(["file", "send", path, remote], timeout=180)
            cmd = f"bm install -p {shlex.quote(remote)}"
            if reinstall:
                cmd += " -r"
            result = await self._shell(cmd, timeout=180)
            return result.strip()
        except Exception as e:
            return f"Failed to install app {path}: {e}"
        finally:
            await self._safe_shell(f"rm -f {shlex.quote(remote)}")

    async def get_apps(self, include_system: bool = True) -> List[Dict[str, str]]:
        await self.ensure_connected()
        output = await self._shell("bm dump -a -l")
        apps = self._parse_apps(output)
        if not apps:
            packages = await self.list_packages(include_system=include_system)
            apps = [{"package": pkg, "label": pkg} for pkg in packages]

        if include_system:
            return apps
        return [
            app
            for app in apps
            if not app["package"].startswith(("com.ohos.", "ohos.", "com.huawei."))
        ]

    async def list_packages(self, include_system: bool = False) -> List[str]:
        await self.ensure_connected()
        output = await self._shell("bm dump -a")
        packages = self._parse_bundle_names(output)
        if include_system:
            return packages
        return [
            pkg
            for pkg in packages
            if not pkg.startswith(("com.ohos.", "ohos.", "com.huawei."))
        ]

    async def screenshot(self, hide_overlay: bool = True) -> bytes:
        await self.ensure_connected()
        remote = f"/data/local/tmp/droidrun_{uuid.uuid4().hex}.png"
        local = Path(tempfile.gettempdir()) / f"droidrun_{uuid.uuid4().hex}.png"
        try:
            await self._shell(f"uitest screenCap -p {remote}")
            await self._run_checked(
                ["file", "recv", remote, str(local)],
                timeout=120,
            )
            data = local.read_bytes()
            if not data:
                raise ValueError("empty screenshot data")
            return data
        finally:
            try:
                if local.exists():
                    local.unlink()
            except Exception:
                pass
            await self._safe_shell(f"rm -f {shlex.quote(remote)}")

    async def get_ui_tree(self) -> Dict[str, Any]:
        await self.ensure_connected()
        remote = f"/data/local/tmp/droidrun_layout_{uuid.uuid4().hex}.json"
        local = Path(tempfile.gettempdir()) / f"droidrun_layout_{uuid.uuid4().hex}.json"
        try:
            await self._shell(f"uitest dumpLayout -p {remote}")
            await self._run_checked(
                ["file", "recv", remote, str(local)],
                timeout=120,
            )
            payload = local.read_text(encoding="utf-8", errors="ignore")
            layout = self._parse_json_payload(payload)
            width, height = self._infer_screen_size(layout)
            return {
                "layout": layout,
                "phone_state": {
                    "currentApp": "Unknown",
                    "packageName": "Unknown",
                    "isEditable": False,
                },
                "screen_width": width,
                "screen_height": height,
            }
        finally:
            try:
                if local.exists():
                    local.unlink()
            except Exception:
                pass
            await self._safe_shell(f"rm -f {shlex.quote(remote)}")

    async def get_date(self) -> str:
        await self.ensure_connected()
        return (await self._shell("date")).strip()

    async def _list_targets(self) -> List[str]:
        rc, out, err = await self._run_hdc(["list", "targets"], with_target=False)
        if rc != 0:
            raise ConnectionError(f"hdc list targets failed: {err or out}")

        targets: List[str] = []
        for raw in out.splitlines():
            line = raw.strip()
            if (
                not line
                or line.lower().startswith("empty")
                or line.startswith("[")
                or "usb:" in line.lower()
            ):
                continue
            targets.append(line)
        return targets

    async def _shell(self, cmd: str, timeout: int = 60) -> str:
        return await self._run_checked(["shell", cmd], timeout=timeout)

    async def _safe_shell(self, cmd: str) -> None:
        try:
            await self._run_checked(["shell", cmd], timeout=15)
        except Exception:
            pass

    async def _run_checked(
        self,
        args: List[str],
        *,
        timeout: int = 60,
        with_target: bool = True,
    ) -> str:
        rc, out, err = await self._run_hdc(
            args,
            timeout=timeout,
            with_target=with_target,
        )
        if rc != 0:
            cmd = " ".join(self._hdc_cmd(args, with_target=with_target))
            detail = err.strip() or out.strip() or f"exit code {rc}"
            raise RuntimeError(f"{cmd} failed: {detail}")
        return out

    async def _run_hdc(
        self,
        args: List[str],
        *,
        timeout: int = 60,
        with_target: bool = True,
    ) -> tuple[int, str, str]:
        cmd = self._hdc_cmd(args, with_target=with_target)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise RuntimeError(f"Command timed out: {' '.join(cmd)}")

        out = out_b.decode("utf-8", errors="ignore")
        err = err_b.decode("utf-8", errors="ignore")
        return proc.returncode or 0, out, err

    def _hdc_cmd(self, args: List[str], *, with_target: bool = True) -> List[str]:
        cmd = ["hdc"]
        if with_target and self._serial:
            cmd.extend(["-t", self._serial])
        cmd.extend(args)
        return cmd

    @staticmethod
    def _duration_to_velocity(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration_ms: float,
    ) -> int:
        if duration_ms <= 0:
            return 600
        distance = max(1.0, math.hypot(float(x2 - x1), float(y2 - y1)))
        velocity = int(distance / (duration_ms / 1000.0))
        return max(200, min(40000, velocity))

    @staticmethod
    def _parse_json_payload(payload: str) -> Dict[str, Any]:
        text = payload.strip()
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {"root": data}
        except json.JSONDecodeError:
            pass

        # Fallback: extract the first JSON object from command-noise text.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
            return data if isinstance(data, dict) else {"root": data}
        except json.JSONDecodeError:
            return {}

    @classmethod
    def _infer_screen_size(cls, layout: Dict[str, Any]) -> tuple[int, int]:
        max_right = 0
        max_bottom = 0
        for node in cls._iter_nodes(layout):
            bounds = cls._extract_bounds(node)
            if not bounds:
                continue
            _, _, right, bottom = bounds
            max_right = max(max_right, right)
            max_bottom = max(max_bottom, bottom)
        return max_right or 1080, max_bottom or 2400

    @classmethod
    def _iter_nodes(cls, obj: Any):
        if isinstance(obj, dict):
            yield obj
            for value in obj.values():
                yield from cls._iter_nodes(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from cls._iter_nodes(item)

    @staticmethod
    def _extract_bounds(node: Dict[str, Any]) -> tuple[int, int, int, int] | None:
        # Format 1: bounds as dict: {left, top, right, bottom}
        bounds = node.get("bounds")
        if isinstance(bounds, dict):
            keys = ("left", "top", "right", "bottom")
            if all(k in bounds for k in keys):
                return tuple(int(bounds[k]) for k in keys)  # type: ignore[return-value]

        # Format 2: rect as dict: {left, top, width, height}
        rect = node.get("rect")
        if isinstance(rect, dict) and all(k in rect for k in ("left", "top")):
            left = int(rect.get("left", 0))
            top = int(rect.get("top", 0))
            width = int(rect.get("width", 0))
            height = int(rect.get("height", 0))
            return left, top, left + width, top + height

        # Format 3: string "x1,y1,x2,y2" or "[x1,y1][x2,y2]"
        raw = node.get("bounds") or node.get("bound") or node.get("frame")
        if isinstance(raw, str):
            nums = re.findall(r"-?\d+", raw)
            if len(nums) >= 4:
                x1, y1, x2, y2 = map(int, nums[:4])
                if x2 >= x1 and y2 >= y1:
                    return x1, y1, x2, y2
        return None

    @staticmethod
    def _parse_bundle_names(output: str) -> List[str]:
        pattern = re.compile(
            r'"(?:bundleName|name)"\s*:\s*"([A-Za-z0-9_.]+)"',
            re.IGNORECASE,
        )
        names = [m.group(1) for m in pattern.finditer(output)]
        if not names:
            # Fallback: parse package-like tokens
            names = re.findall(r"\b[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){2,}\b", output)
        dedup: List[str] = []
        seen: set[str] = set()
        for name in names:
            if name not in seen:
                seen.add(name)
                dedup.append(name)
        return dedup

    @staticmethod
    def _looks_like_aa_start_success(output: str) -> bool:
        text = (output or "").lower()
        return "start ability successfully" in text or "start ability for result ok" in text

    async def _resolve_launch_ability(
        self, package: str
    ) -> tuple[str | None, str | None]:
        try:
            dump = await self._shell(f"bm dump -n {shlex.quote(package)}")
        except Exception:
            return None, None

        module_name: str | None = None
        ability_name: str | None = None

        m_mod = re.search(r'"mainEntry"\s*:\s*"([^"]+)"', dump)
        if m_mod:
            module_name = m_mod.group(1).strip()

        m_ability = re.search(r'"mainAbility"\s*:\s*"([^"]+)"', dump)
        if m_ability:
            ability_name = m_ability.group(1).strip()

        if not ability_name:
            m_ability2 = re.search(r'"mainElementName"\s*:\s*"([^"]+)"', dump)
            if m_ability2:
                ability_name = m_ability2.group(1).strip()

        if not ability_name:
            # Fallback to first ability entry.
            m_any = re.search(
                r'"abilityInfos"\s*:\s*\[\s*\{[\s\S]*?"name"\s*:\s*"([^"]+)"',
                dump,
            )
            if m_any:
                ability_name = m_any.group(1).strip()

        return module_name, ability_name

    @classmethod
    def _parse_apps(cls, output: str) -> List[Dict[str, str]]:
        packages = cls._parse_bundle_names(output)
        labels = re.findall(r'"label"\s*:\s*"([^"]*)"', output, flags=re.IGNORECASE)
        if not packages:
            return []

        apps: List[Dict[str, str]] = []
        for idx, package in enumerate(packages):
            label = labels[idx] if idx < len(labels) and labels[idx] else package
            apps.append({"package": package, "label": label})
        return apps
