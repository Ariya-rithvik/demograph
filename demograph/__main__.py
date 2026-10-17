"""DemoGraph command line.

    python -m demograph explore http://127.0.0.1:8765 --agents 3
    python -m demograph explore http://127.0.0.1:8765 --resume
    python -m demograph explore http://127.0.0.1:8765 --join <crawl-id>
    python -m demograph status  http://127.0.0.1:8765
    python -m demograph changes http://127.0.0.1:8765
    python -m demograph map     http://127.0.0.1:8765
    python -m demograph plan    http://127.0.0.1:8765 "export csv"
    python -m demograph record  http://127.0.0.1:8765 "export csv"     (plan + record)
    python -m demograph record  http://127.0.0.1:8765 <demo-id>
    python -m demograph demos   http://127.0.0.1:8765
"""

import argparse
import asyncio
import json
import sys

from . import queries as Q
from .config import settings
from .db import GraphStore
from .explorer import CrawlConfig, explore
from .ids import graph_name_for, normalize_url, origin
from .planner import Planner, PlanError
from .recorder import RecordError, record_demo
from .recorder.media import DEFAULT_VOICE


def _store(url: str, graph: str | None) -> GraphStore:
    return GraphStore(graph or graph_name_for(normalize_url(url) or url))


async def cmd_explore(a) -> None:
    summary = await explore(CrawlConfig(
        root_url=a.url, agents=a.agents, max_pages=a.max_pages, max_depth=a.max_depth,
        interact=a.interact, headless=not a.headed, graph=a.graph, join=a.join, resume=a.resume, pace=a.pace))
    print(f"\n{summary['crawl']}: {summary['visited']} pages by {summary['agents']} agents, "
          f"{summary['unchanged']} unchanged since last crawl, {summary['changes']} changes recorded, "
          f"{summary['clicks_run']} clicks run, {summary['clicks_reused']} reused from graph memory")
    print(f"graph: {summary['graph']}")


async def cmd_status(a) -> None:
    store = _store(a.url, a.graph)
    try:
        pages, elements, links, crawls, agents = await store.one(Q.GRAPH_STATS)
        print(f"graph {store.name}: {pages} pages, {elements} elements, {links} navigation edges, "
              f"{crawls} crawls, {agents} agents")
        rows = await store.query(
            "MATCH (c:Crawl) RETURN c.id ORDER BY c.started_at DESC LIMIT 5")
        for (cid,) in rows:
            s = await store.one(Q.CRAWL_SUMMARY, {"crawl_id": cid})
            print(f"  {s[0]}  {s[1]:<10} {int(s[2] or 0):>3} pages  {int(s[3] or 0):>3} unchanged  "
                  f"{int(s[5] or 0):>3} changes  clicks run/reused {int(s[6] or 0)}/{int(s[7] or 0)}")
    finally:
        await store.close()


async def cmd_changes(a) -> None:
    store = _store(a.url, a.graph)
    try:
        crawl = a.crawl
        if not crawl:
            row = await store.one(Q.LATEST_COMPLETED_CRAWL, {"site_id": origin(normalize_url(a.url))})
            if not row:
                print("No completed crawl yet.")
                return
            crawl = row[0]
        rows = await store.query(Q.CRAWL_CHANGES, {"crawl_id": crawl})
        print(f"{len(rows)} changes in {crawl}")
        for kind, page, el_kind, selector, old, new in rows:
            what = f"{el_kind or 'page'}"
            if kind == "changed":
                print(f"  CHANGED  {page:<24} {what:<8} {old!r} -> {new!r}")
            elif kind in ("added", "removed"):
                print(f"  {kind.upper():<8} {page:<24} {what:<8} {(new or old)!r}")
            else:
                print(f"  {kind.upper():<8} {page}")
    finally:
        await store.close()


async def cmd_plan(a) -> None:
    store = _store(a.url, a.graph)
    try:
        planner = Planner(store, normalize_url(a.url))
        try:
            plan = await planner.plan(a.goal, start_path=a.start)
        except PlanError as e:
            raise SystemExit(str(e))
        t = plan.target
        print(f"Goal     {a.goal!r}")
        print(f"Target   {t.kind} “{t.text}” on {t.page_path}  (full-text score {t.score:g}, "
              f"{len(plan.considered)} reachable matches considered)")
        for c in plan.considered[1:4]:
            print(f"         also: {c.kind} “{c.text[:60]}” on {c.page_path}  (score {c.score:g}, cost {c.cost:g})")
        route = " ".join([plan.route_paths[0]] + [f"--{h.action} “{h.label}”--> {p}"
                                                  for h, p in zip(plan.hops, plan.route_paths[1:])])
        print(f"Route    cost {plan.cost:g} via algo.SPpaths over LEADS_TO\n         {route}")
        print("Scenes")
        for s in plan.scenes:
            cites = ", ".join(f"{c.role} “{c.text[:40]}”" for c in s.cites)
            print(f"  {s.index}  {s.kind:<9} {s.page_path:<26} {s.narration}")
            print(f"     {'':<9} {'':<26} cites: {cites}")
        if a.dry_run:
            return
        try:
            await planner.save(plan)
        except PlanError as e:
            raise SystemExit(str(e))
        out = settings.data_dir / "demos" / f"{plan.id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved    {plan.id}: {len(plan.scenes)} scenes, "
              f"{sum(len(s.cites) for s in plan.scenes)} citations -> graph + {out.as_posix()}")
    finally:
        await store.close()


async def cmd_record(a) -> None:
    store = _store(a.url, a.graph)
    root = normalize_url(a.url)
    try:
        demo_id = a.demo
        if not await store.one(Q.DEMO_INFO, {"demo_id": demo_id}):
            planner = Planner(store, root)  # not a saved demo id: treat it as a goal and plan it now
            plan = await planner.plan(a.demo, start_path=a.start)
            await planner.save(plan)
            demo_id = plan.id
            print(f"Planned  {demo_id}: {'  ->  '.join(plan.route_paths)}")
        res = await record_demo(store, root, demo_id, voice=a.voice, engine="silent" if a.silent else "auto",
                                headless=not a.headed)
    except (PlanError, RecordError) as e:
        raise SystemExit(str(e))
    finally:
        await store.close()
    print(f"Video    {res.path.as_posix()}  ({res.duration:.1f}s, voice: {res.voice})")
    for t in res.timeline:
        print(f"         {t['start']:>5.1f}s - {t['end']:>5.1f}s  {t['scene_id']}")
    print(f"Captions {res.srt.as_posix()}")
    print(f"Poster   {res.poster.as_posix()}")
    if res.raw:
        print(f"Raw      {res.raw.as_posix()}  (unedited browser session)")
    print(f"Graph    (:Video {{id: '{res.video_id}'}}) -[:SHOWS {{start, end}}]-> {len(res.timeline)} scenes, "
          f"{res.citations} citations verified live")


async def cmd_demos(a) -> None:
    store = _store(a.url, a.graph)
    try:
        rows = await store.query(Q.DEMO_HEALTH)
        if not rows:
            print("No demos yet. Run `demograph plan <url> \"<goal>\"`.")
        for demo_id, goal, _, scenes, stale in rows:
            state = "up to date" if not stale else f"{int(stale)} of {scenes} scenes STALE"
            videos = await store.query(Q.DEMO_VIDEOS, {"demo_id": demo_id})
            print(f"  {demo_id:<34} {goal!r:<24} {state}" + (f"  ({len(videos)} video{'s' * (len(videos) != 1)})" if videos else ""))
            if stale:
                for idx, kind, role, was, now, removed in await store.query(Q.STALE_CITATIONS, {"demo_id": demo_id}):
                    now_text = "(removed)" if removed else repr(now)
                    if role == "result":
                        print(f"      scene {idx} {kind:<9} confirmation after clicking an element that is now {now_text}")
                    else:
                        print(f"      scene {idx} {kind:<9} {role} {was!r} is now {now_text}")
        segments = await store.query(Q.STALE_VIDEO_SEGMENTS)
        if segments:
            print("\nVideo seconds that no longer match the product:")
            for vid, path, idx, start, end, was, now, removed, role in segments:
                now_text = "is gone" if removed else f"now says “{now}”"
                if role == "result":
                    what = f"shows what happens after clicking an element that {now_text}"
                else:
                    what = f"says “{was}”, the product {now_text}"
                print(f"  {path}  {start:.1f}s-{end:.1f}s  scene {idx}: {what}")
    finally:
        await store.close()


async def cmd_map(a) -> None:
    store = _store(a.url, a.graph)
    try:
        for src, action, label, dst in await store.query(Q.SITE_MAP):
            print(f"  {src:<28} --{action}: {label!r}--> {dst}")
    finally:
        await store.close()


def main(argv=None) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="demograph")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("explore", help="explore a product in a real browser and write it to the graph")
    ex.add_argument("url")
    ex.add_argument("--agents", type=int, default=2)
    ex.add_argument("--max-pages", type=int, default=40)
    ex.add_argument("--max-depth", type=int, default=5)
    ex.add_argument("--interact", action=argparse.BooleanOptionalAction, default=None,
                    help="click buttons and submit forms (default: only for localhost / *.test apps)")
    ex.add_argument("--headed", action="store_true", help="show the browser windows")
    ex.add_argument("--pace", type=float, default=0.0, help="seconds each agent waits after a page (for the live dashboard)")
    ex.add_argument("--join", metavar="CRAWL_ID", help="add agents to a crawl another process is running")
    ex.add_argument("--resume", action="store_true", help="continue the latest unfinished crawl")
    ex.add_argument("--graph")

    pl = sub.add_parser("plan", help="plan a demo of a feature: shortest route + scenes that cite elements")
    pl.add_argument("url")
    pl.add_argument("goal", help='what to show, e.g. "export csv" or "create an invoice"')
    pl.add_argument("--start", default="/", help="page path the demo starts on")
    pl.add_argument("--dry-run", action="store_true", help="print the plan without saving it")
    pl.add_argument("--graph")

    rc = sub.add_parser("record", help="replay a demo in a real browser and render a narrated MP4")
    rc.add_argument("url")
    rc.add_argument("demo", help="a demo id from `plan`, or a goal to plan and record in one go")
    rc.add_argument("--voice", default=DEFAULT_VOICE, help="Edge neural voice name")
    rc.add_argument("--silent", action="store_true", help="no voiceover (captions only)")
    rc.add_argument("--start", default="/", help="start page when planning from a goal")
    rc.add_argument("--headed", action="store_true", help="show the browser while replaying")
    rc.add_argument("--graph")

    ui = sub.add_parser("ui", help="live dashboard: watch the crawl, plan demos, play videos")
    ui.add_argument("url")
    ui.add_argument("--port", type=int, default=8800)

    for name in ("status", "changes", "map", "demos"):
        p = sub.add_parser(name)
        p.add_argument("url")
        p.add_argument("--graph")
        if name == "changes":
            p.add_argument("--crawl")

    a = ap.parse_args(argv)
    if a.cmd == "ui":
        from .ui.server import serve
        return serve(a.url, a.port)
    handler = {"explore": cmd_explore, "status": cmd_status, "changes": cmd_changes, "map": cmd_map,
               "plan": cmd_plan, "demos": cmd_demos, "record": cmd_record}[a.cmd]
    asyncio.run(handler(a))


if __name__ == "__main__":
    sys.exit(main())
