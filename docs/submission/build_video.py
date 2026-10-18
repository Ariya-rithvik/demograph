"""Build the hackathon submission video from real artifacts, with a neural voiceover.

    python docs/submission/build_video.py   -> docs/submission/demograph_submission.mp4

1. 3D explainer (docs/animation/explainer.mp4) + narration
2. live recording of the dashboard (replay exploration, plan a route) + narration
3. a real demo video DemoGraph rendered (docs/proof-run/create-an-invoice.mp4, its own voice)
"""

import asyncio
import shutil
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from demograph.recorder.media import ffmpeg, synthesize, wav_duration  # noqa: E402

OUT = ROOT / "docs" / "submission"
WORK = ROOT / "data" / "submission"
DASH = "http://127.0.0.1:8800/"

VOICE_A = ("Product demo videos go out of date the moment a product changes. DemoGraph fixes that with FalkorDB. "
           "Explorer agents open the product in a real browser and share one crawl queue stored in the graph. "
           "Every page becomes a node, and every click becomes an edge. "
           "To show a feature, DemoGraph finds it with full-text search, and plans the route with FalkorDB's weighted shortest path. "
           "It records a narrated video, where every sentence cites a real element on the page. "
           "When a release renames a button, the graph traces the change to the exact seconds of the video that are now wrong.")
VOICE_C = ("This is DemoGraph Studio, reading FalkorDB live. Replay exploration shows how three agents mapped the product, page by page. "
           "Seven agents work on one graph: explorer, knowledge graph, demo, documentation, QA, release intelligence, and an orchestrator. "
           "Plan route searches the graph and lights up the shortest path. "
           "With Watch on, a release is detected, and the stale demo, its video, guide and tests are rebuilt automatically.")
VOICE_B_INTRO = "Here is a real video DemoGraph produced on its own, every step verified against the live product."


async def record_dashboard(path: Path, seconds: float) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=["--disable-gpu"])
        ctx = await browser.new_context(viewport={"width": 1280, "height": 720},
                                        record_video_dir=str(WORK), record_video_size={"width": 1280, "height": 720})
        page = await ctx.new_page()
        await page.goto(DASH)
        await page.wait_for_timeout(2500)
        await page.click("#replayBtn")
        await page.wait_for_timeout(int(seconds * 1000 * 0.55))
        await page.fill("#goal", "create an invoice")
        await page.click("#planBtn")
        await page.wait_for_timeout(int(seconds * 1000 * 0.45))
        video = page.video
        await ctx.close()
        await browser.close()
        shutil.move(await video.path(), path)


def segment(video: Path, voice: Path | None, out: Path, length: float, start: float = 0.0) -> None:
    vf = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p"
    if voice:
        ffmpeg("-ss", start, "-t", length, "-i", video, "-i", voice, "-filter_complex",
               f"[0:v]{vf}[v];[1:a]apad,atrim=0:{length},aresample=44100[a]", "-map", "[v]", "-map", "[a]",
               "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-ac", "2", out)
    else:
        ffmpeg("-i", video, "-vf", vf, "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
               "-ar", "44100", "-ac", "2", out)


async def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    va, vc, vb = await asyncio.gather(*(synthesize(t) for t in (VOICE_A, VOICE_C, VOICE_B_INTRO)))
    print("voice", va.engine, round(va.duration, 1), round(vc.duration, 1), round(vb.duration, 1))

    a = WORK / "a.mp4"
    segment(ROOT / "docs" / "animation" / "explainer.mp4", va.path, a, max(va.duration + 1, 50))

    dash_raw = WORK / "dashboard.webm"
    await record_dashboard(dash_raw, vc.duration + 3)
    c = WORK / "c.mp4"
    segment(dash_raw, vc.path, c, vc.duration + 1.5, start=1.5)

    # short spoken intro over the demo's title card, then the demo with its own narration
    demo = ROOT / "docs" / "proof-run" / "create-an-invoice.mp4"
    b1 = WORK / "b1.mp4"
    segment(demo, vb.path, b1, vb.duration + 0.4)
    b2 = WORK / "b2.mp4"
    segment(demo, None, b2, 0)

    lst = WORK / "list.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in (a, c, b1, b2)), encoding="utf-8")
    out = OUT / "demograph_submission.mp4"
    ffmpeg("-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
           "-movflags", "+faststart", out)
    print("wrote", out)


if __name__ == "__main__":
    asyncio.run(main())
