"""
Download free, redistributable taxonomy / parts-list datasets that we can
mine for recipe candidates. Everything here is a one-time pull — runtime use
is fully offline.

Storage:  data/recipe_sources/<dataset_id>/

Sources (all CC-licensed or public domain):

  google_product_taxonomy
      Google's public product category tree (~5500 categories). The
      "Hardware > ..." subtree is gold for FDM-printable item names.
      License: CC-BY.

  wikipedia_mechanical_components
      Wikipedia article "List of mechanical components" + a few sibling
      lists. Names + one-line descriptions, scraped from the public REST
      API once. License: CC-BY-SA.

  printables_categories_seed
      Static seed list of Printables.com top-level + second-level
      categories. Hand-curated, not scraped — used as a category lattice
      for the LLM recipe expander. Ships in-repo, no download.

  cadquery_example_index
      Filesystem scan of the cloned data/cad_sources/* repos, producing a
      JSON catalog of every example .py with its docstring title. Each
      entry becomes a recipe candidate. No network needed.

Run idempotently — already-downloaded files are skipped.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data" / "recipe_sources"


# ----------------------------------------------------------------------------
# 1. Google Product Taxonomy (CC-BY, ~5500 categories, free direct download)
# ----------------------------------------------------------------------------

GPT_URL = "https://www.google.com/basepages/producttype/taxonomy-with-ids.en-US.txt"


def fetch_gpt(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "taxonomy-with-ids.en-US.txt"
    if raw.exists() and raw.stat().st_size > 100_000:
        print(f"  [skip] {raw} already present ({raw.stat().st_size:,} bytes)")
        return
    print(f"  [http] GET {GPT_URL}")
    with urllib.request.urlopen(GPT_URL, timeout=60) as resp:
        data = resp.read()
    raw.write_bytes(data)
    print(f"  [ok ] wrote {len(data):,} bytes to {raw}")

    # Also emit a JSON of just the leaf names + their full path — easier
    # for the agent to consume.
    leaves = []
    for line in data.decode("utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        # Format: "id - A > B > C"
        if " - " not in line:
            continue
        cid, path = line.split(" - ", 1)
        parts = [p.strip() for p in path.split(">")]
        leaves.append({"id": int(cid), "path": parts, "leaf": parts[-1]})
    out = out_dir / "leaves.json"
    out.write_text(json.dumps(leaves, indent=2))
    print(f"  [ok ] wrote {len(leaves):,} leaves to {out}")


# ----------------------------------------------------------------------------
# 2. Wikipedia "List of ..." article extracts (CC-BY-SA, REST API)
# ----------------------------------------------------------------------------

WIKI_TITLES = [
    "List_of_mechanical,_electrical_and_electronic_equipment",
    "Machine_element",
    "Fastener",
    "Bracket_(architecture)",
    "Bushing_(isolator)",
    "Bearing_(mechanical)",
    "Gear",
    "Pulley",
    "Hinge",
    "Knob",
    "Handle",
    "Clamp_(tool)",
    "Mount_(machine)",
    "Standoff",
    "Spacer_(disambiguation)",
    "Enclosure",
    "Connector",
    "Coupling",
    "Outline_of_machines",
]


def fetch_wikipedia(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "_index.json"
    seen: dict[str, str] = json.loads(index_path.read_text()) if index_path.exists() else {}
    for title in WIKI_TITLES:
        target = out_dir / f"{title}.json"
        if target.exists():
            print(f"  [skip] {title}")
            continue
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
        print(f"  [http] GET {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MissionCrafter/0.1 (recipe-seed download)"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
            target.write_text(body)
            seen[title] = "ok"
            print(f"  [ok ] wrote {target}")
        except Exception as exc:
            seen[title] = f"error: {exc!r}"
            print(f"  [err] {title}: {exc!r}")
        time.sleep(0.3)  # be polite to Wikipedia
    index_path.write_text(json.dumps(seen, indent=2))


# ----------------------------------------------------------------------------
# 3. Printables.com category seed (static)
# ----------------------------------------------------------------------------

PRINTABLES_SEED = {
    "household": [
        "kitchen tools", "bathroom organizers", "cable management", "wall hooks",
        "drawer organizers", "lid openers", "trash can accessories", "shoe storage",
        "key holders", "umbrella stand", "candle holders", "vase",
    ],
    "electronics": [
        "phone stand", "phone wall mount", "tablet stand", "laptop stand",
        "headphone stand", "controller holder", "remote holder", "cable clip",
        "charger dock", "earbud case", "switch dock", "tv mount accessory",
        "monitor riser", "monitor arm accessory", "vesa adapter",
    ],
    "tools": [
        "wrench holder", "drill bit organizer", "screwdriver holder",
        "tape dispenser", "vise jaw insert", "drill press jig",
        "marking gauge", "drawer pull", "pegboard hook",
    ],
    "office": [
        "pen holder", "pencil cup", "monitor stand", "headset hanger",
        "cable tray", "business card holder", "label clip",
    ],
    "garden": [
        "plant pot", "self-watering planter", "seed starter tray",
        "garden marker", "hose holder", "rain gauge",
    ],
    "hobby": [
        "paint brush holder", "dice tower", "miniature base", "bookend",
        "puzzle holder", "model display stand", "card box",
    ],
    "outdoor": [
        "bottle opener", "carabiner", "tent stake", "bike accessory",
        "scooter accessory", "skateboard accessory",
    ],
    "mechanical": [
        "linear rail holder", "v-slot bracket", "stepper motor mount",
        "fan duct", "pulley", "gear set", "hinge", "thumbscrew",
        "knob", "spacer", "standoff", "cable gland",
    ],
    "automotive": [
        "dashboard holder", "vent mount", "trunk organizer", "cup holder insert",
        "license plate frame", "key fob shell",
    ],
}


def write_printables(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "categories.json"
    target.write_text(json.dumps(PRINTABLES_SEED, indent=2))
    total = sum(len(v) for v in PRINTABLES_SEED.values())
    print(f"  [ok ] wrote {total} category names across {len(PRINTABLES_SEED)} families to {target}")


# ----------------------------------------------------------------------------
# 4. CadQuery example-bank scan (filesystem only, no download)
# ----------------------------------------------------------------------------

CAD_SOURCES_ROOT = Path(__file__).resolve().parents[1] / "data" / "cad_sources"


def scan_cadquery_examples(out_dir: Path) -> None:
    if not CAD_SOURCES_ROOT.exists():
        print(f"  [skip] {CAD_SOURCES_ROOT} not present — run bootstrap_cad_sources.ps1 first")
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for py in CAD_SOURCES_ROOT.rglob("*.py"):
        rel = py.relative_to(CAD_SOURCES_ROOT).as_posix()
        title = py.stem.replace("_", " ")
        docstring = None
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="ignore"))
            mod_doc = ast.get_docstring(tree)
            if mod_doc:
                docstring = mod_doc.strip().splitlines()[0][:200]
        except Exception:
            pass
        entries.append({
            "path": rel,
            "title": title,
            "docstring_line": docstring,
            "size_bytes": py.stat().st_size,
        })
    target = out_dir / "examples.json"
    target.write_text(json.dumps(entries, indent=2))
    print(f"  [ok ] indexed {len(entries):,} CadQuery example files into {target}")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip", action="append", default=[],
                        choices=["gpt", "wiki", "printables", "cadq_index"])
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)

    print("== google_product_taxonomy ==")
    if "gpt" not in args.skip:
        fetch_gpt(ROOT / "google_product_taxonomy")

    print("\n== wikipedia_mechanical_components ==")
    if "wiki" not in args.skip:
        fetch_wikipedia(ROOT / "wikipedia_mechanical_components")

    print("\n== printables_categories_seed ==")
    if "printables" not in args.skip:
        write_printables(ROOT / "printables_categories_seed")

    print("\n== cadquery_example_index ==")
    if "cadq_index" not in args.skip:
        scan_cadquery_examples(ROOT / "cadquery_example_index")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
