"""Indexes and constraints for a product graph. Safe to run repeatedly."""

from .db import GraphStore

UNIQUE_NODE_IDS = ["Site", "Crawl", "Page", "Element", "Agent", "Demo", "Scene", "Video"]

NODE_INDEXES = [
    ("Crawl", "status"),
    ("Page", "path"),
    ("Element", "kind"),
]

EDGE_INDEXES = [
    ("FRONTIER", "status"),
    ("LEADS_TO", "via"),
    ("CHANGED_IN", "kind"),
]


def _already_exists(err: Exception) -> bool:
    msg = str(err).lower()
    return "already" in msg or "exists" in msg


async def apply_schema(store: GraphStore) -> list[str]:
    applied: list[str] = []
    # A graph key only exists after its first write; constraints need the key.
    await store.query("MERGE (:Site {id: '__schema__'})")
    await store.query("MATCH (s:Site {id: '__schema__'}) DELETE s")

    for label in UNIQUE_NODE_IDS:
        try:
            await store.graph.create_node_unique_constraint(label, "id")
            applied.append(f"UNIQUE {label}.id")
        except Exception as e:
            if not _already_exists(e):
                raise

    for label, prop in NODE_INDEXES:
        try:
            await store.graph.create_node_range_index(label, prop)
            applied.append(f"INDEX {label}.{prop}")
        except Exception as e:
            if not _already_exists(e):
                raise

    for rel, prop in EDGE_INDEXES:
        try:
            await store.graph.create_edge_range_index(rel, prop)
            applied.append(f"INDEX [:{rel}].{prop}")
        except Exception as e:
            if not _already_exists(e):
                raise

    # Full-text search over what the product says, for finding features by name later.
    try:
        await store.query("CALL db.idx.fulltext.createNodeIndex('Element', 'text')")
        applied.append("FULLTEXT Element.text")
    except Exception as e:
        if not _already_exists(e):
            raise
    return applied
