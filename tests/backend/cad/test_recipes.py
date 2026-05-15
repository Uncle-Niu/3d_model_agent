from backend.cad.recipes import (
    build_adaptive_recipe_context,
    build_combined_recipe_context,
    build_recipe_prompt_context,
    infer_requirement_families,
    retrieve_recipe_cards,
    validate_plan_against_recipes,
)
from backend.domain.models import DesignComponent, DesignPlan


def test_retrieves_bracket_recipe():
    cards = retrieve_recipe_cards("Generate a wall mount bracket with screw holes")

    assert cards
    assert cards[0].recipe_id == "bracket_or_mount"


def test_bracket_plan_rejects_missing_fastener_cuts():
    cards = retrieve_recipe_cards("wall mount bracket")
    plan = DesignPlan(
        summary="A simple bracket with a base and support",
        components=[
            DesignComponent(
                name="main_plate",
                description="primary load-bearing plate",
                primitive="box",
                dimensions={"length": 90, "width": 70, "height": 5},
                operation="base",
            ),
            DesignComponent(
                name="support",
                description="vertical support slab",
                primitive="box",
                dimensions={"length": 80, "width": 6, "height": 90},
                operation="union",
            ),
        ],
        key_features=["main plate", "support"],
    )

    report = validate_plan_against_recipes(plan, cards)

    assert not report.is_sufficient
    assert any("through holes" in item for item in report.missing_negative_space)
    assert "Missing required negative-space/cut features" in report.feedback


def test_bracket_plan_accepts_reinforced_mount_with_holes():
    cards = retrieve_recipe_cards("wall mount bracket")
    plan = DesignPlan(
        summary="A reinforced wall mount bracket with screw interfaces",
        components=[
            DesignComponent(
                name="primary_mounting_plate",
                description="primary load-bearing plate or body",
                primitive="box",
                dimensions={"width": 100, "depth": 95, "height": 5},
                operation="base",
            ),
            DesignComponent(
                name="upright_support",
                description="support wall joined to the mounting plate",
                primitive="extrude",
                dimensions={"width": 90, "height": 105, "thickness": 6},
                operation="union",
            ),
            DesignComponent(
                name="gusset_ribs",
                description="ribs and gussets that thicken the junction for stiffness",
                primitive="extrude",
                dimensions={"width": 8, "height": 35, "depth": 35},
                operation="union",
            ),
            DesignComponent(
                name="mounting_holes",
                description="through holes and counterbores for fastener interfaces",
                primitive="cylinder",
                dimensions={"diameter": 5, "counterbore_diameter": 9},
                operation="cut",
            ),
            DesignComponent(
                name="stress_relief_fillets",
                description="fillets and chamfers at stress concentrations",
                primitive="fillet",
                dimensions={"radius": 1.5},
                operation="fillet",
            ),
        ],
        key_features=[
            "primary load-bearing plate",
            "mounting holes",
            "fastener counterbores",
            "ribs/gussets",
            "fillets/chamfers",
        ],
    )

    report = validate_plan_against_recipes(plan, cards)

    assert report.is_sufficient


def test_recipe_prompt_context_mentions_negative_space():
    context = build_recipe_prompt_context("wall mount bracket")

    assert "Required negative-space/cut features" in context
    assert "through holes" in context


def test_adaptive_recipe_infers_fasteners_and_reinforcement():
    families = infer_requirement_families("wall mount bracket with M5 screws")
    context = build_adaptive_recipe_context("wall mount bracket with M5 screws")

    assert "fastener interfaces" in families
    assert "load-bearing reinforcement" in families
    assert "through holes, counterbores, countersinks" in context
    assert "ribs, gussets" in context


def test_adaptive_recipe_infers_cavity_and_port_cutouts():
    context = build_adaptive_recipe_context("electronics case with USB cable port")

    assert "internal cavities or shells" in context
    assert "clearance and access cutouts" in context
    assert "ports, cable paths" in context
    assert "wall thickness" in context


def test_combined_recipe_context_exists_without_static_match():
    context = build_combined_recipe_context("custom hinge joint with sliding rail", cards=[])

    assert "Adaptive CAD Recipe Synthesis" in context
    assert "moving or mating interfaces" in context
    assert "mating clearances" in context


def test_phone_prompt_uses_adaptive_recipe_not_static_phone_card():
    cards = retrieve_recipe_cards("Generate a model for iPhone 16 Pro Max holder")
    context = build_combined_recipe_context("Generate a model for iPhone 16 Pro Max holder", cards)

    assert all(card.recipe_id != "phone_holder_desktop" for card in cards)
    assert "Adaptive CAD Recipe Synthesis" in context
    assert "retention geometry" in context
    assert "load-bearing reinforcement" in context
