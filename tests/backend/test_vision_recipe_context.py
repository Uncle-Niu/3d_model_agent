from backend.vision.critic import VISION_SYSTEM_PROMPT, _build_vision_user_prompt
from backend.domain.models import DesignComponent, DesignPlan, PhysicalUse, Rotation


def test_vision_prompt_uses_recipe_context_as_independent_rubric():
    prompt = _build_vision_user_prompt(
        user_intent="Generate a wall mount bracket",
        recipe_context=(
            "## Retrieved CAD Recipe Context\n"
            "- primary load-bearing plate\n"
            "- through holes or counterbores for fasteners\n"
            "- ribs/gussets at the load-bearing junction\n"
        ),
    )

    assert "Product Archetype" in prompt
    assert "independent rubric" in prompt
    assert "counterbores" in prompt
    assert "plain base plus slab" in prompt


def test_vision_prompt_surfaces_locked_rotations_for_tilt_verification():
    # When the plan locks a rotation on a component, the vision critic must
    # be told to verify the component is actually tilted in the render — not
    # appearing vertical or horizontal. This was the missing signal that
    # let a vertical backrest pass the "all features present" check even
    # though the plan said it should lean -45° back.
    plan = DesignPlan(
        summary="Phone stand",
        components=[
            DesignComponent(
                name="base", description="base plate", primitive="box", operation="base",
            ),
            DesignComponent(
                name="backrest",
                description="tilted back support",
                primitive="box",
                operation="union",
                rotation=Rotation(axis="X", angle_deg=-45, intent="lean back for viewing"),
            ),
        ],
    )
    prompt = _build_vision_user_prompt(
        user_intent="phone stand to watch movies",
        plan=plan,
    )

    assert "Locked rotations" in prompt
    assert "backrest" in prompt
    assert "-45" in prompt
    # The instruction tying tilt verification to a specific issue type
    # must be present so the critic knows how to report mismatches.
    assert "wrong_shape" in prompt


def test_vision_prompt_surfaces_physical_use_for_critic():
    # Containment strategy and pose intent must be surfaced so the critic
    # can verify gravity / anti-slip / tilt behavior, not just feature
    # presence. (The system prompt's new "would this part actually work"
    # rules need this data in the user message to act on.)
    plan = DesignPlan(
        summary="phone stand",
        components=[DesignComponent(name="base", description="base", primitive="box")],
        physical_use=PhysicalUse(
            orientation="rests on a desk",
            containment_strategy="front lip catches the phone bottom edge",
            pose_intent="backrest leans back 25 degrees",
        ),
    )
    prompt = _build_vision_user_prompt(
        user_intent="phone stand",
        plan=plan,
    )

    assert "Real-world use" in prompt
    assert "Containment" in prompt
    assert "front lip" in prompt
    assert "Pose intent" in prompt


def test_vision_system_prompt_includes_tilt_and_contact_patch_rules():
    # The system prompt is the place the critic learns *how* to assess
    # tilt-vs-plan and flat-base contact. Both must be present so the
    # critic raises the right issue_type rather than burying these in a
    # vague "proportions" advisory.
    assert "Tilt verification" in VISION_SYSTEM_PROMPT
    assert "Containment under gravity" in VISION_SYSTEM_PROMPT
    assert "weak_contact_patch" in VISION_SYSTEM_PROMPT
