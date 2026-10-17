"""Ship a 'release' of Ledgerly by editing a working copy of its catalog.

    python sample_app/mutate.py price pro 35
    python sample_app/mutate.py label new_invoice "Create an invoice"
    python sample_app/mutate.py reset

The committed catalog.json is never modified.
"""

import argparse
import json
import os
import shutil
from pathlib import Path

from server import DEFAULT_CATALOG, WORKING_CATALOG


def target() -> Path:
    return Path(os.environ["LEDGERLY_CATALOG"]) if os.getenv("LEDGERLY_CATALOG") else WORKING_CATALOG


def load() -> dict:
    path = target()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(DEFAULT_CATALOG, path)
    return json.loads(path.read_text(encoding="utf-8"))


def save(c: dict) -> None:
    target().write_text(json.dumps(c, indent=2), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("price")
    p.add_argument("plan")
    p.add_argument("amount", type=float)
    lb = sub.add_parser("label")
    lb.add_argument("key")
    lb.add_argument("text")
    sub.add_parser("reset")
    args = ap.parse_args()

    if args.cmd == "reset":
        if target().exists() and target() != DEFAULT_CATALOG:
            target().unlink()
        print("Ledgerly catalog reset to the committed version.")
        return

    c = load()
    if args.cmd == "price":
        plan = next(p for p in c["plans"] if p["id"] == args.plan)
        old = plan["price"]
        plan["price"] = int(args.amount) if args.amount.is_integer() else args.amount
        print(f"{plan['name']} price: {old} -> {plan['price']}")
    elif args.cmd == "label":
        old = c["labels"][args.key]
        c["labels"][args.key] = args.text
        print(f"label {args.key}: {old!r} -> {args.text!r}")
    save(c)


if __name__ == "__main__":
    main()
