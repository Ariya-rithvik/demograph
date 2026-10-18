"""Recorder: replay a planned demo in a real browser and render a narrated MP4.

    graph check -> voiceover -> live replay (+ checks) -> render -> Video node in the graph

Nothing is rendered if the graph says a citation is stale or the live product
disagrees with what a scene cites.
"""

import asyncio
import datetime as dt
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .. import queries as Q
from ..config import settings
from ..db import GraphStore
from ..ids import origin
from ..planner import product_name
from ..schema import apply_schema
from .media import DEFAULT_VOICE, build_track, synthesize
from .render import FPS, OUTRO_S, TITLE_S, VOICE_LEAD_S, SceneSlot, card, render, write_srt
from .replay import Replayer


class RecordError(Exception):
    pass


@dataclass
class RecordResult:
    video_id: str
    path: Path
    duration: float
    timeline: list[dict]
    srt: Path
    poster: Path
    raw: Path | None
    voice: str
    citations: int
    doc_id: str | None = None
    guide: Path | None = None
    guide_html: Path | None = None


async def load_demo(store: GraphStore, demo_id: str) -> tuple[dict, list[dict]]:
    info = await store.one(Q.DEMO_INFO, {"demo_id": demo_id})
    if not info:
        raise RecordError(f"No demo {demo_id!r} in graph {store.name}.")
    rows = await store.query(Q.DEMO_SCENES, {"demo_id": demo_id})
    scenes = [{"id": r[0], "index": r[1], "kind": r[2], "narration": r[3], "steps": r[4], "url": r[5],
               "page_path": r[6], "page_title": r[7], "cites": [c for c in r[8] if c["fact_id"]],
               "full_title": r[9]} for r in rows]
    return {"goal": info[0], "start_path": info[1], "crawl": info[2], "target_text": info[3]}, scenes


def stale_citations(scenes: list[dict]) -> list[str]:
    problems = []
    for s in scenes:
        for c in s["cites"]:
            if c["removed"]:
                problems.append(f"scene {s['index']} cites “{c['text']}”, which the latest crawl no longer found")
            elif c["hash"] != c["current"]:
                problems.append(f"scene {s['index']} cites “{c['text']}”, which has changed since the demo was planned")
    return problems


async def record_demo(store: GraphStore, root_url: str, demo_id: str, voice: str = DEFAULT_VOICE,
                      engine: str = "auto", headless: bool = True, scale: int = 2,
                      log=print, guide: bool = True) -> RecordResult:
    await apply_schema(store)
    meta, scenes = await load_demo(store, demo_id)
    problems = stale_citations(scenes)
    if problems:
        raise RecordError("Not recording a stale demo (plan it again):\n  " + "\n  ".join(problems))
    site = origin(root_url)
    crawl = await store.one(Q.LATEST_COMPLETED_CRAWL, {"site_id": site})
    if not crawl:
        raise RecordError("No completed crawl to verify against.")

    log(f"Voiceover for {len(scenes)} scenes ({'silent' if engine == 'silent' else voice})")
    voices = await asyncio.gather(*(synthesize(s["narration"], voice, engine) for s in scenes))

    out_dir = settings.data_dir / "videos" / store.name / demo_id
    out_dir.mkdir(parents=True, exist_ok=True)
    log("Replaying the demo in Chromium and checking every citation on the live page")
    replayer = Replayer(scale=scale, headless=headless, raw_video_dir=out_dir)
    captures = await replayer.run(scenes)
    live_problems = [p for c in captures for p in c.problems]
    if live_problems or len(captures) != len(scenes):
        raise RecordError("The live product does not match the plan:\n  " + "\n  ".join(live_problems))

    doc_id = md = html_path = None
    if guide:  # the documentation agent writes the step-by-step guide from this same verified replay
        from ..documenter import build_guide, save_guide
        md, html_path, _ = build_guide(captures, scenes, meta, root_url, crawl[0],
                                       settings.data_dir / "docs" / store.name / demo_id)
        doc_id = await save_guide(store, demo_id, crawl[0], md, html_path,
                                  md.read_text(encoding="utf-8").splitlines()[0][2:], scenes)
        log(f"Guide written: {html_path.as_posix()}")

    slots = [SceneSlot(capture=c, duration=round(max(2.4, v.duration + 0.8) * FPS) / FPS, voice=v.path)
             for c, v in zip(captures, voices)]
    audio = out_dir / "voiceover.wav"
    build_track([(0.0, None, TITLE_S)] + [(VOICE_LEAD_S, s.voice, s.duration) for s in slots]
                + [(0.0, None, OUTRO_S)], audio)

    product = product_name(scenes[0]["full_title"], urlsplit(root_url).netloc)
    route = []
    for s in scenes:
        if not route or route[-1] != s["page_path"]:
            route.append(s["page_path"])
    citations = sum(len(s["cites"]) for s in scenes)
    title = card(f"{product} · product demo", meta["target_text"] or meta["goal"],
                 [f"Planned from {product}'s knowledge graph in FalkorDB",
                  "Route: " + "  →  ".join(route)])
    outro = card("Verified demo", "Every step checks out",
                 [f"{citations} citations, each pointing at a real element on the page",
                  f"Replayed live in Chromium against {urlsplit(root_url).netloc} on {dt.date.today():%d %b %Y}",
                  "Built with DemoGraph + FalkorDB"])

    log("Rendering")
    mp4 = out_dir / f"{demo_id}.mp4"
    poster = out_dir / "poster.jpg"
    result = await asyncio.to_thread(render, slots, mp4, audio, title, outro, poster)
    srt = out_dir / f"{demo_id}.srt"
    write_srt(result.timeline, slots, srt)
    audio.unlink(missing_ok=True)

    video_id = f"{demo_id}-video-{secrets.token_hex(2)}"
    voice_used = sorted({v.engine for v in voices})
    await store.query(Q.SAVE_VIDEO, {
        "demo_id": demo_id, "crawl_id": crawl[0], "video_id": video_id, "path": mp4.as_posix(),
        "duration": result.duration, "now": int(time.time() * 1000), "voice": ", ".join(voice_used),
        "raw": replayer.raw_video.as_posix() if replayer.raw_video else None, "srt": srt.as_posix(),
        "verified": True, "timeline": result.timeline})
    return RecordResult(video_id=video_id, path=mp4, duration=result.duration, timeline=result.timeline,
                        srt=srt, poster=poster, raw=replayer.raw_video, voice=", ".join(voice_used),
                        citations=citations, doc_id=doc_id, guide=md, guide_html=html_path)
