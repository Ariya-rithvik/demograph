"""End-to-end tests: the Explorer crawls the Ledgerly sample app into a real FalkorDB."""

import asyncio
import json

import pytest

from conftest import crawl, q
from demograph.db import GraphStore
from demograph.explorer import CrawlConfig, explore

NO_DOUBLE_VISITS = "MATCH (c:Crawl)-[v:VISITED]->(p:Page) WITH c, p, count(v) AS n WHERE n > 1 RETURN p.path"


def test_crawl_memory_and_drift(app, graph):
    first = crawl(app.url, graph)
    assert first["status"] == "completed"
    assert first["visited"] >= 20
    assert first["agents"] >= 2, "work should be shared between agents through the graph frontier"
    assert q(graph, NO_DOUBLE_VISITS) == []

    edges = {(a, act, b) for a, act, b in q(graph, "MATCH (a:Page)-[r:LEADS_TO]->(b:Page) RETURN a.path, r.action, b.path")}
    assert ("/", "link", "/pricing") in edges
    assert ("/", "click", "/signup") in edges
    assert ("/pricing", "click", "/signup?plan=pro") in edges
    assert ("/app/invoices/new", "submit", "/app/invoices?created=INV-2001") in edges

    inputs = q(graph, "MATCH (:Page {path:'/signup?plan=pro'})-[r:LEADS_TO {action:'submit'}]->() RETURN r.inputs")[0][0]
    assert json.loads(inputs)["Plan"].startswith("Pro"), "preselected plan should be kept"

    effects = dict(q(graph, "MATCH (e:Element) WHERE e.click_effect IS NOT NULL RETURN e.text, e.click_effect"))
    assert effects["Export CSV"] == "feedback"
    assert "Delete workspace" not in effects, "dangerous buttons are never clicked"

    second = crawl(app.url, graph)
    assert second["unchanged"] == second["visited"]
    assert second["clicks_run"] == 0
    assert second["clicks_reused"] == first["clicks_run"]
    assert second["changes"] == 0

    app.edit(lambda c: next(p for p in c["plans"] if p["id"] == "pro").update(price=35))
    third = crawl(app.url, graph)
    assert third["changes"] > 0
    changed = q(graph, """
        MATCH (p:Page {path: '/pricing'})-[:HAS_ELEMENT]->(e:Element)-[ch:CHANGED_IN]->(:Crawl {id: $c})
        RETURN ch.old_text, ch.new_text""", {"c": third["crawl"]})
    assert ["$29", "$35"] in changed
    assert q(graph, "MATCH (:Crawl {id: $a})-[:FOLLOWS]->(:Crawl {id: $b}) RETURN 1",
             {"a": third["crawl"], "b": second["crawl"]}) == [[1]]
    untouched = q(graph, "MATCH (c:Crawl {id: $c})-[v:VISITED {unchanged: true}]->(p:Page) RETURN p.path", {"c": third["crawl"]})
    assert ["/help"] in untouched


def test_resume_after_crash(app, graph):
    async def crash_midway():
        task = asyncio.create_task(explore(CrawlConfig(root_url=app.url, graph=graph, agents=2), log=lambda _: None))
        store = GraphStore(graph)
        try:
            for _ in range(300):
                await asyncio.sleep(0.1)
                try:
                    done = await store.query("MATCH (:Crawl)-[f:FRONTIER {status:'done'}]->() RETURN count(f)")
                except Exception:
                    continue
                if done and done[0][0] >= 5:
                    break
        finally:
            await store.close()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(crash_midway())
    assert q(graph, "MATCH (c:Crawl) RETURN c.status") == [["running"]]

    resumed = crawl(app.url, graph, agents=2, resume=True)
    assert resumed["status"] == "completed"
    assert resumed["visited"] >= 20
    assert q(graph, NO_DOUBLE_VISITS) == []


def test_page_cap_is_respected(app, graph):
    capped = crawl(app.url, graph, agents=3, max_pages=4)
    assert capped["visited"] == 4
    assert q(graph, "MATCH (c:Crawl) RETURN c.capped") == [[True]]
