"""Render explainer.html to MP4 frame by frame (smooth regardless of machine speed).

    python docs/animation/capture.py            -> docs/animation/explainer.mp4
"""

import asyncio
import subprocess
import sys
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from demograph.recorder.media import ffmpeg_exe  # noqa: E402

HERE = Path(__file__).resolve().parent
FPS, DURATION = 30, 50.0


async def main() -> None:
    out = HERE / "explainer.mp4"
    cmd = [ffmpeg_exe(), "-v", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        page = await browser.new_page(viewport={"width": 1280, "height": 720})
        await page.goto((HERE / "explainer.html").as_uri() + "?capture")
        await page.wait_for_function("window.__ready === true", timeout=60_000)
        stage = page.locator("#stage")
        total = int(DURATION * FPS)
        for f in range(total):
            await page.evaluate("t => window.__step(t)", f / FPS)
            proc.stdin.write(await stage.screenshot(type="jpeg", quality=92))
            if f % 150 == 0:
                print(f"frame {f}/{total}", flush=True)
        await browser.close()
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed")
    print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
