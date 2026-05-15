from backend.vision.critic import _build_vision_user_prompt


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
