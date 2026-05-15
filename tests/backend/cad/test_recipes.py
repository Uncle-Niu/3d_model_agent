from backend.cad.recipes import (
    build_recipe_prompt_context,
    retrieve_recipe_cards,
    validate_plan_against_recipes,
)
from backend.domain.models import DesignComponent, DesignPlan


def test_retrieves_phone_holder_recipe():
    cards = retrieve_recipe_cards("Generate a model for iPhone 16 Pro Max holder")

    assert cards
    assert cards[0].recipe_id == "phone_holder_desktop"


def test_phone_holder_plan_rejects_plain_base_and_slab():
    cards = retrieve_recipe_cards("iPhone holder")
    plan = DesignPlan(
        summary="A simple phone holder with a base and support",
        components=[
            DesignComponent(
                name="base",
                description="flat base",
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
        key_features=["base", "support"],
    )

    report = validate_plan_against_recipes(plan, cards)

    assert not report.is_sufficient
    assert any("charging-cable notch" in item for item in report.missing_negative_space)
    assert "plain base plus slab" in report.feedback


def test_phone_holder_plan_accepts_archetype_features_and_cut():
    cards = retrieve_recipe_cards("iPhone holder")
    plan = DesignPlan(
        summary="A case-friendly phone holder with angled support and cable access",
        components=[
            DesignComponent(
                name="stable_base_plate",
                description="wide stable base plate for phone footprint",
                primitive="box",
                dimensions={"width": 100, "depth": 95, "height": 5},
                operation="base",
            ),
            DesignComponent(
                name="angled_backrest",
                description="angled backrest tilted for a comfortable lean",
                primitive="extrude",
                dimensions={"width": 90, "height": 105, "thickness": 6},
                operation="union",
            ),
            DesignComponent(
                name="front_retaining_lip",
                description="front lip and ledge that stop the phone from sliding",
                primitive="box",
                dimensions={"width": 80, "depth": 16, "height": 18},
                operation="union",
            ),
            DesignComponent(
                name="side_guides",
                description="two separated side guide cheeks with center access",
                primitive="box",
                dimensions={"width": 5, "depth": 24, "height": 24},
                operation="pattern",
            ),
            DesignComponent(
                name="center_charging_cable_notch",
                description="center charging cable notch cut through the front lip and base",
                primitive="box",
                dimensions={"width": 24, "depth": 22, "height": 22},
                operation="cut",
            ),
            DesignComponent(
                name="rounded_edges",
                description="fillet and chamfer printable edges",
                primitive="fillet",
                dimensions={"radius": 1.5},
                operation="fillet",
            ),
        ],
        key_features=[
            "stable base plate",
            "angled backrest",
            "front retaining lip",
            "case-friendly clearance slot",
            "two separated side guides",
            "center charging-cable notch",
            "rounded/chamfered printable edges",
        ],
    )

    report = validate_plan_against_recipes(plan, cards)

    assert report.is_sufficient


def test_recipe_prompt_context_mentions_negative_space():
    context = build_recipe_prompt_context("phone holder")

    assert "Required negative-space/cut features" in context
    assert "center charging-cable notch" in context

