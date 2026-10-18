"""Watcher agent: keeps every demo, video, and guide true as the product changes.

Each round:
  1. re-crawl the product (the explorer's graph memory makes unchanged pages cheap),
  2. if anything changed, find the demos whose citations no longer match (one graph query),
  3. rebuild each one, locked onto the same element by fact id, so a renamed button is
     still recognised as the same button,
  4. re-record its video and guide, and link (:Demo new)-[:SUPERSEDES]->(:Demo old) and
     (:Crawl)-[:TRIGGERED]->(:Demo new) so the history of every artifact stays in the graph.
A demo whose feature can no longer be reached is marked broken instead of being silently dropped.
"""

import asyncio
import time

from . import queries as Q
from .db import GraphStore
from .explorer import CrawlConfig, explore
from .ids import graph_name_for, normalize_url
from .planner import Planner, PlanError
from .recorder import RecordError, record_demo


def _now() -> int:
    return int(time.time() * 1000)


async def watch_once(root_url: str, graph: str | None = None, agents: int = 3, pace: float = 0.0,
                     engine: str = "auto", log=print) -> dict:
    root = normalize_url(root_url)
    summary = await explore(CrawlConfig(root_url=root, agents=agents, graph=graph, pace=pace), log=log)
    report = {"crawl": summary["crawl"], "changes": summary["changes"], "updated": [], "broken": [], "healed": []}
    store = GraphStore(graph or graph_name_for(root))
    try:
        if summary["changes"]:
            log(f"Release detected: {summary['changes']} change(s) in {summary['crawl']}")
            await _rebuild_stale(store, root, summary["crawl"], engine, report, log)
        else:
            log(f"No changes in {summary['crawl']}.")
        await _finish_interrupted(store, root, engine, report, log)
    finally:
        await store.close()
    return report


async def _rebuild_stale(store: GraphStore, root: str, crawl_id: str, engine: str, report: dict, log) -> None:
    stale = [r for r in await store.query(Q.DEMO_HEALTH) if int(r[4] or 0) > 0 and r[5] == "current"]
    if not stale:
        log("No demo cites anything that changed.")
    # Several demos of the same goal on the same element are duplicates: rebuild the newest
    # one and retire the rest onto it, so a release never multiplies videos.
    groups: dict[tuple, list] = {}
    for row in stale:  # DEMO_HEALTH is newest first
        target = await store.one(Q.DEMO_TARGET, {"demo_id": row[0]})
        groups.setdefault((row[1], target[0] if target else None), []).append(row)
    for (goal, target), rows in groups.items():
        demo_id = rows[0][0]
        reasons = [f"“{was}” is now {'gone' if removed else '“' + str(now) + '”'}"
                   for _, _, role, was, now, removed in await store.query(Q.STALE_CITATIONS, {"demo_id": demo_id})
                   if role != "result"]
        log(f"Demo '{goal}' ({demo_id}) is stale: {'; '.join(reasons) or 'a confirmation changed'}")
        info = await store.one(Q.DEMO_INFO, {"demo_id": demo_id})
        planner = Planner(store, root)
        try:
            plan = await planner.plan(goal, start_path=info[1] or "/", target_id=target)
            await planner.save(plan)
        except PlanError as e:
            for old in (r[0] for r in rows):
                await store.query(Q.MARK_DEMO_BROKEN, {"demo_id": old, "crawl_id": crawl_id,
                                                       "reason": str(e), "now": _now()})
            report["broken"].append({"demo": demo_id, "goal": goal, "reason": str(e)})
            log(f"Demo '{goal}' is broken: {e}")
            continue
        # Supersede first: if recording is interrupted, _finish_interrupted completes it next round.
        for old in (r[0] for r in rows):
            await store.query(Q.SUPERSEDE_DEMO, {"old_id": old, "new_id": plan.id, "crawl_id": crawl_id, "now": _now()})
        entry = {"old": demo_id, "new": plan.id, "goal": goal, "reasons": reasons, "route": plan.route_paths,
                 "retired": [r[0] for r in rows[1:]], "video": None, "guide": None}
        had_artifacts = False  # any version in the group with a video or guide gets one rebuilt
        for old in (r[0] for r in rows):
            videos, docs = await store.one(Q.DEMO_ARTIFACTS, {"demo_id": old})
            had_artifacts = had_artifacts or bool(videos or docs)
        if had_artifacts:
            await _record(store, root, plan.id, goal, engine, entry, log)
        report["updated"].append(entry)
        log(f"Rebuilt '{goal}': {demo_id} -> {plan.id}"
            + (f" (video {entry['video']}, guide {entry['guide']})" if entry["video"] else ""))


async def _finish_interrupted(store: GraphStore, root: str, engine: str, report: dict, log) -> None:
    """A rebuilt demo whose predecessor had a video, but which has none yet, was interrupted mid-recording."""
    for demo_id, goal in await store.query(Q.PENDING_REBUILDS):
        log(f"Finishing the interrupted rebuild of '{goal}' ({demo_id})")
        entry = {"new": demo_id, "goal": goal, "video": None, "guide": None}
        await _record(store, root, demo_id, goal, engine, entry, log)
        report["healed"].append(entry)


async def _record(store: GraphStore, root: str, demo_id: str, goal: str, engine: str, entry: dict, log) -> None:
    try:
        res = await record_demo(store, root, demo_id, engine=engine, log=log)
        entry["video"] = res.path.as_posix()
        entry["guide"] = res.guide_html.as_posix() if res.guide_html else None
    except RecordError as e:
        entry["error"] = str(e)
        log(f"Could not re-record '{goal}': {e}")


async def watch(root_url: str, every: float = 60.0, rounds: int | None = None, log=print, **kw) -> None:
    n = 0
    while rounds is None or n < rounds:
        await watch_once(root_url, log=log, **kw)
        n += 1
        if rounds is None or n < rounds:
            log(f"Next check in {every:g}s")
            await asyncio.sleep(every)
