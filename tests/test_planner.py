"""Planner tests: goal -> target element -> shortest route -> scenes that cite elements."""

import asyncio
import json

import pytest

from conftest import crawl, q
from demograph import queries as Q
from demograph.db import GraphStore
from demograph.planner import Planner, PlanError


def plan(url: str, graph: str, goal: str, save: bool = False, **kw):
    async def run():
        store = GraphStore(graph)
        try:
            planner = Planner(store, url + "/")
            p = await planner.plan(goal, **kw)
            problems = await planner.verify(p)
            if save:
                await planner.save(p)
            return p, problems
        finally:
            await store.close()
    return asyncio.run(run())


def test_feedback_feature_gets_route_and_cited_scenes(crawled):
    app, graph = crawled
    p, problems = plan(app.url, graph, "export csv", save=True)

    assert problems == []
    assert p.target.kind == "button" and p.target.text == "Export CSV"
    assert p.route_paths == ["/", "/app", "/app/reports"]
    assert p.cost == 2
    assert [s.kind for s in p.scenes] == ["intro", "navigate", "navigate", "action", "result"]
    assert "CSV export started" in p.scenes[-1].narration
    assert all(s.cites for s in p.scenes), "every scene must cite at least one element"

    # Saved: every citation holds the element's current content hash, so nothing is stale yet.
    rows = q(graph, """
        MATCH (:Demo {id: $d})-[:HAS_SCENE]->(s:Scene)-[c:CITES]->(e:Element)
        RETURN s.index, c.hash = e.content_hash""", {"d": p.id})
    assert len(rows) == sum(len(s.cites) for s in p.scenes)
    assert all(fresh for _, fresh in rows)
    assert q(graph, "MATCH (:Demo {id: $d})-[:TARGETS]->(e:Element) RETURN e.text", {"d": p.id}) == [["Export CSV"]]
    assert len(q(graph, "MATCH (:Demo {id: $d})-[:HAS_SCENE]->(a:Scene)-[:NEXT]->(b:Scene) RETURN a, b", {"d": p.id})) == 4


def test_form_feature_fills_fields_and_ends_on_result(crawled):
    app, graph = crawled
    p, problems = plan(app.url, graph, "create an invoice")

    assert problems == []
    assert p.target.text == "Create invoice"
    assert p.route_paths == ["/", "/app", "/app/invoices/new", "/app/invoices?created=INV-2001"]
    form = next(s for s in p.scenes if s.kind == "form")
    assert [c.role for c in form.cites] == ["fill", "fill", "fill", "click"]
    assert [st["do"] for st in form.steps] == ["select", "fill", "fill", "click"]
    assert form.steps[-1]["expect_url"].endswith("/app/invoices?created=INV-2001")
    assert p.scenes[-1].kind == "result"
    assert "INV-2001" in p.scenes[-1].narration


def test_shortest_route_starts_where_asked(crawled):
    app, graph = crawled
    p, _ = plan(app.url, graph, "export csv", start_path="/pricing")
    assert p.route_paths[0] == "/pricing"
    assert p.route_paths[-1] == "/app/reports"
    assert p.scenes[0].narration.startswith("We start on the Pricing page")


def test_verify_rejects_numbers_nothing_cites(crawled):
    app, graph = crawled
    p, _ = plan(app.url, graph, "export csv")

    async def check():
        store = GraphStore(graph)
        try:
            planner = Planner(store, app.url + "/")
            p.scenes[2].narration += " It only costs $99."
            assert any("99" in problem for problem in await planner.verify(p))
            with pytest.raises(PlanError):
                await planner.save(p)
        finally:
            await store.close()
    asyncio.run(check())


def test_release_marks_exactly_the_affected_scene_stale(app, graph):
    crawl(app.url, graph)
    invoice, _ = plan(app.url, graph, "create an invoice", save=True)
    export, _ = plan(app.url, graph, "export csv", save=True)

    app.edit(lambda c: c["labels"].update(new_invoice="Create an invoice"))
    crawl(app.url, graph)

    health = {row[0]: int(row[4]) for row in q(graph, Q.DEMO_HEALTH)}
    assert health[invoice.id] == 1
    assert health[export.id] == 0
    stale = q(graph, Q.STALE_CITATIONS, {"demo_id": invoice.id})
    assert [(r[1], r[3], r[4]) for r in stale] == [("navigate", "New invoice", "Create an invoice")]


def test_removed_links_are_never_routed_through(app, graph):
    crawl(app.url, graph)
    app.edit(lambda c: c.update(features=[f for f in c["features"] if f["slug"] != "reports"]))
    crawl(app.url, graph)

    stale = q(graph, "MATCH (a:Page)-[r:LEADS_TO {stale: true}]->(b:Page {path: '/features/reports'}) RETURN a.path")
    assert stale, "links to the removed page should be marked stale"
    assert q(graph, "MATCH (p:Page {path: '/features/reports'}) RETURN p.gone") == [[True]]

    p, _ = plan(app.url, graph, "cash-flow report")
    assert "/features/reports" not in p.route_paths
    steps = json.dumps([s.steps for s in p.scenes])
    assert "/features/reports" not in steps
