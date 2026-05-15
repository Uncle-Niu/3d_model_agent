from pathlib import Path

from backend.cad import example_bank


def test_example_bank_retrieves_relevant_cadquery_snippet(tmp_path, monkeypatch):
    root = tmp_path / "cad_sources"
    example_dir = root / "repo" / "examples"
    example_dir.mkdir(parents=True)
    (example_dir / "support_stand.py").write_text(
        """
import cadquery as cq

base = cq.Workplane("XY").box(90, 80, 5)
back = cq.Workplane("XY").box(80, 6, 90)
result = base.union(back)
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(example_bank, "SOURCE_ROOT", root)
    example_bank.load_example_bank.cache_clear()

    hits = example_bank.retrieve_example_snippets("support stand", max_snippets=3)

    assert hits
    assert hits[0].path == "repo/examples/support_stand.py"
    assert "cq.Workplane" in hits[0].excerpt


def test_example_bank_cadquery_only_filters_build123d(tmp_path, monkeypatch):
    root = tmp_path / "cad_sources"
    example_dir = root / "repo" / "examples"
    example_dir.mkdir(parents=True)
    (example_dir / "mount_build123d.py").write_text(
        """
from build123d import *

with BuildPart() as wall_mount:
    Box(90, 80, 5)
""",
        encoding="utf-8",
    )
    (example_dir / "mount_cadquery.py").write_text(
        """
import cadquery as cq

result = cq.Workplane("XY").box(90, 80, 5)
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(example_bank, "SOURCE_ROOT", root)
    example_bank.load_example_bank.cache_clear()

    hits = example_bank.retrieve_example_snippets("wall mount", cadquery_only=True)

    assert [hit.path for hit in hits] == ["repo/examples/mount_cadquery.py"]


def test_example_bank_prompt_context_warns_not_to_copy_external_imports(tmp_path, monkeypatch):
    root = tmp_path / "cad_sources"
    example_dir = root / "repo" / "examples"
    example_dir.mkdir(parents=True)
    (example_dir / "mount.py").write_text(
        """
import cadquery as cq

result = cq.Workplane("XY").box(40, 20, 5).faces(">Z").workplane().hole(4)
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(example_bank, "SOURCE_ROOT", root)
    example_bank.load_example_bank.cache_clear()

    context = example_bank.build_example_bank_prompt_context("mount with holes")

    assert "Retrieved Local CAD Example Bank" in context
    assert "Do not copy external-only imports" in context
    assert "repo/examples/mount.py" in context
