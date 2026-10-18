"""Orchestrator agent: runs the whole pipeline for a product in one command.

    explore (Explorer + Knowledge Graph agents)
      -> release notes, if there was an earlier crawl (Release Intelligence agent)
      -> for each goal: plan (Demo agent) -> video (Demo agent) + guide (Documentation agent)
      -> regression + smoke tests (QA agent)
      -> run report, recorded in the graph as (:Run)-[:PRODUCED]->(:Demo)

A failing goal is reported and skipped; the rest of the run carries on.
"""

import datetime as dt
import secrets
import time

from . import queries as Q
from .config import settings
from .db import GraphStore
from .explorer import CrawlConfig, explore
from .ids import graph_name_for, normalize_url
from .planner import Planner, PlanError
from .qa import qa
from .recorder import RecordError, record_demo
from .release import release_notes


async def run_pipeline(root_url: str, goals: list[str], agents: int = 3, engine: str = "auto",
                       graph: str | None = None, max_pages: int = 40, log=print) -> dict:
    root = normalize_url(root_url)
    run_id = f"run-{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    log(f"[orchestrator] {run_id}: {len(goals)} goal(s) on {root}")
    log("[orchestrator] 1/4 explore")
    crawl = await explore(CrawlConfig(root_url=root, agents=agents, graph=graph, max_pages=max_pages), log=log)
    store = GraphStore(graph or graph_name_for(root))
    report = {"run": run_id, "crawl": crawl["crawl"], "pages": crawl["visited"], "changes": crawl["changes"],
              "demos": [], "failures": [], "qa": None, "release": None}
    try:
        if await store.one(Q.PREVIOUS_CRAWL, {"crawl_id": crawl["crawl"]}):
            note = await release_notes(store, root, crawl["crawl"])
            report["release"] = note.path.as_posix()
            log(f"[orchestrator] release notes: {note.summary}")

        log("[orchestrator] 2/4 plan, record and document each goal")
        for goal in goals:
            try:
                planner = Planner(store, root)
                plan = await planner.plan(goal)
                await planner.save(plan)
                res = await record_demo(store, root, plan.id, engine=engine, log=log)
                report["demos"].append({"goal": goal, "demo": plan.id, "route": plan.route_paths,
                                        "video": res.path.as_posix(),
                                        "guide": res.guide_html.as_posix() if res.guide_html else None})
                log(f"[orchestrator] ✓ {goal}: {' -> '.join(plan.route_paths)}")
            except Exception as e:  # PlanError, RecordError, or a browser/ffmpeg failure
                msg = str(e) or type(e).__name__
                report["failures"].append({"goal": goal, "error": msg})
                log(f"[orchestrator] ✗ {goal}: {msg.splitlines()[0]}")

        log("[orchestrator] 3/4 generate and run tests")
        suites = await qa(store, root, [d["demo"] for d in report["demos"]], log=log)
        passed = sum(s.passed or 0 for s in suites)
        failed = sum(s.failed or 0 for s in suites)
        report["qa"] = {"suites": [s.path.as_posix() for s in suites], "passed": passed, "failed": failed}

        log("[orchestrator] 4/4 report")
        status = "ok" if not report["failures"] and not failed else "partial"
        path = settings.data_dir / "runs" / store.name / f"{run_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {run_id}", "", f"Site {root} · crawl `{crawl['crawl']}` · {crawl['visited']} pages, "
                 f"{crawl['changes']} changes since the previous crawl", ""]
        lines += [f"- ✓ **{d['goal']}**: {' → '.join(d['route'])} · video `{d['video']}` · guide `{d['guide']}`"
                  for d in report["demos"]]
        lines += [f"- ✗ **{f['goal']}**: {f['error'].splitlines()[0]}" for f in report["failures"]]
        lines += ["", f"QA: {passed} passed, {failed} failed across {len(suites)} suites."]
        if report["release"]:
            lines.append(f"Release notes: `{report['release']}`")
        path.write_text("\n".join(lines), encoding="utf-8")
        report["report"] = path.as_posix()
        report["status"] = status
        await store.query(Q.SAVE_RUN, {"run_id": run_id, "crawl_id": crawl["crawl"], "now": int(time.time() * 1000),
                                       "goals": goals, "status": status, "report": path.as_posix(),
                                       "failures": [f["goal"] for f in report["failures"]],
                                       "demos": [d["demo"] for d in report["demos"]]})
    finally:
        await store.close()
    return report
