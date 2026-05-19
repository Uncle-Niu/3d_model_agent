import asyncio

from backend.knowledge.local_recall import LocalKnowledgeService


def test_vesa_standard_uses_builtin_reference_without_model_calls():
    async def run():
        events = []
        svc = LocalKnowledgeService(model_chain=("should-not-run",), per_call_timeout_s=0.01)

        async def on_step(event, payload):
            events.append((event, payload))

        consensus = await svc.extract_knowledge(
            subject="VESA mounting standard",
            fields=[
                "hole_spacing_x_mm",
                "hole_spacing_y_mm",
                "hole_diameter_mm",
                "bolt_thread_mm",
                "mounting_depth_mm",
            ],
            on_step=on_step,
        )

        assert consensus.fields["hole_spacing_x_mm"].value == [75, 100]
        assert consensus.fields["hole_spacing_y_mm"].value == [75, 100]
        assert consensus.fields["hole_diameter_mm"].value == 5.0
        assert consensus.fields["bolt_thread_mm"].value == "M4"
        assert "mounting_depth_mm" in consensus.uncertain_fields
        assert consensus.contributing_models == ["built_in_reference"]
        assert events[0][1]["model"] == "built_in_reference"

    asyncio.run(run())


def test_vesa_keyword_is_added_when_detector_misses_it():
    async def run():
        svc = LocalKnowledgeService(per_call_timeout_s=0.001)
        subjects = await svc.detect_subjects(
            "30 degree laptop tray with vesa mount plate on the back",
            main_model="missing-model",
        )
        assert any(subject.subject == "VESA mounting standard" for subject in subjects)

    asyncio.run(run())
