from __future__ import annotations

from server import ResearchResponse, ResearchResult
from deep_research_client import render_context


def test_render_context_formats_llm_ready_output() -> None:
    response = ResearchResponse(
        query="latest iran-us war",
        results=[
            ResearchResult(
                rank_score=1.0,
                url="https://example.com/article",
                title="Example Article",
                clean_content="Paragraph one.\n\nParagraph two.",
                metadata={"engine": "brave", "canonical_url": "https://example.com/article"},
            )
        ],
    )

    output = render_context(response, include_metadata=True)

    assert "Deep research context for: latest iran-us war" in output
    assert "[Source 1] Example Article" in output
    assert "URL: https://example.com/article" in output
    assert "Paragraph one." in output
    assert "- engine: brave" in output


def test_render_context_handles_empty_results() -> None:
    response = ResearchResponse(query="no results", results=[])

    output = render_context(response)

    assert "No relevant web results were found." in output
