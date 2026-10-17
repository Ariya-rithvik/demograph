"""Recorder tests: replay a planned demo live, render the MP4, and refuse anything unverifiable.

Voiceover is silent here so the tests do not need the network; timing and captions still apply.
"""

import asyncio
import re
import subprocess

import pytest

from conftest import crawl, q
from demograph import queries as Q
from demograph.db import GraphStore
from demograph.planner import Planner
from demograph.recorder import RecordError, record_demo
from demograph.recorder.media import ffmpeg_exe
from demograph.recorder.render import OUTRO_S, TITLE_S


def plan_and_save(url: str, graph: str, goal: str) -> str:
    async def run():
        store = GraphStore(graph)
        try:
            planner = Planner(store, url + "/")
            p = await planner.plan(goal)
            await planner.save(p)
            return p.id
        finally:
            await store.close()
    return asyncio.run(run())


def record(url: str, graph: str, demo_id: str):
    async def run():
        store = GraphStore(graph)
        try:
            return await record_demo(store, url + "/", demo_id, engine="silent", scale=1, log=lambda _: None)
        finally:
            await store.close()
    return asyncio.run(run())


def probe(path) -> str:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True).stderr


def test_records_a_verified_narrated_video(crawled):
    app, graph = crawled
    demo = plan_and_save(app.url, graph, "export csv")
    res = record(app.url, graph, demo)

    info = probe(res.path)
    assert "Video: h264" in info and "1280x720" in info and "Audio: aac" in info
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info).groups()
    assert abs(int(h) * 3600 + int(m) * 60 + float(s) - res.duration) < 0.2

    assert len(res.timeline) == 5
    assert res.timeline[0]["start"] == pytest.approx(TITLE_S, abs=0.05)
    assert all(a["end"] == pytest.approx(b["start"], abs=0.05) for a, b in zip(res.timeline, res.timeline[1:]))
    assert res.duration == pytest.approx(res.timeline[-1]["end"] + OUTRO_S, abs=0.1)

    assert res.srt.read_text(encoding="utf-8").count(" --> ") == 5
    assert "Export CSV" in res.srt.read_text(encoding="utf-8")
    assert res.poster.exists() and res.raw and res.raw.exists()

    shows = q(graph, """
        MATCH (v:Video {id: $v})-[sh:SHOWS]->(s:Scene)<-[:HAS_SCENE]-(:Demo {id: $d})
        RETURN s.index, sh.start, sh.end ORDER BY s.index""", {"v": res.video_id, "d": demo})
    assert [r[0] for r in shows] == [0, 1, 2, 3, 4]
    assert q(graph, "MATCH (:Video {id: $v})-[:VERIFIED_AGAINST]->(c:Crawl) RETURN c.status",
             {"v": res.video_id}) == [["completed"]]


def test_refuses_a_demo_the_graph_marks_stale(app, graph):
    crawl(app.url, graph)
    demo = plan_and_save(app.url, graph, "create an invoice")
    app.edit(lambda c: c["labels"].update(new_invoice="Create an invoice"))
    crawl(app.url, graph)

    with pytest.raises(RecordError, match="New invoice"):
        record(app.url, graph, demo)


def test_refuses_when_the_live_product_disagrees(app, graph):
    crawl(app.url, graph)
    demo = plan_and_save(app.url, graph, "export csv")
    app.edit(lambda c: c["labels"].update(export_csv="Download CSV"))  # changed, but not crawled yet

    with pytest.raises(RecordError, match="live page says “Download CSV”"):
        record(app.url, graph, demo)


def test_release_pinpoints_stale_seconds_of_a_video(app, graph):
    crawl(app.url, graph)
    demo = plan_and_save(app.url, graph, "export csv")
    res = record(app.url, graph, demo)

    app.edit(lambda c: c["labels"].update(export_csv="Download CSV"))
    crawl(app.url, graph)

    segments = q(graph, Q.STALE_VIDEO_SEGMENTS)
    assert {r[0] for r in segments} == {res.video_id}
    stale_scenes = sorted({r[2] for r in segments})
    assert stale_scenes == [3, 4]  # the click on "Export CSV" and the confirmation that follows it
    for vid, path, idx, start, end, was, now, removed, role in segments:
        assert (start, end) == (res.timeline[idx]["start"], res.timeline[idx]["end"])
        assert now == "Download CSV"
