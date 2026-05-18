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
                description="mounting holes, through holes, slots, and counterbores for fastener interfaces",
                primitive="cylinder",
                dimensions={"diameter": 5, "counterbore_diameter": 9},
                operation="cut",
            ),
            DesignComponent(
                name="stress_relief_fillets",
                description="fillets and chamfers at stress concentrations and thickened junctions",
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


def test_benchmark_holder_prompt_uses_adaptive_recipe_not_exact_static_card():
    cards = retrieve_recipe_cards("Generate a model for iPhone 16 Pro Max holder")
    context = build_combined_recipe_context("Generate a model for iPhone 16 Pro Max holder", cards)

    assert all("phone" not in card.recipe_id and "phone" not in card.title.lower() for card in cards)
    assert "Adaptive CAD Recipe Synthesis" in context
    assert "retention geometry" in context
    assert "load-bearing reinforcement" in context


# --------------------------------------------------------------------------
# Prompt-gated recipe enforcement
#
# The recipe gate used to mechanically apply every required feature of the
# top-scored recipe, which meant a broad word like "holder" forced the tray
# cavity requirement on stand-shaped designs. The fix gates each recipe's
# requirements on the recipe's own ``prompt_required_keywords`` so display
# stands aren't told to dig a pocket and free-standing parts aren't told to
# add fastener holes. Tests below cover both axes.
# --------------------------------------------------------------------------


def _stand_plan_without_cavity():
    return DesignPlan(
        summary="Fixed-angle landscape desk stand for a phone",
        components=[
            DesignComponent(
                name="base_plate",
                description="wide flat foundation that rests on the desk",
                primitive="box",
                dimensions={"length": 180, "width": 110, "height": 6},
                operation="base",
            ),
            DesignComponent(
                name="backrest",
                description="back support tilted backward for viewing angle",
                primitive="box",
                dimensions={"length": 175, "width": 6, "height": 110},
                operation="union",
                rotation=__import__("backend.domain.models", fromlist=["Rotation"]).Rotation(
                    axis="X", angle_deg=-25
                ),
            ),
            DesignComponent(
                name="front_lip",
                description="lip catching the bottom edge of the phone",
                primitive="box",
                dimensions={"length": 175, "width": 6, "height": 14},
                operation="union",
            ),
        ],
        key_features=["wide base", "tilted backrest", "front lip"],
    )


def test_holder_prompt_does_not_force_tray_cavity_on_stand_plan():
    # Real-world failure mode: "phone holder so I can watch movies" matched
    # tray_or_organizer (because "holder" was a tray tag) and the gate then
    # demanded "open cavity or compartments cut/shelled from the body",
    # rejecting an otherwise-good angled-stand plan and forcing the planner
    # into a horizontal pocket on the second iteration. The fix removes
    # "holder" from the tray tag set and gates cavity enforcement on true
    # container words in the prompt — so a stand stays a stand.
    user_message = "Design a iphone 14 pro max holder so I can watch movie on a desk"
    cards = retrieve_recipe_cards(user_message)
    plan = _stand_plan_without_cavity()

    report = validate_plan_against_recipes(plan, cards, user_message=user_message)

    assert report.is_sufficient, (
        "Stand-style 'holder' plan should not be forced to add a cavity. "
        f"Got missing_features={report.missing_features}, "
        f"missing_negative_space={report.missing_negative_space}"
    )


def test_explicit_tray_prompt_still_enforces_cavity_requirement():
    # The narrowed gate must not let true tray prompts off the hook.
    user_message = "design a tray with compartments for screws"
    cards = retrieve_recipe_cards(user_message)
    assert cards and cards[0].recipe_id == "tray_or_organizer"

    plain_solid_plan = DesignPlan(
        summary="A solid block with no cavity",
        components=[
            DesignComponent(
                name="solid_block",
                description="a plain box",
                primitive="box",
                dimensions={"length": 100, "width": 60, "height": 20},
                operation="base",
            ),
        ],
        key_features=["block"],
    )

    report = validate_plan_against_recipes(plain_solid_plan, cards, user_message=user_message)

    assert not report.is_sufficient
    assert any("cavity" in item or "compart" in item for item in report.missing_negative_space)


def test_freestanding_holder_does_not_demand_fastener_holes():
    # Bracket recipe used to share the "holder" tag and would gate fastener
    # cut requirements on any holder prompt. Free-standing desk parts have
    # nothing to attach to — forcing screw holes on them is the worse
    # failure mode than omitting them.
    user_message = "headphone holder that sits on my desk"
    cards = retrieve_recipe_cards(user_message)
    plan = DesignPlan(
        summary="Headphone hanger that rests on the desk",
        components=[
            DesignComponent(
                name="base",
                description="weighted base resting on the desk",
                primitive="box",
                dimensions={"length": 120, "width": 80, "height": 8},
                operation="base",
            ),
            DesignComponent(
                name="hook_post",
                description="upright post with curved hook for the headphones",
                primitive="extrude",
                dimensions={"length": 8, "width": 8, "height": 220},
                operation="union",
            ),
        ],
        key_features=["base", "hook"],
    )

    report = validate_plan_against_recipes(plan, cards, user_message=user_message)

    # The plan may still have other gaps, but it must NOT be rejected for
    # missing fastener cuts — there's nothing to fasten to.
    fastener_complaints = [
        item for item in (report.missing_negative_space + report.missing_features)
        if "fastener" in item.lower() or "screw" in item.lower() or "counterbore" in item.lower()
    ]
    assert not fastener_complaints, (
        f"Free-standing holder should not be told to add fastener holes. "
        f"Got: {fastener_complaints}"
    )


def test_wall_mount_prompt_still_enforces_fastener_cuts():
    # The narrowed gate must not silence true wall-mount prompts.
    user_message = "wall-mount bracket with M5 screw holes"
    cards = retrieve_recipe_cards(user_message)
    plan = DesignPlan(
        summary="A wall mount without any fastener cuts",
        components=[
            DesignComponent(
                name="main_plate",
                description="primary load-bearing plate",
                primitive="box",
                dimensions={"length": 80, "width": 60, "height": 5},
                operation="base",
            ),
            DesignComponent(
                name="upright",
                description="vertical support slab",
                primitive="box",
                dimensions={"length": 60, "width": 6, "height": 80},
                operation="union",
            ),
            DesignComponent(
                name="rib",
                description="ribs/gussets stiffening the junction",
                primitive="extrude",
                dimensions={"length": 30, "width": 6, "height": 30},
                operation="union",
            ),
        ],
        key_features=["plate", "upright", "rib"],
    )

    report = validate_plan_against_recipes(plan, cards, user_message=user_message)

    assert not report.is_sufficient
    assert any("through holes" in item or "fastener" in item.lower()
               for item in report.missing_negative_space)


# --------------------------------------------------------------------------
# Prose-vs-rotation parity check
# --------------------------------------------------------------------------

def test_prose_tilt_without_rotation_tag_is_flagged():
    # When the planner describes a component as "angled / tilted / leaning"
    # but never emits a structured <rotation> tag, the code generator
    # produces a vertical or horizontal component instead — exactly the
    # failure that left the phone-holder backrest standing straight up.
    # The plan-quality gate now catches this so the planner is asked to
    # add the rotation tag before code generation runs.
    user_message = "lectern that sits on a desk"  # not a tray, not a bracket
    cards = retrieve_recipe_cards(user_message)
    plan = DesignPlan(
        summary="Lectern with a leaning reading surface",
        components=[
            DesignComponent(
                name="base",
                description="flat base resting on the desk",
                primitive="box",
                operation="base",
                dimensions={"length": 200, "width": 150, "height": 8},
            ),
            DesignComponent(
                name="reading_surface",
                description="a tilted reading surface angled back for comfort",
                primitive="box",
                operation="union",
                dimensions={"length": 200, "width": 6, "height": 220},
                # NOTE: no rotation tag — the prose says "tilted" but the
                # structured field is missing.
            ),
        ],
        key_features=["base", "tilted reading surface"],
    )

    report = validate_plan_against_recipes(plan, cards, user_message=user_message)

    assert not report.is_sufficient
    joined = " ".join(report.missing_features).lower()
    assert "rotation" in joined and "reading_surface" in joined


def test_prose_tilt_with_rotation_tag_is_accepted():
    from backend.domain.models import Rotation
    user_message = "lectern that sits on a desk"
    cards = retrieve_recipe_cards(user_message)
    plan = DesignPlan(
        summary="Lectern with a properly tagged tilted reading surface",
        components=[
            DesignComponent(
                name="base",
                description="flat base resting on the desk",
                primitive="box",
                operation="base",
                dimensions={"length": 200, "width": 150, "height": 8},
            ),
            DesignComponent(
                name="reading_surface",
                description="a tilted reading surface angled back for comfort",
                primitive="box",
                operation="union",
                dimensions={"length": 200, "width": 6, "height": 220},
                rotation=Rotation(axis="X", angle_deg=-15, intent="tilt backward"),
            ),
        ],
        key_features=["base", "tilted reading surface"],
    )

    report = validate_plan_against_recipes(plan, cards, user_message=user_message)
    # The plan may still trip other gates (this is a non-tray, non-bracket
    # prompt so no recipe-required feature applies). All we care about: the
    # prose-vs-rotation check is satisfied.
    rotation_complaints = [
        item for item in report.missing_features
        if "rotation" in item.lower() and "reading_surface" in item
    ]
    assert not rotation_complaints
