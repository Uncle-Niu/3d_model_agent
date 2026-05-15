"""
Web research tool.
Provides search capabilities using DuckDuckGo.
"""

from __future__ import annotations

import logging
from duckduckgo_search import DDGS
from ..domain.models import SearchResult, WebSearchProvider

logger = logging.getLogger(__name__)

async def search_web(
    query: str,
    provider: WebSearchProvider = WebSearchProvider.DUCKDUCKGO,
    max_results: int = 5
) -> list[SearchResult]:
    """
    Search the web for a given query.
    """
    results: list[SearchResult] = []
    
    try:
        if provider == WebSearchProvider.DUCKDUCKGO:
            with DDGS() as ddgs:
                ddgs_results = ddgs.text(query, max_results=max_results)
                for r in ddgs_results:
                    results.append(SearchResult(
                        title=r.get("title", ""),
                        url=r.get("href", ""),
                        snippet=r.get("body", ""),
                        source="duckduckgo"
                    ))
        else:
            logger.warning(f"Search provider {provider} not implemented, falling back to DuckDuckGo")
            return await search_web(query, WebSearchProvider.DUCKDUCKGO, max_results)
            
    except Exception as e:
        logger.error(f"Web search failed: {e}")
        
    return results

def get_research_prompt_extension(results: list[SearchResult]) -> str:
    """
    Format search results into a prompt extension for the LLM.
    """
    if not results:
        return ""
    
    prompt = "\n\n### Web Research Context\n"
    for i, r in enumerate(results, 1):
        prompt += f"{i}. [{r.title}]({r.url})\n"
        prompt += f"   Snippet: {r.snippet}\n\n"
    
    return prompt
