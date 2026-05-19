"""
Local CAD example-bank retrieval.

This scans cloned CAD source repositories under ``data/cad_sources`` and returns
small, relevant source snippets for prompt-time RAG. The snippets are references,
not dependencies: generated code is still expected to use the sandboxed CadQuery
API unless the project explicitly supports more imports later.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re


SOURCE_ROOT = Path("data") / "cad_sources"
SUPPORTED_SUFFIXES = {".py", ".md", ".rst", ".scad"}
SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules", "images", "_static"}
MAX_FILE_CHARS = 80_000
MAX_INDEX_FILES = 2_500

CAD_KEYWORDS = {
    "cadquery",
    "cq.",
    "workplane",
    "box(",
    "cylinder(",
    "extrude(",
    "revolve(",
    "loft(",
    "sweep(",
    "shell(",
    "fillet(",
    "chamfer(",
    "hole(",
    "cut(",
    "union(",
    "assembly",
    "buildpart",
    "buildsketch",
    "openscad",
}

QUERY_EXPANSIONS = {
    "holder": {"stand", "mount", "clip", "bracket", "support", "cradle"},
    "mount": {"bracket", "support", "plate", "holes", "fastener"},
    "bracket": {"mount", "support", "holes", "gusset", "rib"},
    "enclosure": {"case", "box", "lid", "shell", "standoff", "port"},
    "case": {"enclosure", "box", "lid", "shell", "cover"},
    "tray": {"organizer", "bin", "box", "compartment", "shell"},
    "gear": {"sprocket", "tooth", "chain", "thread"},
}

STOPWORDS = {
    "a",
    "an",
    "and",
    "for",
    "from",
    "generate",
    "make",
    "model",
    "of",
    "pro",
    "the",
    "to",
    "with",
}


@dataclass(frozen=True)
class ExampleBankEntry:
    path: str
    title: str
    text: str
    tokens: frozenset[str]
    is_cadquery: bool
    is_build123d: bool


@dataclass(frozen=True)
class RetrievedExample:
    path: str
    title: str
    excerpt: str
    score: int
    source_kind: str


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", text.lower()))


def _expanded_query_tokens(user_message: str) -> set[str]:
    tokens = _tokens(user_message) - STOPWORDS
    expanded = set(tokens)
    for token in tokens:
        expanded.update(QUERY_EXPANSIONS.get(token, set()))
    return expanded


def _iter_source_files(root: Path) -> list[Path]:
    if not root.exists():
        return []

    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        paths.append(path)

    def sort_key(path: Path) -> tuple[int, str]:
        rel = path.relative_to(root).as_posix().lower()
        priority = 0
        if "/examples/" in f"/{rel}" or "/demos/" in f"/{rel}":
            priority -= 5
        if "/docs/" in f"/{rel}" or "/doc/" in f"/{rel}":
            priority -= 2
        if "/tests/" in f"/{rel}":
            priority += 4
        return priority, rel

    return sorted(paths, key=sort_key)[:MAX_INDEX_FILES]


@lru_cache(maxsize=1)
def load_example_bank() -> tuple[ExampleBankEntry, ...]:
    """Load a lightweight in-memory index of local CAD source files."""
    entries: list[ExampleBankEntry] = []
    root = SOURCE_ROOT
    for path in _iter_source_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:MAX_FILE_CHARS]
        except OSError:
            continue

        lowered = text.lower()
        if not any(keyword in lowered for keyword in CAD_KEYWORDS) and path.suffix.lower() != ".scad":
            continue

        rel = path.relative_to(root).as_posix()
        is_cadquery = "import cadquery" in lowered or "cq.workplane" in lowered or "from cadquery" in lowered
        is_build123d = "build123d" in lowered or "buildpart" in lowered or "buildsketch" in lowered
        entries.append(
            ExampleBankEntry(
                path=rel,
                title=Path(rel).stem.replace("_", " ").replace("-", " "),
                text=text,
                tokens=frozenset(_tokens(rel + "\n" + text[:8000])),
                is_cadquery=is_cadquery,
                is_build123d=is_build123d,
            )
        )
    return tuple(entries)


def _score_entry(entry: ExampleBankEntry, query_tokens: set[str]) -> int:
    path_tokens = _tokens(entry.path)
    score = 0
    score += 8 * len(query_tokens & path_tokens)
    score += 2 * len(query_tokens & set(entry.tokens))

    rel = entry.path.lower()
    if "/examples/" in f"/{rel}" or "/demos/" in f"/{rel}":
        score += 8
    if entry.is_cadquery:
        score += 5
    if entry.is_build123d:
        score += 2
    if "/tests/" in f"/{rel}":
        score -= 4
    return score


def _excerpt(entry: ExampleBankEntry, query_tokens: set[str], max_lines: int = 38) -> str:
    lines = entry.text.splitlines()
    if not lines:
        return ""

    relevant_line = 0
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(token in lower for token in query_tokens) or any(keyword in lower for keyword in CAD_KEYWORDS):
            relevant_line = i
            break

    start = max(0, relevant_line - 6)
    end = min(len(lines), start + max_lines)
    snippet = "\n".join(lines[start:end]).strip()
    if len(snippet) > 2500:
        snippet = snippet[:2500].rstrip() + "\n..."
    return snippet


def retrieve_example_snippets(
    user_message: str,
    max_snippets: int = 5,
    cadquery_only: bool = False,
) -> list[RetrievedExample]:
    """Return compact, relevant snippets from the local example bank."""
    query_tokens = _expanded_query_tokens(user_message)
    candidates: list[tuple[int, ExampleBankEntry]] = []

    for entry in load_example_bank():
        if cadquery_only and not entry.is_cadquery:
            continue
        score = _score_entry(entry, query_tokens)
        if score > 0:
            candidates.append((score, entry))

    candidates.sort(key=lambda item: (-item[0], item[1].path))
    results: list[RetrievedExample] = []
    seen_roots: set[str] = set()
    for score, entry in candidates:
        # Keep repo diversity when possible.
        repo_root = entry.path.split("/", 1)[0]
        if len(seen_roots) < max_snippets // 2 and repo_root in seen_roots:
            continue
        seen_roots.add(repo_root)
        source_kind = "CadQuery" if entry.is_cadquery else "build123d" if entry.is_build123d else "CAD source"
        results.append(
            RetrievedExample(
                path=entry.path,
                title=entry.title,
                excerpt=_excerpt(entry, query_tokens),
                score=score,
                source_kind=source_kind,
            )
        )
        if len(results) >= max_snippets:
            break

    # If diversity filtering was too aggressive, fill from the remaining top hits.
    if len(results) < max_snippets:
        used_paths = {result.path for result in results}
        for score, entry in candidates:
            if entry.path in used_paths:
                continue
            source_kind = "CadQuery" if entry.is_cadquery else "build123d" if entry.is_build123d else "CAD source"
            results.append(
                RetrievedExample(
                    path=entry.path,
                    title=entry.title,
                    excerpt=_excerpt(entry, query_tokens),
                    score=score,
                    source_kind=source_kind,
                )
            )
            if len(results) >= max_snippets:
                break

    return results


def build_example_bank_prompt_context(
    user_message: str,
    max_snippets: int = 2,
    cadquery_only: bool = False,
    max_chars: int = 2200,
) -> str:
    """Format retrieved example snippets for LLM prompts."""
    snippets = retrieve_example_snippets(
        user_message,
        max_snippets=max_snippets,
        cadquery_only=cadquery_only,
    )
    if not snippets:
        return ""

    lines = [
        "## Retrieved Local CAD Example Bank",
        "Use these as pattern references. Do not copy external-only imports; adapt the modeling strategy to the allowed CadQuery sandbox.",
    ]
    for item in snippets:
        lines.append(f"### {item.title} ({item.source_kind})")
        lines.append(f"Source: data/cad_sources/{item.path}")
        lines.append("```python" if item.path.endswith(".py") else "```text")
        lines.append(item.excerpt)
        lines.append("```")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n..."
    return text
