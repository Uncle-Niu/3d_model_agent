import pytest
from unittest.mock import MagicMock, patch
from backend.tools.web_research import search_web, get_research_prompt_extension
from backend.domain.models import WebSearchProvider

@pytest.fixture
def anyio_backend():
    return "asyncio"

@pytest.mark.anyio
async def test_search_web_duckduckgo():
    mock_results = [
        {"title": "Test Result 1", "href": "http://test1.com", "body": "Snippet 1"},
        {"title": "Test Result 2", "href": "http://test2.com", "body": "Snippet 2"},
    ]
    
    with patch("backend.tools.web_research.DDGS") as mock_ddgs:
        instance = mock_ddgs.return_value.__enter__.return_value
        instance.text.return_value = mock_results
        
        results = await search_web("cadquery bracket")
        
        assert len(results) == 2
        assert results[0].title == "Test Result 1"
        assert results[0].url == "http://test1.com"
        assert results[0].snippet == "Snippet 1"
        assert results[0].source == "duckduckgo"

def test_get_research_prompt_extension():
    from backend.domain.models import SearchResult
    results = [
        SearchResult(title="Title 1", url="http://1.com", snippet="Snippet 1"),
    ]
    
    prompt = get_research_prompt_extension(results)
    
    assert "### Web Research Context" in prompt
    assert "Title 1" in prompt
    assert "http://1.com" in prompt
    assert "Snippet 1" in prompt

@pytest.mark.anyio
async def test_search_web_failure():
    with patch("backend.tools.web_research.DDGS") as mock_ddgs:
        instance = mock_ddgs.return_value.__enter__.return_value
        instance.text.side_effect = Exception("Search failed")
        
        results = await search_web("query")
        
        assert results == []
