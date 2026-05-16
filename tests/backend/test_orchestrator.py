import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from backend.agent.orchestrator import AgentOrchestrator
from backend.domain.models import (
    ProjectConfig, ChatMessage, ModelMetadata, CritiqueReport, GeometryIssue,
    DesignPlan, FailureType, SearchResult
)

class TestAgentOrchestrator(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.mock_storage = MagicMock()
        self.mock_storage.get_project.return_value = ProjectConfig(project_id="p1", name="Test Project")
        self.mock_storage.get_chat_thread_messages.return_value = []
        self.mock_storage.next_model_id.return_value = "model-001"
        self.mock_storage.latest_successful_model.return_value = None

        self.mock_llm = AsyncMock()
        self.mock_llm.base_url = "http://localhost:11434/v1"
        self.mock_llm.model = "qwen3.6:35b"
        self.mock_llm.decide_research.return_value = None
        self.mock_llm.plan_design.return_value = DesignPlan(summary="Test plan", key_features=["a feature"])

        async def mock_generate_stream(*args, **kwargs):
            chunks = ["```python\n", "result = cq.Workplane().box(10,10,10)\n", "```"]
            for chunk in chunks:
                yield chunk
        self.mock_llm.generate_stream.side_effect = mock_generate_stream
        self.mock_llm.repair_cadquery.return_value = "```python\nresult = cq.Workplane().box(5,5,5)\n```"

    @patch("backend.agent.orchestrator.AgentOrchestrator.check_vision_connectivity", new_callable=AsyncMock)
    @patch("backend.agent.orchestrator.process_cadquery_code")
    @patch("backend.agent.orchestrator.AgentOrchestrator._run_render", new_callable=AsyncMock)
    @patch("backend.agent.orchestrator.AgentOrchestrator._run_vision_critique", new_callable=AsyncMock)
    async def test_run_pipeline_success(self, mock_critique, mock_render, mock_exec, mock_vision):
        orchestrator = AgentOrchestrator(storage=self.mock_storage, llm=self.mock_llm)
        mock_vision.return_value = (True, "ok")
        mock_exec.return_value = {
            "success": True, "message": "Success", "files": ["glb"],
            "geometry_stats": {"face_count": 6, "bounding_box": "10x10x10"}, "_shape": MagicMock()
        }
        mock_render.return_value = {"iso": "path/to/iso.png"}
        mock_critique.return_value = CritiqueReport(overall_printability=0.9, issues=[])

        model_id = await orchestrator.run_pipeline("p1", "t1", "make a box")

        self.assertEqual(model_id, "model-001")
        self.assertEqual(mock_exec.call_count, 1)

    @patch("backend.agent.orchestrator.AgentOrchestrator.check_vision_connectivity", new_callable=AsyncMock)
    @patch("backend.agent.orchestrator.process_cadquery_code")
    @patch("backend.agent.orchestrator.search_web", new_callable=AsyncMock)
    @patch("backend.agent.orchestrator.AgentOrchestrator._run_render", new_callable=AsyncMock)
    @patch("backend.agent.orchestrator.AgentOrchestrator._run_vision_critique", new_callable=AsyncMock)
    async def test_run_pipeline_with_research(self, mock_critique, mock_render, mock_search, mock_exec, mock_vision):
        orchestrator = AgentOrchestrator(storage=self.mock_storage, llm=self.mock_llm)
        mock_vision.return_value = (True, "ok")
        self.mock_llm.decide_research.return_value = "standard M6 bolt dimensions"
        
        mock_exec.return_value = {"success": True, "message": "Ok", "files": ["glb"], "geometry_stats": {}}
        mock_search.return_value = [SearchResult(title="M6 Bolt", url="http://example.com", snippet="M6 is 6mm", source="duckduckgo")]
        mock_critique.return_value = CritiqueReport(overall_printability=0.9, issues=[])
        
        await orchestrator.run_pipeline("p1", "t1", "make a bolt")
        
        self.assertTrue(mock_search.called)
        args, _ = self.mock_storage.save_model_metadata.call_args
        metadata = args[1]
        self.assertEqual(len(metadata.citations), 1)

    @patch("backend.agent.orchestrator.AgentOrchestrator.check_vision_connectivity", new_callable=AsyncMock)
    @patch("backend.agent.orchestrator.process_cadquery_code")
    async def test_run_pipeline_repair_loop(self, mock_exec, mock_vision):
        orchestrator = AgentOrchestrator(storage=self.mock_storage, llm=self.mock_llm)
        mock_vision.return_value = (True, "ok")  # vision is required; repair loop test focuses on syntax/exec repair
        
        # First call fails, second succeeds
        mock_exec.side_effect = [
            {"success": False, "message": "SyntaxError", "failure_type": "syntax_error", "files": []},
            {"success": True, "message": "Fixed", "files": ["glb"], "geometry_stats": {}}
        ]
        
        await orchestrator.run_pipeline("p1", "t1", "make a box")
        self.assertTrue(self.mock_llm.repair_cadquery.called)
        self.assertEqual(mock_exec.call_count, 2)

    @patch("backend.agent.orchestrator.AgentOrchestrator.check_vision_connectivity", new_callable=AsyncMock)
    @patch("backend.agent.orchestrator.process_cadquery_code")
    @patch("backend.agent.orchestrator.AgentOrchestrator._run_render", new_callable=AsyncMock)
    @patch("backend.agent.orchestrator.AgentOrchestrator._run_vision_critique", new_callable=AsyncMock)
    async def test_run_pipeline_vision_repair(self, mock_critique, mock_render, mock_exec, mock_vision):
        orchestrator = AgentOrchestrator(storage=self.mock_storage, llm=self.mock_llm)
        mock_vision.return_value = (True, "ok")
        mock_exec.return_value = {"success": True, "message": "Ok", "files": ["glb"], "_shape": MagicMock(), "geometry_stats": {}}
        
        # Use a counter to return different values
        call_counts = {"critique": 0}
        async def side_effect_critique(*args, **kwargs):
            call_counts["critique"] += 1
            if call_counts["critique"] == 1:
                return CritiqueReport(overall_printability=0.3, issues=[GeometryIssue(issue_type="thin_wall", severity="error", description="Too thin")])
            return CritiqueReport(overall_printability=0.9, issues=[])
        
        mock_critique.side_effect = side_effect_critique
        
        await orchestrator.run_pipeline("p1", "t1", "make a thick box")
        self.assertTrue(self.mock_llm.repair_cadquery.called)
        # Check that it ran at least twice
        self.assertGreaterEqual(mock_exec.call_count, 2)
        self.assertEqual(call_counts["critique"], mock_exec.call_count)

    @patch("backend.agent.orchestrator.AgentOrchestrator.check_ollama_connectivity", new_callable=AsyncMock)
    async def test_connectivity_check_failure(self, mock_conn):
        orchestrator = AgentOrchestrator(storage=self.mock_storage, llm=self.mock_llm)
        mock_conn.return_value = False
        result = await orchestrator.run_pipeline("p1", "t1", "hi")
        self.assertIsNone(result)

if __name__ == "__main__":
    unittest.main()
