from backend.vision.critic import _build_vision_user_prompt


def test_vision_prompt_uses_recipe_context_as_independent_rubric():
    prompt = _build_vision_user_prompt(
        user_intent="Generate an iPhone holder",
        recipe_context=(
            "## Retrieved CAD Recipe Context\n"
            "- angled backrest\n"
            "- center charging-cable notch cut through the front lip/base\n"
        ),
    )

    assert "Product Archetype" in prompt
    assert "independent rubric" in prompt
    assert "charging-cable notch" in prompt
    assert "plain base plus slab" in prompt

