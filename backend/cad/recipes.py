"""
Local CAD recipe retrieval and plan-quality checks.

The recipes are compact product/archetype cards. They are not runtime CAD
dependencies; they give the planner and verifier the missing "what should this
object contain?" prior before any CadQuery code is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from ..domain.models import DesignPlan


@dataclass(frozen=True)
class CadRecipe:
    recipe_id: str
    title: str
    tags: tuple[str, ...]
    source_refs: tuple[str, ...]
    required_features: tuple[str, ...]
    negative_space_features: tuple[str, ...] = ()
    construction_strategy: tuple[str, ...] = ()
    cadquery_patterns: tuple[str, ...] = ()
    validation_rules: tuple[str, ...] = ()
    feature_keywords: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanQualityReport:
    is_sufficient: bool
    missing_features: tuple[str, ...] = ()
    missing_negative_space: tuple[str, ...] = ()
    feedback: str = ""


RECIPE_SOURCE_ROOT = Path("data") / "cad_sources"


RECIPES: tuple[CadRecipe, ...] = (
    CadRecipe(
        recipe_id="phone_holder_desktop",
        title="Desktop phone holder / charging stand",
        tags=("phone", "iphone", "holder", "stand", "cradle", "dock", "charger"),
        source_refs=(
            "cadquery-contrib examples: tray, reinforce junction, remote enclosure",
            "build123d examples: pegboard hook, handle, multiple workplanes",
            "OpenSCAD MCAD patterns: triangles/gussets, polyholes/printable holes",
        ),
        required_features=(
            "stable base plate sized wider/deeper than the phone footprint",
            "angled backrest so the phone leans, not a vertical slab",
            "front retaining lip or ledge that prevents sliding",
            "case-friendly clearance slot or cradle sized from phone width/thickness",
            "two separated front supports or side guides that leave access in the middle",
            "rounded/chamfered printable edges",
        ),
        negative_space_features=(
            "center charging-cable notch cut through the front lip/base",
        ),
        construction_strategy=(
            "Prefer a side-profile polyline extruded across width for the base, front lip, and angled backrest.",
            "Use boolean cuts for the cable notch and clearance reliefs.",
            "Add triangular ribs/gussets or side cheeks if the backrest is tall.",
            "Declare phone dimensions and case clearance as named parameters.",
        ),
        cadquery_patterns=(
            'cq.Workplane("YZ").polyline(profile).close().extrude(width)',
            "body.cut(cable_notch_cutter)",
            "base.union(backrest).union(front_lip)",
            ".edges().fillet(fillet_radius) or .edges().chamfer(chamfer_size)",
        ),
        validation_rules=(
            "A base plus one plain box/slab is insufficient for a phone holder.",
            "The cable notch must be visibly open from the front/top unless explicitly omitted.",
            "The slot must exceed phone thickness plus case clearance.",
        ),
        feature_keywords={
            "stable base plate sized wider/deeper than the phone footprint": ("base", "plate", "footprint", "stable"),
            "angled backrest so the phone leans, not a vertical slab": ("angled", "backrest", "lean", "tilt", "support"),
            "front retaining lip or ledge that prevents sliding": ("front", "lip", "ledge", "stop", "retaining"),
            "case-friendly clearance slot or cradle sized from phone width/thickness": ("slot", "cradle", "clearance", "case", "thickness"),
            "two separated front supports or side guides that leave access in the middle": ("side", "guide", "cheek", "rail", "separated", "supports"),
            "rounded/chamfered printable edges": ("fillet", "chamfer", "rounded", "edge"),
            "center charging-cable notch cut through the front lip/base": ("cable", "charging", "notch", "cut", "center"),
        },
    ),
    CadRecipe(
        recipe_id="tray_or_organizer",
        title="Tray / organizer / bin",
        tags=("tray", "organizer", "holder", "bin", "drawer", "gridfinity", "compartment"),
        source_refs=(
            "cadquery-contrib examples: tray.py, hexagonal_drawers",
            "awesome-cadquery: cq-gridfinity boxes and drawer spacers",
        ),
        required_features=(
            "outer tray body with stable bottom",
            "raised perimeter walls with printable wall thickness",
            "rounded internal and external corners",
        ),
        negative_space_features=(
            "open cavity or compartments cut/shelled from the body",
        ),
        construction_strategy=(
            "Use shell or explicit inner cutouts for the cavity.",
            "For organizers, pattern dividers or compartments with named parameters.",
        ),
        cadquery_patterns=("body.faces('>Z').shell(-wall)", "body.cut(inner_cavity)"),
        validation_rules=("A tray must have an open usable volume, not a solid block.",),
    ),
    CadRecipe(
        recipe_id="bracket_or_mount",
        title="Bracket / mount / support",
        tags=("bracket", "mount", "holder", "support", "clip", "fixture"),
        source_refs=(
            "cadquery-contrib examples: 3D_Printer_Extruder_Support, Panel_with_Various_Holes",
            "build123d examples: pillow block, din rail, pegboard hook",
        ),
        required_features=(
            "primary load-bearing plate or body",
            "mounting holes or fastener interfaces",
            "ribs/gussets or thickened junctions for stiffness",
            "fillets/chamfers at stress concentrations",
        ),
        negative_space_features=(
            "through holes, slots, or counterbores for fasteners",
        ),
        construction_strategy=(
            "Model plates and uprights as separate named bodies, then union.",
            "Add ribs as triangular extrusions near inside corners.",
            "Cut mounting holes after the main body exists.",
        ),
        cadquery_patterns=(".pushPoints(points).hole(diameter)", "profile.polyline(...).extrude(width)"),
        validation_rules=("A mount without fastener features is usually incomplete.",),
    ),
    CadRecipe(
        recipe_id="enclosure",
        title="Electronics enclosure / case",
        tags=("enclosure", "case", "cover", "lid", "electronics", "remote"),
        source_refs=(
            "cadquery-contrib examples: Parametric_Enclosure, Remote_Enclosure",
            "build123d examples: circuit board with holes",
        ),
        required_features=(
            "outer shell with named wall thickness",
            "open cavity or removable lid strategy",
            "mounting bosses or board standoffs when electronics are implied",
            "ports/cutouts for connectors when applicable",
            "edge fillets/chamfers",
        ),
        negative_space_features=(
            "internal cavity created by shell or boolean cut",
            "connector/port cutouts when requested",
        ),
        construction_strategy=(
            "Use shell for simple open-top boxes; use explicit inner cutters for controlled cavities.",
            "Create bosses as cylinders and cut screw holes through them.",
        ),
        cadquery_patterns=("body.faces('>Z').shell(-wall)", "boss.faces('>Z').workplane().hole(screw_d)"),
        validation_rules=("A solid box is not a usable enclosure.",),
    ),
)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _combined_plan_text(plan: DesignPlan) -> str:
    parts: list[str] = [
        plan.summary,
        " ".join(plan.key_features),
        " ".join(plan.assumptions),
        " ".join(plan.risks),
        " ".join(plan.parameters.keys()),
    ]
    for component in plan.components:
        parts.extend(
            [
                component.name,
                component.description,
                component.primitive,
                component.operation,
                " ".join(component.dimensions.keys()),
            ]
        )
    return " ".join(p for p in parts if p)


def retrieve_recipe_cards(user_message: str, max_cards: int = 3) -> list[CadRecipe]:
    """Return the most relevant local recipe cards for a request."""
    query_tokens = _tokens(user_message)
    scored: list[tuple[int, CadRecipe]] = []
    for recipe in RECIPES:
        tag_hits = sum(1 for tag in recipe.tags if tag in query_tokens or tag.replace("_", " ") in user_message.lower())
        keyword_hits = 0
        for keywords in recipe.feature_keywords.values():
            keyword_hits += sum(1 for keyword in keywords if keyword in query_tokens)
        score = tag_hits * 5 + keyword_hits
        if score > 0:
            scored.append((score, recipe))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [recipe for _, recipe in scored[:max_cards]]


def discover_local_source_hints(user_message: str, max_hints: int = 6) -> list[str]:
    """Find relevant files in the optional cloned CAD source repos."""
    root = RECIPE_SOURCE_ROOT
    if not root.exists():
        return []

    query_tokens = _tokens(user_message)
    hints: list[tuple[int, str]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".py", ".md", ".scad"}:
            continue
        rel = path.relative_to(root).as_posix()
        rel_tokens = _tokens(rel)
        score = len(query_tokens & rel_tokens)
        # Generic high-signal CAD examples are useful even when filenames do not
        # directly match the request.
        if any(word in rel.lower() for word in ("tray", "enclosure", "hook", "handle", "hole", "support", "reinforce")):
            score += 1
        if score > 0:
            hints.append((score, rel))

    hints.sort(key=lambda item: (-item[0], item[1]))
    return [rel for _, rel in hints[:max_hints]]


def build_recipe_prompt_context(user_message: str, cards: list[CadRecipe] | None = None) -> str:
    """Render retrieved recipes as compact prompt context."""
    cards = cards if cards is not None else retrieve_recipe_cards(user_message)
    if not cards:
        return ""

    lines = ["## Retrieved CAD Recipe Context (treat as product-design ground truth)"]
    for card in cards:
        lines.append(f"### {card.title} (`{card.recipe_id}`)")
        lines.append("Required visible/function features:")
        lines.extend(f"- {feature}" for feature in card.required_features)
        if card.negative_space_features:
            lines.append("Required negative-space/cut features:")
            lines.extend(f"- {feature}" for feature in card.negative_space_features)
        if card.construction_strategy:
            lines.append("Recommended construction strategy:")
            lines.extend(f"- {strategy}" for strategy in card.construction_strategy)
        if card.cadquery_patterns:
            lines.append("CadQuery patterns to consider:")
            lines.extend(f"- `{pattern}`" for pattern in card.cadquery_patterns)
        if card.validation_rules:
            lines.append("Plan/render rejection rules:")
            lines.extend(f"- {rule}" for rule in card.validation_rules)
        if card.source_refs:
            lines.append("Pattern sources:")
            lines.extend(f"- {source}" for source in card.source_refs)

    source_hints = discover_local_source_hints(user_message)
    if source_hints:
        lines.append("Relevant files in cloned local CAD sources:")
        lines.extend(f"- data/cad_sources/{hint}" for hint in source_hints)

    return "\n".join(lines)


def validate_plan_against_recipes(plan: DesignPlan, cards: list[CadRecipe]) -> PlanQualityReport:
    """Check that the plan is detailed enough before code generation."""
    if not cards:
        return PlanQualityReport(is_sufficient=True)

    plan_text = _combined_plan_text(plan).lower()
    component_count = len(plan.components)
    key_feature_count = len(plan.key_features)
    missing: list[str] = []
    missing_negative: list[str] = []

    # Gate against the primary recipe only. Secondary cards are useful prompt
    # inspiration, but enforcing all of them can overconstrain broad words like
    # "holder" into unrelated requirements.
    for card in cards[:1]:
        for feature in card.required_features:
            keywords = card.feature_keywords.get(feature, ())
            if keywords and not any(keyword in plan_text for keyword in keywords):
                missing.append(feature)
            elif not keywords and feature.lower() not in plan_text:
                missing.append(feature)

        for feature in card.negative_space_features:
            keywords = card.feature_keywords.get(feature, ())
            has_keyword = any(keyword in plan_text for keyword in keywords) if keywords else feature.lower() in plan_text
            has_cut_component = any((component.operation or "").lower() == "cut" for component in plan.components)
            if not (has_keyword and has_cut_component):
                missing_negative.append(feature)

    if component_count < 3 and cards[0].recipe_id in {"phone_holder_desktop", "bracket_or_mount", "enclosure"}:
        missing.append("at least three named components/sub-shapes with dimensions")
    if key_feature_count < 4 and cards[0].recipe_id == "phone_holder_desktop":
        missing.append("a key-feature checklist detailed enough for visual verification")

    # Preserve order while removing duplicates.
    missing = list(dict.fromkeys(missing))
    missing_negative = list(dict.fromkeys(missing_negative))
    is_sufficient = not missing and not missing_negative

    feedback_lines = []
    if missing:
        feedback_lines.append("Missing required plan features:")
        feedback_lines.extend(f"- {item}" for item in missing)
    if missing_negative:
        feedback_lines.append("Missing required negative-space/cut features:")
        feedback_lines.extend(f"- {item}" for item in missing_negative)
    if feedback_lines:
        feedback_lines.append("Revise the plan before code generation. Do not solve this by producing a plain base plus slab.")

    return PlanQualityReport(
        is_sufficient=is_sufficient,
        missing_features=tuple(missing),
        missing_negative_space=tuple(missing_negative),
        feedback="\n".join(feedback_lines),
    )
