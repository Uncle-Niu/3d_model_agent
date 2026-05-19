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

from ..domain.models import DesignPlan, HardConstraints


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
    # Words in the USER prompt (not the plan text) that gate recipe-wide
    # requirements. The matcher already retrieves a recipe when ANY tag
    # appears, but a broad tag like "holder" should not be enough to force
    # every recipe-required feature on the plan. Recipes set this to the
    # narrower set of words that actually justify their hard requirements:
    # e.g. tray_or_organizer wants "tray/bin/drawer/organizer" before it
    # insists on a cavity. Empty tuple = always gate (legacy behavior).
    prompt_required_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanQualityReport:
    is_sufficient: bool
    missing_features: tuple[str, ...] = ()
    missing_negative_space: tuple[str, ...] = ()
    feedback: str = ""


def merge_plan_quality_reports(*reports: PlanQualityReport) -> PlanQualityReport:
    """Combine independent plan-quality gates into one repair signal."""
    missing_features: list[str] = []
    missing_negative_space: list[str] = []
    feedback: list[str] = []
    for report in reports:
        missing_features.extend(report.missing_features)
        missing_negative_space.extend(report.missing_negative_space)
        if report.feedback:
            feedback.append(report.feedback)
    missing_features = list(dict.fromkeys(missing_features))
    missing_negative_space = list(dict.fromkeys(missing_negative_space))
    return PlanQualityReport(
        is_sufficient=not missing_features and not missing_negative_space,
        missing_features=tuple(missing_features),
        missing_negative_space=tuple(missing_negative_space),
        feedback="\n\n".join(feedback),
    )


RECIPE_SOURCE_ROOT = Path("data") / "cad_sources"


GENERAL_REQUIREMENT_CUES: dict[str, tuple[str, ...]] = {
    "fastener interfaces": (
        "bolt",
        "screw",
        "mount",
        "bracket",
        "wall",
        "fixture",
        "attach",
        "fasten",
        "hole",
        "m3",
        "m4",
        "m5",
        "m6",
    ),
    "clearance and access cutouts": (
        "cable",
        "wire",
        "usb",
        "charger",
        "port",
        "access",
        "slot",
        "notch",
        "pass",
    ),
    "internal cavities or shells": (
        "box",
        "case",
        "enclosure",
        "container",
        "tray",
        "cup",
        "holder",
        "organizer",
        "bin",
    ),
    "retention geometry": (
        "clip",
        "clamp",
        "snap",
        "holder",
        "latch",
        "grip",
        "hook",
        "catch",
        "retainer",
    ),
    "load-bearing reinforcement": (
        "support",
        "stand",
        "bracket",
        "mount",
        "arm",
        "shelf",
        "holder",
        "heavy",
        "load",
    ),
    "moving or mating interfaces": (
        "hinge",
        "slide",
        "drawer",
        "lid",
        "cap",
        "joint",
        "gear",
        "bearing",
        "thread",
        "knob",
    ),
}


FEATURE_FAMILY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "fastener interfaces": (
        "Consider whether fastener geometry (through holes, counterbores, countersinks, slots, or bosses) is actually needed for this design. If the user did not ask the part to attach to anything, do NOT add mounting holes by default — declare so in <feature_decisions>.",
        "If fasteners ARE needed, include nominal diameters, clearances, and placement pattern dimensions as named parameters.",
    ),
    "clearance and access cutouts": (
        "Consider whether ports, cable paths, hand access, or reliefs apply to this design. If applicable, represent them as negative-space/cut components and list them in key_features so vision can verify them.",
    ),
    "internal cavities or shells": (
        "Consider whether the object implies storage, containment, or insertion. If yes, use shelling or explicit inner cutters and include wall thickness as a named parameter. If the user is asking for a solid display object, do not invent a cavity.",
    ),
    "retention geometry": (
        "If the part holds another object, consider lips, hooks, side guides, detents, clips, jaws, or stops to physically retain it — and include clearance/tolerance assumptions. If the user described 'a stand' or 'a tray' where the object simply rests, retention features may be unnecessary; document the choice in <feature_decisions>.",
    ),
    "load-bearing reinforcement": (
        "Consider whether the design carries directional load and would benefit from ribs, gussets, thickened junctions, broad bases, or triangular supports. Reinforce tall thin slabs/posts only when the expected load actually demands it.",
    ),
    "moving or mating interfaces": (
        "If parts move or mate, define mating clearances, pivots, rails, stops, thread/gear/bearing assumptions, and assembly orientation. Separate mating parts or named features so later edits can target them.",
    ),
}


# Map planner feature_decisions feature names to recipe requirement families.
# The planner emits coarse families like "fasteners_or_mounting_holes"; this
# table tells the recipe gate which feature-family text patterns those map to
# so an explicit "needed=false" decision can suppress a specific complaint.
FEATURE_DECISION_TO_FAMILY: dict[str, tuple[str, ...]] = {
    "fasteners_or_mounting_holes": ("fastener", "mounting", "hole", "screw", "bolt", "boss"),
    "internal_cavity_or_shell": ("cavity", "shell", "hollow", "compartment"),
    "retention_geometry": ("lip", "retention", "guide", "clip", "hook", "rim", "wall"),
    "load_bearing_reinforcement": ("rib", "gusset", "reinforce", "stiff", "junction"),
    "clearance_or_port_cutouts": ("port", "cutout", "cable", "access", "notch", "slot"),
    "moving_or_mating_interface": ("hinge", "thread", "rail", "slide", "bearing", "mate", "gear"),
}


RECIPES: tuple[CadRecipe, ...] = (
    CadRecipe(
        recipe_id="tray_or_organizer",
        title="Tray / organizer / bin",
        tags=("tray", "organizer", "bin", "drawer", "gridfinity", "compartment", "container"),
        # Cavity-required only fires when the prompt actually names a
        # container-shape. The bare word "holder" is too broad — "phone
        # holder for movies" is a stand, not a tray, and forcing a cavity
        # produces a horizontal pocket instead of a tilted backrest.
        prompt_required_keywords=("tray", "organizer", "bin", "drawer", "compartment", "container", "gridfinity", "cup", "cubby"),
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
        feature_keywords={
            "outer tray body with stable bottom": ("outer", "tray", "body", "stable", "bottom"),
            "raised perimeter walls with printable wall thickness": ("raised", "perimeter", "walls", "wall", "thickness"),
            "rounded internal and external corners": ("rounded", "fillet", "corner", "corners"),
            "open cavity or compartments cut/shelled from the body": ("open", "cavity", "compartment", "compartments", "shell", "shelled", "cut"),
        },
    ),
    CadRecipe(
        recipe_id="bracket_or_mount",
        title="Bracket / mount / support",
        tags=("bracket", "mount", "support", "clip", "fixture"),
        # Fastener-cuts and "mounting holes are required" only fire when the
        # prompt actually implies attaching to something. A free-standing
        # holder/stand has no parent surface — adding fastener holes to it is
        # a worse failure than omitting them.
        prompt_required_keywords=("bracket", "mount", "wall", "ceiling", "screw", "bolt", "fastener", "attach", "fix", "m3", "m4", "m5", "m6", "din"),
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
        feature_keywords={
            "primary load-bearing plate or body": ("primary", "load", "bearing", "plate", "body"),
            "mounting holes or fastener interfaces": ("mounting", "holes", "hole", "fastener", "interfaces"),
            "ribs/gussets or thickened junctions for stiffness": ("ribs", "rib", "gussets", "gusset", "thickened", "junction", "stiffness"),
            "fillets/chamfers at stress concentrations": ("fillets", "fillet", "chamfers", "chamfer", "stress", "concentrations"),
            "through holes, slots, or counterbores for fasteners": ("through", "holes", "slots", "counterbores", "fasteners", "cut"),
        },
    ),
    CadRecipe(
        recipe_id="enclosure",
        title="Electronics enclosure / case",
        tags=("enclosure", "case", "cover", "lid", "electronics", "remote"),
        # Enclosure-cavity-required only fires when the prompt actually
        # implies enclosing something. "Case" alone is enough; a generic
        # "holder" is not.
        prompt_required_keywords=("enclosure", "case", "cover", "lid", "shell", "housing", "box", "chassis"),
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
        feature_keywords={
            "outer shell with named wall thickness": ("outer", "shell", "wall", "thickness"),
            "open cavity or removable lid strategy": ("open", "cavity", "removable", "lid"),
            "mounting bosses or board standoffs when electronics are implied": ("mounting", "bosses", "boss", "board", "standoffs", "electronics"),
            "ports/cutouts for connectors when applicable": ("ports", "port", "cutouts", "connector", "connectors"),
            "edge fillets/chamfers": ("edge", "fillets", "fillet", "chamfers", "chamfer"),
            "internal cavity created by shell or boolean cut": ("internal", "cavity", "shell", "boolean", "cut"),
            "connector/port cutouts when requested": ("connector", "port", "cutouts", "cut"),
        },
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


def infer_requirement_families(user_message: str) -> list[str]:
    """Infer broad functional requirement families from the request text."""
    query_tokens = _tokens(user_message)
    inferred: list[tuple[int, str]] = []
    for family, cues in GENERAL_REQUIREMENT_CUES.items():
        hits = sum(1 for cue in cues if cue in query_tokens or cue in user_message.lower())
        if hits:
            inferred.append((hits, family))
    inferred.sort(key=lambda item: (-item[0], item[1]))
    return [family for _, family in inferred]


def build_adaptive_recipe_context(user_message: str) -> str:
    """Build a general recipe-synthesis rubric for any product request.

    Static recipe cards are intentionally limited. This adaptive context teaches
    the planner to synthesize a recipe from the user's object/function words and
    the retrieved example bank instead of waiting for a hand-authored card.

    The language here is intentionally permissive about optional features —
    the planner is expected to *decide* (via ``<feature_decisions>``) whether
    each family below applies, rather than mechanically adding every cue it
    sees in the request.
    """
    families = infer_requirement_families(user_message)
    lines = [
        "## Adaptive CAD Recipe Synthesis",
        "If no exact recipe exists, synthesize a product-specific recipe before planning. Use the user's object/function words and the retrieved local CAD examples to decide what features are required.",
        "A sufficient plan should describe:",
        "- Primary body or load path",
        "- Interfaces to the thing being held, mounted, enclosed, moved, or joined",
        "- Negative-space features (holes, slots, ports, cavities, notches, clearances, reliefs) WHEN they are functionally required — not just because the recipe mentions them",
        "- Manufacturing details: wall thickness, fillets/chamfers, ribs/gussets, print orientation, and named parameters",
        "- A key-feature checklist detailed enough for visual verification",
        "Reject placeholder designs that are only primitive boxes/cylinders when the request implies functional interfaces.",
        "Use `<feature_decisions>` to make the include/skip choice explicit for each optional feature family below. Adding fastener holes to a part that never attaches to anything is a worse failure than omitting them.",
    ]

    if families:
        lines.append("Inferred candidate feature families for this request (decide explicitly which apply):")
        for family in families:
            lines.append(f"### {family}")
            for requirement in FEATURE_FAMILY_REQUIREMENTS[family]:
                lines.append(f"- {requirement}")

    return "\n".join(lines)


def build_combined_recipe_context(user_message: str, cards: list[CadRecipe] | None = None) -> str:
    """Return static recipe cards plus the adaptive recipe-synthesis rubric."""
    parts = [
        build_recipe_prompt_context(user_message, cards),
        build_adaptive_recipe_context(user_message),
    ]
    return "\n\n".join(part for part in parts if part)


def _opted_out_keyword_patterns(plan: DesignPlan) -> set[str]:
    """Collect keyword fragments that the planner explicitly marked as
    not-needed via ``<feature_decisions>``. A required-feature is suppressed
    when its keyword list overlaps any of these patterns.
    """
    patterns: set[str] = set()
    for decision in getattr(plan, "feature_decisions", []) or []:
        if decision.needed:
            continue
        family_keys = FEATURE_DECISION_TO_FAMILY.get(decision.feature.lower(), ())
        patterns.update(family_keys)
    return patterns


def _feature_is_opted_out(feature: str, keywords: tuple[str, ...], opt_outs: set[str]) -> bool:
    """A feature is considered opted out when its keywords (or its own name)
    overlap with the planner's not-needed patterns.
    """
    if not opt_outs:
        return False
    haystack = list(keywords) + [feature.lower()]
    return any(any(p in word for p in opt_outs) for word in haystack)


def _prompt_satisfies_recipe(card: CadRecipe, user_message: str) -> bool:
    """True if the user's prompt actually justifies enforcing this recipe's
    hard requirements.

    A recipe is *retrieved* on any tag match (so its prompt-context still
    reaches the planner) but is only *gated against* the plan when the
    prompt contains one of its narrower ``prompt_required_keywords``. This
    avoids the historic failure where a generic word like "holder" matched
    the tray recipe and forced the planner to add a cavity to what was
    clearly a display stand.

    Recipes without any ``prompt_required_keywords`` retain the legacy
    always-gate behavior.
    """
    if not card.prompt_required_keywords:
        return True
    msg = (user_message or "").lower()
    if not msg:
        return True
    msg_tokens = _tokens(user_message)
    for kw in card.prompt_required_keywords:
        kw_lc = kw.lower()
        if kw_lc in msg_tokens or kw_lc in msg:
            return True
    return False


def validate_plan_against_recipes(
    plan: DesignPlan,
    cards: list[CadRecipe],
    user_message: str = "",
) -> PlanQualityReport:
    """Check that the plan is detailed enough before code generation.

    Respects ``plan.feature_decisions``: if the planner explicitly marked a
    feature family as not needed (e.g. no mounting holes on a phone stand
    that simply sits flat on a desk), the corresponding required-feature
    check is suppressed and the plan is not gated on it.

    Also respects ``card.prompt_required_keywords``: a recipe's hard
    requirements only apply when the user prompt contains a word that
    justifies them. This prevents broad tag matches (e.g. "holder" hitting
    the tray recipe) from forcing irrelevant features onto stand-like
    designs. ``user_message`` is the original user request — pass it
    through so the gating decision can see the true intent rather than only
    the planner-text echo.
    """
    plan_text = _combined_plan_text(plan).lower()
    component_count = len(plan.components)
    key_feature_count = len(plan.key_features)
    missing: list[str] = []
    missing_negative: list[str] = []
    opt_outs = _opted_out_keyword_patterns(plan)

    # Gate against the primary recipe only. Secondary cards are useful prompt
    # inspiration, but enforcing all of them can overconstrain broad words like
    # "holder" into unrelated requirements.
    for card in cards[:1]:
        # Skip gating entirely when the prompt does not justify it. The
        # recipe context still reaches the planner via the retrieved cards,
        # so the planner can choose to include cavity/fastener features if
        # they fit — but the plan won't be REJECTED for omitting them.
        if not _prompt_satisfies_recipe(card, user_message):
            continue

        for feature in card.required_features:
            keywords = card.feature_keywords.get(feature, ())
            if _feature_is_opted_out(feature, keywords, opt_outs):
                continue
            if keywords and not any(keyword in plan_text for keyword in keywords):
                missing.append(feature)
            elif not keywords and feature.lower() not in plan_text:
                missing.append(feature)

        for feature in card.negative_space_features:
            keywords = card.feature_keywords.get(feature, ())
            if _feature_is_opted_out(feature, keywords, opt_outs):
                continue
            has_keyword = any(keyword in plan_text for keyword in keywords) if keywords else feature.lower() in plan_text
            has_cut_component = any((component.operation or "").lower() == "cut" for component in plan.components)
            if not (has_keyword and has_cut_component):
                missing_negative.append(feature)

    if cards:
        primary = cards[0]
        if (
            component_count < 3
            and primary.recipe_id in {"bracket_or_mount", "enclosure", "tray_or_organizer"}
            and _prompt_satisfies_recipe(primary, user_message)
        ):
            missing.append("at least three named components/sub-shapes with dimensions")

    # Generic prose-vs-rotation consistency check. If a component description
    # mentions a tilt/angle in prose but no structured <rotation> tag was
    # emitted, the code generator will silently produce a vertical/horizontal
    # part — the prose has no effect on geometry. This check applies to every
    # product category, not just stands.
    tilt_words = ("angle", "angled", "tilt", "tilted", "lean", "leaning",
                   "recline", "reclined", "slant", "slanted", "pitched")
    prose_tilt_without_rotation: list[str] = []
    for component in plan.components:
        desc = (component.description or "").lower()
        name = (component.name or "")
        if component.rotation is not None:
            continue
        if any(w in desc for w in tilt_words):
            prose_tilt_without_rotation.append(name or "(unnamed)")
    if prose_tilt_without_rotation:
        missing.append(
            "structured <rotation> tag for components whose description says they are "
            f"angled/tilted/leaning: {', '.join(prose_tilt_without_rotation)}. "
            "Prose-only tilt descriptions are ignored by the code generator."
        )

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
        feedback_lines.append(
            "Revise the plan before code generation. If any of these are genuinely not needed for "
            "this design, list them as `needed=false` in <feature_decisions> with a clear rationale "
            "instead of silently dropping them. Do not solve this by producing a plain base plus slab."
        )

    return PlanQualityReport(
        is_sufficient=is_sufficient,
        missing_features=tuple(missing),
        missing_negative_space=tuple(missing_negative),
        feedback="\n".join(feedback_lines),
    )


def validate_plan_against_constraints(
    plan: DesignPlan,
    hard_constraints: HardConstraints,
) -> PlanQualityReport:
    """Reject single-body plans that cannot fit the configured print volume.

    The planner often writes an assembled real-world size first, then assumes
    the execution repair loop will shrink it if it is too large. That produces
    misleading plans and repeated scale patches. Catch it before code
    generation: either keep the single fused part inside the build envelope, or
    explicitly split the design into separately printable assembly parts.
    """
    if not plan:
        return PlanQualityReport(is_sufficient=True)

    max_dims = (
        float(hard_constraints.max_x_mm),
        float(hard_constraints.max_y_mm),
        float(hard_constraints.max_z_mm),
    )
    axis_names = ("X", "Y", "Z")
    missing: list[str] = []

    union_ops = sum(
        1 for c in plan.components
        if (c.operation or "").strip().lower() == "union"
    )
    union_connections = sum(
        1 for c in (plan.connections or [])
        if (c.kind or "").strip().lower() == "union"
    )
    # Most generated plans describe one fused print when they contain union
    # operations/connections. Plans with no unions may be true multi-part
    # assemblies; allow their assembled envelope to exceed the per-part cap.
    appears_single_fused_part = bool(union_ops or union_connections or len(plan.components) <= 1)

    if plan.components and not plan.overall_dimensions_mm:
        missing.append(
            "plan does not declare overall_dimensions_mm, so the agent cannot "
            "verify the design against the print-volume cap"
        )

    if (
        appears_single_fused_part
        and plan.overall_dimensions_mm
        and len(plan.overall_dimensions_mm) == 3
    ):
        for axis, planned, cap in zip(axis_names, plan.overall_dimensions_mm, max_dims):
            planned_f = float(planned)
            if planned_f > cap:
                missing.append(
                    f"plan overall {axis} dimension {planned_f:.1f}mm exceeds the "
                    f"{cap:.1f}mm print-volume cap for a single fused part"
                )

    for component in plan.components:
        op = (component.operation or "").strip().lower()
        if op in {"cut", "fillet", "chamfer", "shell", "pattern"}:
            continue
        if not component.dimensions:
            missing.append(
                f"component `{component.name}` does not declare dimensions, so "
                "the agent cannot verify it against the print-volume cap"
            )
            continue
        for dim_name, value in (component.dimensions or {}).items():
            value_f = float(value)
            if value_f > max(max_dims):
                missing.append(
                    f"component `{component.name}` dimension `{dim_name}` is "
                    f"{value_f:.1f}mm, larger than the largest print-volume axis "
                    f"({max(max_dims):.1f}mm)"
                )

    missing = list(dict.fromkeys(missing))
    if not missing:
        return PlanQualityReport(is_sufficient=True)

    feedback_lines = [
        "Plan violates hard print-volume constraints before code generation:",
        *[f"- {item}" for item in missing],
        "Revise the plan before code generation. Do not rely on a final "
        "`result.val().scale(...)` patch to make an oversized single-part "
        "design printable. Either reduce the named dimensions while preserving "
        "the functional interfaces, or explicitly split the design into named "
        "assembly parts that each fit inside the print volume.",
    ]
    return PlanQualityReport(
        is_sufficient=False,
        missing_features=tuple(missing),
        feedback="\n".join(feedback_lines),
    )
