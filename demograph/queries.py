"""Every Cypher query the product depends on, in one place.

Graph model (one FalkorDB graph per product, named demograph_<host>_<port>):

  (:Site)-[:HAS_CRAWL]->(:Crawl)-[:FOLLOWS]->(:Crawl)          crawl history, newest first
  (:Crawl)-[:FRONTIER {status, depth}]->(:Page)                shared work queue for explorer agents
  (:Crawl)-[:VISITED {agent, unchanged}]->(:Page)              what each crawl actually saw
  (:Agent)-[:EXPLORED {crawl}]->(:Page)                        which agent did the work
  (:Page)-[:HAS_ELEMENT]->(:Element)                           facts: one element on one page
  (:Page)-[:LEADS_TO {via, action, label}]->(:Page)            navigation discovered by following/clicking/submitting
  (:Element)-[:OPENS]->(:Page)                                 the element that performs that navigation
  (:Page)-[:REDIRECTS_TO]->(:Page)
  (:Element|:Page)-[:CHANGED_IN {kind, old_text, new_text}]->(:Crawl)   drift between crawls
"""

# --- Crawl lifecycle -------------------------------------------------------

UPSERT_SITE = """
MERGE (s:Site {id: $site_id})
ON CREATE SET s.root_url = $root_url, s.origin = $origin, s.created_at = $now
RETURN s.id
"""

LATEST_COMPLETED_CRAWL = """
MATCH (:Site {id: $site_id})-[:HAS_CRAWL]->(c:Crawl {status: 'completed'})
RETURN c.id
ORDER BY c.finished_at DESC
LIMIT 1
"""

LATEST_RUNNING_CRAWL = """
MATCH (:Site {id: $site_id})-[:HAS_CRAWL]->(c:Crawl {status: 'running'})
RETURN c.id
ORDER BY c.started_at DESC
LIMIT 1
"""

CREATE_CRAWL = """
MATCH (s:Site {id: $site_id})
CREATE (s)-[:HAS_CRAWL]->(c:Crawl {
  id: $crawl_id, root_url: $root_url, status: 'running', started_at: $now,
  max_pages: $max_pages, max_depth: $max_depth, interact: $interact
})
RETURN c.id
"""

LINK_PREVIOUS_CRAWL = """
MATCH (c:Crawl {id: $crawl_id}), (prev:Crawl {id: $prev_id})
MERGE (c)-[:FOLLOWS]->(prev)
"""

SEED_FRONTIER = """
MATCH (c:Crawl {id: $crawl_id})
MERGE (p:Page {id: $page_id})
ON CREATE SET p.url = $url, p.path = $path, p.first_seen = $now
MERGE (c)-[f:FRONTIER]->(p)
ON CREATE SET f.status = 'pending', f.depth = 0, f.queued_at = $now, f.attempts = 0
"""

# Write queries run one at a time per graph in FalkorDB, so this
# match-then-set is atomic: two agents can never claim the same page.
CLAIM_NEXT_PAGE = """
MATCH (c:Crawl {id: $crawl_id, status: 'running'})
OPTIONAL MATCH (c)-[taken:FRONTIER]->()
WHERE taken.status IN ['claimed', 'done']
WITH c, count(taken) AS taken
WHERE taken < c.max_pages
MATCH (c)-[f:FRONTIER {status: 'pending'}]->(p:Page)
WITH f, p
ORDER BY f.depth ASC, f.queued_at ASC
LIMIT 1
SET f.status = 'claimed', f.agent = $agent_id, f.claimed_at = $now, f.attempts = f.attempts + 1
RETURN p.id, p.url, f.depth
"""

FRONTIER_COUNTS = """
MATCH (c:Crawl {id: $crawl_id})
OPTIONAL MATCH (c)-[f:FRONTIER]->()
RETURN c.status, c.max_pages,
       sum(CASE WHEN f.status = 'pending' THEN 1 ELSE 0 END) AS pending,
       sum(CASE WHEN f.status = 'claimed' THEN 1 ELSE 0 END) AS claimed,
       sum(CASE WHEN f.status = 'done' THEN 1 ELSE 0 END) AS done
"""

# Claims held longer than the stale window belong to an agent that died.
REQUEUE_STALE_CLAIMS = """
MATCH (:Crawl {id: $crawl_id})-[f:FRONTIER {status: 'claimed'}]->()
WHERE f.claimed_at < $stale_before
SET f.status = CASE WHEN f.attempts >= $max_attempts THEN 'failed' ELSE 'pending' END
RETURN count(f)
"""

MARK_FRONTIER = """
MATCH (:Crawl {id: $crawl_id})-[f:FRONTIER]->(:Page {id: $page_id})
SET f.status = $status, f.done_at = $now, f.error = $error
"""

FAILED_PAGES = """
MATCH (:Crawl {id: $crawl_id})-[f:FRONTIER {status: 'failed'}]->()
RETURN count(f)
"""

# Only one agent wins the switch to 'finalizing', so change detection runs once.
BEGIN_FINALIZE = """
MATCH (c:Crawl {id: $crawl_id, status: 'running'})
SET c.status = 'finalizing'
RETURN c.id
"""

PAGES_ADDED = """
MATCH (c:Crawl {id: $crawl_id})-[:FOLLOWS]->(prev:Crawl)
MATCH (c)-[:VISITED]->(p:Page)
WHERE NOT (prev)-[:VISITED]->(p)
CREATE (p)-[:CHANGED_IN {kind: 'page_added', at: $now}]->(c)
SET p.gone = false
RETURN count(p)
"""

PAGES_REMOVED = """
MATCH (c:Crawl {id: $crawl_id})-[:FOLLOWS]->(prev:Crawl)
MATCH (prev)-[:VISITED]->(p:Page)
WHERE NOT (c)-[:VISITED]->(p)
CREATE (p)-[:CHANGED_IN {kind: 'page_removed', at: $now}]->(c)
SET p.gone = true
RETURN count(p)
"""

STALE_COST = 1000  # path weight of a link that no longer exists; the planner rejects such paths

# A link seen on a page in an earlier crawl but not when this crawl visited
# that page no longer exists. The planner must not route a demo through it.
MARK_STALE_LINKS = """
MATCH (c:Crawl {id: $crawl_id})-[:VISITED]->(:Page)-[r:LEADS_TO]->()
WHERE r.crawl <> $crawl_id
SET r.stale = true, r.cost = $stale_cost
RETURN count(r)
"""

COMPLETE_CRAWL = """
MATCH (c:Crawl {id: $crawl_id})
SET c.status = 'completed', c.finished_at = $now, c.pages = $pages, c.capped = $capped
"""

ADD_CRAWL_STATS = """
MATCH (c:Crawl {id: $crawl_id})
SET c.clicks_run = coalesce(c.clicks_run, 0) + $clicks_run,
    c.clicks_reused = coalesce(c.clicks_reused, 0) + $clicks_reused
"""

# --- Page visits -----------------------------------------------------------

# What the graph already remembers about a page: current facts and the
# known result of clicking each one (so unchanged buttons are not re-clicked).
PAGE_MEMORY = """
MATCH (p:Page {id: $page_id})-[:HAS_ELEMENT]->(e:Element)
WHERE NOT coalesce(e.removed, false)
RETURN e.id, e.content_hash, e.text, e.click_hash, e.click_effect, e.click_target_url, e.click_note, e.click_inputs,
       e.options
"""

PAGE_FINGERPRINT = """
MATCH (p:Page {id: $page_id})
RETURN p.fingerprint
"""

UPSERT_ELEMENTS = """
UNWIND $elements AS el
MATCH (p:Page {id: $page_id})
MERGE (e:Element {id: el.id})
SET e += el, e.removed = false, e.last_seen = $now,
    e.first_seen = coalesce(e.first_seen, $now)
MERGE (p)-[:HAS_ELEMENT]->(e)
"""

TOUCH_PAGE_ELEMENTS = """
MATCH (:Page {id: $page_id})-[:HAS_ELEMENT]->(e:Element)
WHERE NOT coalesce(e.removed, false)
SET e.last_seen = $now
"""

REMOVE_ELEMENTS = """
UNWIND $ids AS id
MATCH (e:Element {id: id})
SET e.removed = true, e.removed_at = $now
"""

RECORD_CHANGES = """
UNWIND $changes AS ch
MATCH (c:Crawl {id: $crawl_id}), (e:Element {id: ch.id})
CREATE (e)-[:CHANGED_IN {
  kind: ch.kind, old_text: ch.old_text, new_text: ch.new_text,
  old_hash: ch.old_hash, new_hash: ch.new_hash, at: $now
}]->(c)
"""

RECORD_CLICKS = """
UNWIND $clicks AS k
MATCH (e:Element {id: k.id})
SET e.click_effect = k.effect, e.click_target_url = k.target_url, e.click_note = k.note,
    e.click_inputs = k.inputs, e.click_hash = k.hash, e.clicked_at = $now
"""

# Navigation edges, plus the next pages for the shared frontier.
RECORD_NAVIGATION = """
UNWIND $links AS l
MATCH (src:Page {id: $page_id}), (c:Crawl {id: $crawl_id}), (via:Element {id: l.via})
MERGE (dst:Page {id: l.dst_id})
ON CREATE SET dst.url = l.dst_url, dst.path = l.dst_path, dst.first_seen = $now
MERGE (src)-[r:LEADS_TO {via: l.via}]->(dst)
SET r.action = l.action, r.label = l.label, r.inputs = l.inputs, r.crawl = $crawl_id, r.last_seen = $now,
    r.cost = CASE l.action WHEN 'submit' THEN 3 ELSE 1 END, r.stale = false
MERGE (via)-[:OPENS]->(dst)
MERGE (c)-[f:FRONTIER]->(dst)
ON CREATE SET f.status = CASE WHEN l.enqueue THEN 'pending' ELSE 'skipped' END,
              f.depth = l.depth, f.queued_at = $now, f.attempts = 0, f.from = $page_id
"""

RECORD_REDIRECT = """
MATCH (src:Page {id: $page_id}), (c:Crawl {id: $crawl_id})
MERGE (dst:Page {id: $dst_id})
ON CREATE SET dst.url = $dst_url, dst.path = $dst_path, dst.first_seen = $now
MERGE (src)-[r:REDIRECTS_TO]->(dst)
SET r.crawl = $crawl_id
MERGE (c)-[f:FRONTIER]->(dst)
ON CREATE SET f.status = 'pending', f.depth = $depth, f.queued_at = $now, f.attempts = 0, f.from = $page_id
"""

RECORD_VISIT = """
MATCH (c:Crawl {id: $crawl_id})-[f:FRONTIER]->(p:Page {id: $page_id})
SET p.url = $url, p.title = $title, p.last_seen = $now, p.fingerprint = $fingerprint,
    p.screenshot = $screenshot, p.status_code = $status_code, p.gone = false,
    f.status = 'done', f.done_at = $now
MERGE (c)-[v:VISITED]->(p)
SET v.at = $now, v.agent = $agent_id, v.unchanged = $unchanged,
    v.screenshot = $screenshot, v.elements = $element_count, v.ms = $ms
MERGE (a:Agent {id: $agent_id})
ON CREATE SET a.kind = 'explorer', a.first_seen = $now
SET a.last_seen = $now
MERGE (a)-[x:EXPLORED {crawl: $crawl_id}]->(p)
SET x.at = $now
"""

# --- Reports ---------------------------------------------------------------

GRAPH_STATS = """
OPTIONAL MATCH (p:Page) WITH count(p) AS pages
OPTIONAL MATCH (e:Element) WHERE NOT coalesce(e.removed, false)
WITH pages, count(e) AS elements
OPTIONAL MATCH ()-[l:LEADS_TO]->() WITH pages, elements, count(l) AS links
OPTIONAL MATCH (c:Crawl) WITH pages, elements, links, count(c) AS crawls
OPTIONAL MATCH (a:Agent)
RETURN pages, elements, links, crawls, count(a) AS agents
"""

CRAWL_SUMMARY = """
MATCH (c:Crawl {id: $crawl_id})
OPTIONAL MATCH (c)-[v:VISITED]->(p:Page)
WITH c, count(p) AS visited, sum(CASE WHEN v.unchanged THEN 1 ELSE 0 END) AS unchanged,
     collect(DISTINCT v.agent) AS agents
OPTIONAL MATCH (c)<-[ch:CHANGED_IN]-()
RETURN c.id, c.status, visited, unchanged, size(agents), count(ch),
       c.clicks_run, c.clicks_reused, c.started_at, c.finished_at
"""

CRAWL_CHANGES = """
MATCH (c:Crawl {id: $crawl_id})<-[ch:CHANGED_IN]-(x)
OPTIONAL MATCH (owner:Page)-[:HAS_ELEMENT]->(x)
RETURN ch.kind, coalesce(owner.path, x.path) AS page, x.kind, x.selector, ch.old_text, ch.new_text
ORDER BY page, ch.kind
"""

SITE_MAP = """
MATCH (a:Page)-[r:LEADS_TO]->(b:Page)
RETURN a.path, r.action, r.label, b.path
ORDER BY a.path, b.path
"""

# --- Demo planner ----------------------------------------------------------
#
#   (:Demo)-[:PLANNED_FROM]->(:Crawl)
#   (:Demo)-[:TARGETS]->(:Element)
#   (:Demo)-[:HAS_SCENE {index}]->(:Scene)-[:NEXT]->(:Scene)
#   (:Scene)-[:ON_PAGE]->(:Page)
#   (:Scene)-[:CITES {role, hash, text}]->(:Element)     hash = what the element said when the scene was planned

# Older graphs may have links recorded before costs existed.
BACKFILL_LINK_COSTS = """
MATCH ()-[r:LEADS_TO]->()
WHERE r.cost IS NULL
SET r.cost = CASE r.action WHEN 'submit' THEN 3 ELSE 1 END, r.stale = coalesce(r.stale, false)
RETURN count(r)
"""

# Find what the user asked to see, using the full-text index on what the product says.
FIND_ELEMENTS = """
CALL db.idx.fulltext.queryNodes('Element', $query) YIELD node, score
WITH node, score
WHERE NOT coalesce(node.removed, false)
MATCH (p:Page)-[:HAS_ELEMENT]->(node)
WHERE NOT coalesce(p.gone, false)
RETURN node.id, node.kind, node.text, p.id, p.path, node.is_submit, node.click_effect, node.click_note, score
ORDER BY score DESC
LIMIT 25
"""

# Cheapest route between two pages over links that still exist (weighted by r.cost).
SHORTEST_PATH = """
MATCH (a:Page {id: $start}), (b:Page {id: $target})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['LEADS_TO'],
                   weightProp: 'cost', maxLen: $max_len, pathCount: 1})
YIELD path, pathWeight
RETURN [n IN nodes(path) | n.id],
       [r IN relationships(path) | {via: r.via, action: r.action, label: r.label, inputs: r.inputs, cost: r.cost}],
       pathWeight
ORDER BY pathWeight ASC
LIMIT 1
"""

# Where clicking/submitting an element goes, if it navigates.
ELEMENT_NAVIGATION = """
MATCH (src:Page)-[r:LEADS_TO {via: $fact_id}]->(dst:Page)
WHERE NOT coalesce(r.stale, false)
RETURN dst.id, r.action, r.label, r.inputs, r.cost
LIMIT 1
"""

PAGES = """
UNWIND $ids AS id
MATCH (p:Page {id: id})
RETURN p.id, p.url, p.path, p.title, p.screenshot
"""

PAGE_ELEMENTS = """
MATCH (:Page {id: $page_id})-[:HAS_ELEMENT]->(e:Element)
WHERE NOT coalesce(e.removed, false)
RETURN e.id, e.kind, e.text, e.selector, e.bbox, e.content_hash, e.form, e.input_type, e.tag, e.options, e.click_note
ORDER BY e.bbox[1], e.bbox[0]
"""

SAVE_DEMO = """
MATCH (c:Crawl {id: $crawl_id}), (t:Element {id: $target_id})
CREATE (d:Demo {id: $demo_id, goal: $goal, created_at: $now, path_cost: $path_cost,
                scenes: $scene_count, start_path: $start_path, target_text: t.text})
CREATE (d)-[:PLANNED_FROM]->(c)
CREATE (d)-[:TARGETS]->(t)
"""

SAVE_SCENES = """
UNWIND $scenes AS s
MATCH (d:Demo {id: $demo_id}), (p:Page {id: s.page_id})
CREATE (d)-[:HAS_SCENE {index: s.index}]->(sc:Scene {
  id: s.id, index: s.index, kind: s.kind, narration: s.narration, steps: s.steps,
  screenshot: s.screenshot, zoom: s.zoom, page_title: s.page_title
})
CREATE (sc)-[:ON_PAGE]->(p)
"""

SAVE_CITATIONS = """
UNWIND $cites AS c
MATCH (sc:Scene {id: c.scene_id}), (e:Element {id: c.fact_id})
CREATE (sc)-[:CITES {role: c.role, hash: c.hash, text: c.text}]->(e)
"""

LINK_SCENES = """
UNWIND $pairs AS pair
MATCH (a:Scene {id: pair[0]}), (b:Scene {id: pair[1]})
CREATE (a)-[:NEXT]->(b)
"""

# A scene is stale when anything it cites now says something else, or is gone.
DEMO_HEALTH = """
MATCH (d:Demo)-[:HAS_SCENE]->(s:Scene)-[c:CITES]->(e:Element)
WITH d, s, c.hash <> e.content_hash OR coalesce(e.removed, false) AS broken
WITH d, s, max(CASE WHEN broken THEN 1 ELSE 0 END) AS stale
RETURN d.id, d.goal, d.created_at, count(s) AS scenes, sum(stale) AS stale_scenes
ORDER BY d.created_at DESC
"""

STALE_CITATIONS = """
MATCH (d:Demo {id: $demo_id})-[:HAS_SCENE]->(s:Scene)-[c:CITES]->(e:Element)
WHERE c.hash <> e.content_hash OR coalesce(e.removed, false)
RETURN s.index, s.kind, c.role, c.text, e.text, coalesce(e.removed, false)
ORDER BY s.index
"""

# --- Recorder ----------------------------------------------------------------
#
#   (:Video)-[:RENDERS]->(:Demo)
#   (:Video)-[:VERIFIED_AGAINST]->(:Crawl)
#   (:Video)-[:SHOWS {start, end}]->(:Scene)          seconds of the video each scene occupies

DEMO_INFO = """
MATCH (d:Demo {id: $demo_id})-[:PLANNED_FROM]->(c:Crawl)
OPTIONAL MATCH (d)-[:TARGETS]->(t:Element)
RETURN d.goal, d.start_path, c.id, t.text
"""

# Everything needed to replay a demo: steps, the page each scene is on, and what it cites,
# with the element's current hash so stale citations are caught before recording.
DEMO_SCENES = """
MATCH (:Demo {id: $demo_id})-[:HAS_SCENE]->(s:Scene)-[:ON_PAGE]->(p:Page)
OPTIONAL MATCH (s)-[c:CITES]->(e:Element)
WITH s, p, collect({fact_id: e.id, role: c.role, text: c.text, hash: c.hash, current: e.content_hash,
                    removed: coalesce(e.removed, false), selector: e.selector, kind: e.kind}) AS cites
RETURN s.id, s.index, s.kind, s.narration, s.steps, p.url, p.path, s.page_title, cites, p.title
ORDER BY s.index
"""

SAVE_VIDEO = """
MATCH (d:Demo {id: $demo_id}), (c:Crawl {id: $crawl_id})
CREATE (v:Video {id: $video_id, path: $path, duration: $duration, rendered_at: $now, voice: $voice,
                 raw_recording: $raw, captions: $srt, verified: $verified})
CREATE (v)-[:RENDERS]->(d)
CREATE (v)-[:VERIFIED_AGAINST]->(c)
WITH v
UNWIND $timeline AS t
MATCH (s:Scene {id: t.scene_id})
CREATE (v)-[:SHOWS {start: t.start, end: t.end}]->(s)
"""

# Which seconds of which rendered videos now show something the product no longer says.
STALE_VIDEO_SEGMENTS = """
MATCH (v:Video)-[sh:SHOWS]->(s:Scene)-[c:CITES]->(e:Element)
WHERE c.hash <> e.content_hash OR coalesce(e.removed, false)
RETURN v.id, v.path, s.index, sh.start, sh.end, c.text, e.text, coalesce(e.removed, false), c.role
ORDER BY v.id, sh.start
"""

DEMO_VIDEOS = """
MATCH (v:Video)-[:RENDERS]->(d:Demo {id: $demo_id})
RETURN v.id, v.path, v.duration, v.rendered_at
ORDER BY v.rendered_at DESC
"""
