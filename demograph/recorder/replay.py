"""Replay a planned demo in a real browser and capture what each scene should show.

For every scene the replay:
  1. runs the steps that get to the scene's state (goto / wait / scroll),
  2. checks it is on the page the plan expects,
  3. checks every cited element on the live page says exactly what the scene cites,
  4. captures keyframes (before acting, after each form field, before the click),
  5. performs the scene's action (fill / select / click) and waits for the expected URL.

A mismatch anywhere is a reason not to ship the video, so it is reported, not ignored.
"""

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..explorer.extract import ELEMENTS_INFO_JS, FEEDBACK_INFO_JS
from ..ids import normalize_url

VIEWPORT = {"width": 1280, "height": 720}
PRE_STEPS = ("goto", "wait", "scroll_to")


@dataclass
class Mark:
    role: str  # shown | click | fill | result
    rect: list  # x, y, w, h in screenshot pixels


@dataclass
class Keyframe:
    png: bytes
    marks: list[Mark]


@dataclass
class SceneCapture:
    id: str
    index: int
    kind: str
    narration: str
    page_path: str
    cites: list[dict]
    frames: list[Keyframe] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


async def _settle(page: Page, ms: int = 2500) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=ms)
    except PlaywrightTimeout:
        pass


async def _wait_for_url(page: Page, expected: str, timeout_s: float = 8.0) -> bool:
    want = normalize_url(expected)
    for _ in range(int(timeout_s / 0.1)):
        if normalize_url(page.url) == want:
            await _settle(page)
            return True
        await asyncio.sleep(0.1)
    return False


class Replayer:
    def __init__(self, scale: int = 2, headless: bool = True, raw_video_dir: Path | None = None):
        self.scale = scale
        self.headless = headless
        self.raw_video_dir = raw_video_dir
        self.raw_video: Path | None = None

    async def run(self, scenes: list[dict]) -> list[SceneCapture]:
        captures: list[SceneCapture] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            opts = {"viewport": VIEWPORT, "device_scale_factor": self.scale, "accept_downloads": False}
            if self.raw_video_dir:
                self.raw_video_dir.mkdir(parents=True, exist_ok=True)
                opts.update(record_video_dir=str(self.raw_video_dir), record_video_size=VIEWPORT)
            ctx = await browser.new_context(**opts)
            page = await ctx.new_page()
            page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))
            try:
                for scene in scenes:
                    cap = SceneCapture(id=scene["id"], index=scene["index"], kind=scene["kind"],
                                       narration=scene["narration"], page_path=scene["page_path"], cites=scene["cites"])
                    captures.append(cap)
                    await self._scene(page, scene, cap)
                    if cap.problems:
                        break  # the rest of the replay would not be trustworthy
            finally:
                video = page.video
                await ctx.close()
                await browser.close()
                if video and self.raw_video_dir:
                    src = Path(await video.path())
                    dst = self.raw_video_dir / "raw-session.webm"
                    shutil.move(src, dst)
                    self.raw_video = dst
        return captures

    async def _scene(self, page: Page, scene: dict, cap: SceneCapture) -> None:
        steps = json.loads(scene["steps"]) if isinstance(scene["steps"], str) else scene["steps"]
        for st in (s for s in steps if s["do"] in PRE_STEPS):
            if st["do"] == "goto":
                await page.goto(st["url"], wait_until="domcontentloaded", timeout=20_000)
                await _settle(page)
            elif st["do"] == "wait":
                await page.wait_for_timeout(min(int(st.get("ms", 500)), 800))
            elif st["do"] == "scroll_to":
                await page.locator(st["selector"]).first.scroll_into_view_if_needed(timeout=5000)

        if normalize_url(page.url) != normalize_url(scene["url"]):
            cap.problems.append(f"scene {cap.index}: browser is on {page.url}, plan expects {scene['url']}")
            return

        marks = await self._check_citations(page, cap)
        if cap.problems:
            return
        cap.frames.append(Keyframe(await self._shot(page), marks))

        actions = [s for s in steps if s["do"] not in PRE_STEPS]
        fills = [s for s in actions if s["do"] in ("fill", "select")]
        for st in fills:
            loc = page.locator(st["selector"]).first
            if st["do"] == "select":
                await loc.select_option(label=st["value"])
            elif await loc.evaluate("el => ['date', 'time', 'datetime-local', 'month', 'week', 'color', 'range']"
                                    ".includes(el.type)"):
                await loc.fill(str(st["value"]))  # pickers take ISO values, not keystrokes
                await loc.evaluate("el => el.blur()")
            else:
                await loc.fill("")
                await loc.press_sequentially(str(st["value"]), delay=25)
            cap.frames.append(Keyframe(await self._shot(page), await self._marks_for(page, cap, focus=st["selector"])))
        clicks = [s for s in actions if s["do"] == "click"]
        if fills and clicks:  # end the form scene on the button about to be clicked
            cap.frames.append(Keyframe(await self._shot(page), await self._marks_for(page, cap, focus=clicks[0]["selector"])))
        for st in clicks:
            await page.locator(st["selector"]).first.click(timeout=5000)
            if st.get("expect_url"):
                if not await _wait_for_url(page, st["expect_url"]):
                    cap.problems.append(f"scene {cap.index}: clicking led to {page.url}, plan expects {st['expect_url']}")
                    return
            else:
                await page.wait_for_timeout(450)

    async def _shot(self, page: Page) -> bytes:
        return await page.screenshot(type="png")

    def _px(self, rect: list) -> list:
        return [round(v * self.scale) for v in rect]

    async def _check_citations(self, page: Page, cap: SceneCapture) -> list[Mark]:
        """Every cited element must exist on the live page and say what the scene cites."""
        page_cites = [c for c in cap.cites if c["role"] != "result"]
        if page_cites:
            first = page_cites[0]["selector"]
            await page.locator(first).first.scroll_into_view_if_needed(timeout=5000)
            await page.evaluate("sel => { const el = document.querySelector(sel); "
                                "if (el) el.scrollIntoView({block: 'center'}); }", first)
        infos = await page.evaluate(ELEMENTS_INFO_JS, [c["selector"] for c in page_cites])
        marks = []
        for c, info in zip(page_cites, infos):
            if info is None:
                cap.problems.append(f"scene {cap.index}: cited {c['kind']} “{c['text']}” is not on the live page")
            elif info["text"] != c["text"]:
                cap.problems.append(f"scene {cap.index}: live page says “{info['text']}”, scene cites “{c['text']}”")
            else:
                marks.append(Mark(c["role"], self._px(info["rect"])))
        for c in (c for c in cap.cites if c["role"] == "result"):
            fb = await page.evaluate(FEEDBACK_INFO_JS)
            if not fb or fb["text"] != c["text"]:
                cap.problems.append(f"scene {cap.index}: expected the product to confirm “{c['text']}”, "
                                    f"it showed {('“' + fb['text'] + '”') if fb else 'nothing'}")
            else:
                marks.append(Mark("result", self._px(fb["rect"])))
                src = await page.evaluate(ELEMENTS_INFO_JS, [c["selector"]])
                if src and src[0]:
                    marks.append(Mark("shown", self._px(src[0]["rect"])))
        return marks

    async def _marks_for(self, page: Page, cap: SceneCapture, focus: str) -> list[Mark]:
        """Marks for a mid-scene keyframe: the element being worked on is the focus."""
        cites = [c for c in cap.cites if c["role"] != "result"]
        infos = await page.evaluate(ELEMENTS_INFO_JS, [c["selector"] for c in cites])
        marks = []
        for c, info in zip(cites, infos):
            if info:
                role = c["role"] if c["selector"] == focus else "context"
                marks.append(Mark(role, self._px(info["rect"])))
        return marks
