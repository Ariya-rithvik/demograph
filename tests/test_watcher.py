"""Documentation agent and watcher agent: guides from verified replays, automatic rebuilds after a release."""

import asyncio

from conftest import crawl, q
from demograph import queries as Q
from demograph.db import GraphStore
from demograph.planner import Planner
from demograph.recorder import record_demo
from demograph.watcher import watch_once


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


def watch(url: str, graph: str) -> dict:
    return asyncio.run(watch_once(url, graph=graph, engine="silent", log=lambda _: None))


def test_recording_also_writes_a_verified_guide(crawled):
    app, graph = crawled
    demo = plan_and_save(app.url, graph, "export csv")
    res = record(app.url, graph, demo)

    md = res.guide.read_text(encoding="utf-8")
    assert md.startswith("# How to Export CSV in Ledgerly")
    assert md.count("\n## Step ") == 5
    assert "“Export CSV”" in md and "element `f_" in md
    assert all((res.guide.parent / f"step-{i}.png").exists() for i in range(1, 6))
    assert "Every step was replayed in a live browser" in " ".join(res.guide_html.read_text(encoding="utf-8").split())
    shows = q(graph, """
        MATCH (doc:Doc {id: $doc})-[:DOCUMENTS]->(:Demo {id: $d}), (doc)-[sh:SHOWS]->(s:Scene)
        RETURN sh.section, s.index ORDER BY sh.section""", {"doc": res.doc_id, "d": demo})
    assert shows == [[i + 1, i] for i in range(5)]


def test_watcher_rebuilds_the_stale_demo_video_and_guide(app, graph):
    crawl(app.url, graph)
    old = plan_and_save(app.url, graph, "export csv")
    record(app.url, graph, old)
    duplicate = plan_and_save(app.url, graph, "export csv")  # same goal, same button: must not get its own video

    quiet = watch(app.url, graph)
    assert quiet["changes"] == 0 and quiet["updated"] == []

    app.edit(lambda c: c["labels"].update(export_csv="Download CSV"))
    report = watch(app.url, graph)

    assert report["changes"] == 1
    assert len(report["updated"]) == 1
    entry = report["updated"][0]
    assert entry["old"] == duplicate and entry["retired"] == [old]   # newest rebuilt, the other retired onto it
    assert entry["video"] and entry["guide"]
    new = entry["new"]
    assert q(graph, "MATCH (d:Demo) WHERE d.id IN $ids RETURN d.id, d.status ORDER BY d.id",
             {"ids": [old, duplicate, new]}) == sorted([[old, "superseded"], [duplicate, "superseded"], [new, "current"]])
    assert q(graph, "MATCH (:Crawl {id: $c})-[:TRIGGERED]->(n:Demo)-[:SUPERSEDES]->(o:Demo) RETURN count(o)",
             {"c": report["crawl"]}) == [[2]]
    # Same button by identity, new words everywhere.
    assert q(graph, """MATCH (n:Demo {id: $n})-[:TARGETS]->(e), (o:Demo {id: $o})-[:TARGETS]->(e2)
                       RETURN e.id = e2.id, e.text""", {"n": new, "o": old}) == [[True, "Download CSV"]]
    narration = [r[0] for r in q(graph, "MATCH (:Demo {id: $n})-[:HAS_SCENE]->(s) RETURN s.narration ORDER BY s.index", {"n": new})]
    assert "Click “Download CSV”." in narration
    guide = q(graph, "MATCH (doc:Doc)-[:DOCUMENTS]->(:Demo {id: $n}) RETURN doc.path", {"n": new})[0][0]
    assert "Download CSV" in open(guide, encoding="utf-8").read()
    # Nothing current is stale any more; the old video is retired, not reported.
    assert [r for r in q(graph, Q.DEMO_HEALTH) if r[5] == "current" and r[4]] == []
    assert q(graph, Q.STALE_VIDEO_SEGMENTS) == [] and q(graph, Q.STALE_DOC_SECTIONS) == []


def test_watcher_marks_a_demo_broken_when_its_feature_is_removed(app, graph):
    crawl(app.url, graph)
    demo = plan_and_save(app.url, graph, "send reminder")
    app.edit(lambda c: [i.update(status="Paid") for i in c["invoices"]])  # paid invoices have no reminder button

    report = watch(app.url, graph)

    assert report["updated"] == []
    assert [b["demo"] for b in report["broken"]] == [demo]
    assert q(graph, "MATCH (d:Demo {id: $d}) RETURN d.status", {"d": demo}) == [["broken"]]
    assert q(graph, "MATCH (:Crawl {id: $c})-[:BROKE]->(:Demo {id: $d}) RETURN 1", {"c": report["crawl"], "d": demo}) == [[1]]
