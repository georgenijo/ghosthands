"""Vision contender — a screenshot+pixel loop driven by a local grounding VLM.

This is the *fallback* modality (DESIGN §3 / the goal's "no-AX surfaces"): no
accessibility tree, just pixels. Each turn it screenshots the window, asks a
local MLX vision model (default mlx-community/MAI-UI-8B-4bit) where to click for
the goal, converts the model's NORMALISED 0–1000 coordinate to window-local
screenshot pixels, and issues a pixel `click`.

It exists mainly to MEASURE the AX-vs-pixel gap in the benchmark: on this machine
pixel/CGEvent clicks do not reliably register on background windows (see
skills/SKILL.md §7 and README findings), so this contender is expected to under-
perform the AX path — that contrast is the point. Screen Recording must be
granted for the screenshot (`som`) capture.

mlx_vlm is imported lazily so the rest of the package stays stdlib-only.
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import time
from pathlib import Path

from . import driver
from .actions import App

DEFAULT_MODEL = "mlx-community/MAI-UI-8B-4bit"
SETTLE_SECONDS = 0.4

# MAI-UI emits malformed JSON; parse coordinates with regex (session finding).
PROMPT_TMPL = (
    "You are a GUI agent operating a macOS window by clicking. Look at the "
    "screenshot and decide the SINGLE next click that advances this goal:\n"
    "GOAL: {goal}\n\n"
    "If the goal already appears complete, reply exactly DONE. Otherwise reply "
    "with ONLY the click location as JSON {{\"x\": <int>, \"y\": <int>}} using a "
    "0-1000 normalized coordinate system (x and y each from 0 to 1000, relative "
    "to the image width and height)."
)


class VisionBrain:
    """Local MLX grounding VLM: screenshot + goal -> normalized click coord."""

    name = "mai-ui-pixel"

    def __init__(self, model: str | None = None, *, max_tokens: int = 64):
        self.model_id = model or DEFAULT_MODEL
        self.max_tokens = max_tokens
        self._model = None
        self._processor = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from mlx_vlm import load
            self._model, self._processor = load(self.model_id)

    def decide(self, goal: str, image_path: str) -> tuple[float, float] | None:
        """Return (norm_x, norm_y) in 0-1000, or None when the model says DONE
        or emits nothing parseable."""
        self._ensure_loaded()
        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template

        config = getattr(self._model, "config", self._processor)
        prompt = PROMPT_TMPL.format(goal=goal)
        try:
            formatted = apply_chat_template(self._processor, config, prompt, num_images=1)
        except Exception:
            formatted = prompt
        result = generate(self._model, self._processor, formatted, image=[image_path],
                          max_tokens=self.max_tokens, temperature=0.0, verbose=False)
        raw = result.text if hasattr(result, "text") else str(result)
        if "DONE" in raw.upper():
            return None
        return _extract_xy(raw)


def _extract_xy(text: str) -> tuple[float, float] | None:
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if "x" in obj and "y" in obj:
            return float(obj["x"]), float(obj["y"])
    pairs = re.findall(r"\(?\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)?", text)
    if pairs:
        return float(pairs[0][0]), float(pairs[0][1])
    return None


def _capture(app: App) -> tuple[str, int, int]:
    """Snapshot the window as a PNG on disk; return (path, width, height)."""
    r = driver.call("get_window_state", {
        "pid": app.pid, "window_id": app.window_id, "capture_mode": "som",
    })
    b64 = r.get("screenshot_png_b64", "")
    if not b64:
        raise driver.DriverError("get_window_state", "no screenshot (Screen Recording?)")
    path = Path(tempfile.mkdtemp(prefix="ghosthands-vis-")) / "win.png"
    path.write_bytes(base64.b64decode(b64))
    return str(path), int(r.get("screenshot_width", 0)), int(r.get("screenshot_height", 0))


def run_vision_loop(brain: VisionBrain, goal: str, bundle_id: str, *, max_turns: int = 12,
                    urls: list[str] | None = None, title_contains: str | None = None,
                    done_check=None, on_action=None, log=print) -> bool:
    """Drive `bundle_id` toward `goal` by screenshot + pixel click. Stops on the
    model's DONE, on an external `done_check`, or after max_turns. `on_action`
    is called once per issued pixel click (for step counting)."""
    app = App.launch(bundle_id, urls=urls, title_contains=title_contains)
    log(f"[{brain.name}] pid={app.pid} window={app.window_id} goal={goal!r}")

    for turn in range(1, max_turns + 1):
        if done_check is not None and done_check():
            return True
        image_path, w, h = _capture(app)
        coord = brain.decide(goal, image_path)
        if coord is None:
            log(f"[{brain.name}] turn {turn}: model said DONE / no coord")
            return done_check() if done_check else True
        nx, ny = coord
        px, py = round(nx / 1000 * w), round(ny / 1000 * h)
        log(f"[{brain.name}] turn {turn}: norm=({nx:.0f},{ny:.0f}) -> px=({px},{py}) of {w}x{h}")
        try:
            driver.call("click", {"pid": app.pid, "window_id": app.window_id, "x": px, "y": py})
            if on_action is not None:
                on_action()
        except driver.DriverError as e:
            log(f"  ! pixel click rejected: {e}")
        time.sleep(SETTLE_SECONDS)

    if done_check is not None:
        return done_check()
    return False
