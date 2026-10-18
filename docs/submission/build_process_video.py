"""Record DemoGraph Studio doing the whole process live, then build the narrated submission video.

    python docs/submission/build_process_video.py   -> docs/submission/demograph_submission.mp4

Needs the dashboard (python -m demograph ui http://127.0.0.1:8765) and the sample app running.
Sections: 3D explainer -> live dashboard process (replay, plan, release, watcher rebuild) -> a real demo video.
"""

import asyncio
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from demograph.recorder.media import ffmpeg, synthesize, wav_duration  # noqa: E402
from build_video import VOICE_A, segment  # noqa: E402

WORK = ROOT / "data" / "submission"
OUT = ROOT / "docs" / "submission"
DASH = "http://127.0.0.1:8800/"
PROCESS_S = 84.0  # length of the sped-up live section

VOICE_P = ("This is DemoGraph Studio, reading FalkorDB live. On the left, the pipeline and the seven agents. "
           "Replay exploration shows how three browser agents mapped the product, page by page, from the home page. "
           "Now we ask for a demo: create an invoice. Full-text search finds the button, and the weighted shortest path "
           "lights up the route, with every scene citing real page elements. "
           "Next, we ship a release that renames a button, and turn on Watch. "
           "The agents re-check every page. Graph memory makes this fast, because unchanged pages are confirmed, not re-read. "
           "The release is detected, the demo turns red, and the change travels to its video and guide. "
           "The watcher re-plans the demo on the same element, and rebuilds the video, the guide and the tests, automatically.")


def state() -> dict:
    return json.load(urllib.request.urlopen(DASH + "api/state"))


async def record_process(path: Path) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--disable-gpu"])
        ctx = await browser.new_context(viewport={"width": 1440, "height": 810}, record_video_dir=str(WORK),
                                        record_video_size={"width": 1440, "height": 810})
        page = await ctx.new_page()
        await page.goto(DASH)
        await page.wait_for_timeout(3000)
        await page.click("#replayBtn")
        await page.wait_for_timeout(10000)
        await page.fill("#goal", "create an invoice")
        await page.click("#planBtn")
        await page.wait_for_timeout(9000)
        started = time.time()
        await page.click("#releaseBtn")
        await page.wait_for_timeout(1500)
        await page.click("#watchBtn")
        rebuilt_at = None
        while time.time() - started < 240:
            s = state()
            if not rebuilt_at and any(u["at"] / 1000 > started for u in s["updates"]):
                rebuilt_at = time.time()
                print("rebuild seen after", round(rebuilt_at - started), "s", flush=True)
            if rebuilt_at and time.time() - rebuilt_at > 12:
                break
            await asyncio.sleep(1)
        video = page.video
        await ctx.close()
        await browser.close()
        shutil.move(await video.path(), path)


async def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    va, vp, vb = await asyncio.gather(synthesize(VOICE_A), synthesize(VOICE_P),
                                      synthesize("Here is a real video DemoGraph produced on its own, every step verified against the live product."))
    raw = WORK / "process.webm"
    await record_process(raw)
    probe = WORK / "process_norm.mp4"
    ffmpeg("-i", raw, "-vf", "fps=30", "-an", probe)
    import subprocess
    from demograph.recorder.media import ffmpeg_exe
    info = subprocess.run([ffmpeg_exe(), "-i", str(probe)], capture_output=True, text=True).stderr
    h, m, s = info.split("Duration: ")[1].split(",")[0].split(":")
    raw_s = int(h) * 3600 + int(m) * 60 + float(s)
    target = max(PROCESS_S, vp.duration + 2)
    speed = max(1.0, raw_s / target)
    fast = WORK / "process_fast.mp4"
    ffmpeg("-i", probe, "-vf", f"setpts=PTS/{speed:.3f},scale=1280:720", "-r", "30", fast)
    print("raw", round(raw_s), "s, speed", round(speed, 2), "x", flush=True)

    a, p, b1, b2 = (WORK / n for n in ("a.mp4", "p.mp4", "b1.mp4", "b2.mp4"))
    segment(ROOT / "docs" / "animation" / "explainer.mp4", va.path, a, max(va.duration + 1, 50))
    segment(fast, vp.path, p, target)
    demo = ROOT / "docs" / "proof-run" / "create-an-invoice.mp4"
    segment(demo, vb.path, b1, vb.duration + 0.4)
    segment(demo, None, b2, 0)
    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{x.as_posix()}'\n" for x in (a, p, b1, b2)), encoding="utf-8")
    out = OUT / "demograph_submission.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out)
    print("wrote", out)


if __name__ == "__main__":
    asyncio.run(main())
