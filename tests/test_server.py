from __future__ import annotations

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from server import DeepResearchService, ScrapedDocument, tokenize


def test_tokenize_normalizes_text() -> None:
    assert tokenize("Solid-state batteries, 2026 edition!") == [
        "solid",
        "state",
        "batteries",
        "2026",
        "edition",
    ]


def test_rank_documents_sorts_highest_relevance_first() -> None:
    service = DeepResearchService(score_threshold=0.0)
    documents = [
        ScrapedDocument(
            url="https://example.com/1",
            title="Battery breakthroughs",
            clean_content="Solid-state batteries improved energy density in recent prototypes.",
            metadata={},
            tokens=tokenize("Battery breakthroughs Solid-state batteries improved energy density in recent prototypes."),
        ),
        ScrapedDocument(
            url="https://example.com/2",
            title="Unrelated gardening guide",
            clean_content="Tomatoes and soil care tips for spring.",
            metadata={},
            tokens=tokenize("Unrelated gardening guide Tomatoes and soil care tips for spring."),
        ),
    ]

    ranked = service.rank_documents("recent breakthroughs in solid-state batteries", documents)

    assert ranked[0].url == "https://example.com/1"
    assert ranked[0].rank_score >= ranked[1].rank_score


def test_postprocess_preserves_paragraphs_and_filters_live_page_noise() -> None:
    text = """
    Latest Updates
    10 hrs ago
    Trump says US will obliterate Iran's power plants if the strait stays closed.

    This live page is now closed.

    Iranian officials said energy sites linked to the US could be targeted in response.
    """

    cleaned = DeepResearchService._postprocess_extracted_text(  # noqa: SLF001
        text,
        url="https://example.com/news/live/abc",
        title="Live updates",
    )

    assert "Latest Updates" not in cleaned
    assert "10 hrs ago" not in cleaned
    assert "This live page is now closed." not in cleaned
    assert "Trump says US will obliterate" in cleaned
    assert "\n\n" in cleaned


@pytest.mark.asyncio
async def test_scrape_single_document_uses_html_extraction() -> None:
    service = DeepResearchService(score_threshold=0.0)

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def request(self, method: str, url: str, params=None):  # noqa: ANN001
            if url.endswith("/robots.txt"):
                return FakeResponse("User-agent: *\nAllow: /\n")
            return FakeResponse(
                """
                <html>
                  <head>
                    <title>Example Article</title>
                    <meta name="description" content="Useful article">
                    <link rel="canonical" href="https://example.com/article">
                  </head>
                  <body>
                    <main>
                      <p>Solid-state batteries are moving from lab demos to pilot manufacturing.</p>
                    </main>
                    <a href="/related">Related</a>
                  </body>
                </html>
                """
            )

    from server import SearxSearchResult

    document = await service._scrape_single_document(  # noqa: SLF001
        client=FakeClient(),  # type: ignore[arg-type]
        result=SearxSearchResult(url="https://example.com/article", title="Fallback title", snippet="snippet"),
        ctx=None,
    )

    assert document is not None
    assert document.title == "Example Article"
    assert "Solid-state batteries" in document.clean_content
    assert document.metadata["canonical_url"] == "https://example.com/article"
    assert "https://example.com/related" in document.metadata["backlinks"]


@pytest.mark.asyncio
async def test_mcp_tool_is_discoverable_over_stdio() -> None:
    server_params = StdioServerParameters(
        command="./.venv/bin/python",
        args=["server.py"],
    )

    async with stdio_client(server_params) as streams:
        read_stream, write_stream = streams
        session = ClientSession(read_stream, write_stream)
        async with session:
            await session.initialize()
            tools = await session.list_tools()

    tool_names = {tool.name for tool in tools.tools}
    assert "deep_web_research" in tool_names


@pytest.mark.asyncio
async def test_deep_research_end_to_end_with_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    service = DeepResearchService(score_threshold=0.0)

    async def fake_search(*args, **kwargs):  # noqa: ANN002, ANN003
        from server import SearxSearchResult

        return [
            SearxSearchResult(
                url="https://example.com/article",
                title="Example Article",
                snippet="solid-state battery progress",
                metadata={"engine": "fake"},
            )
        ]

    async def fake_scrape(*args, **kwargs):  # noqa: ANN002, ANN003
        return [
            ScrapedDocument(
                url="https://example.com/article",
                title="Example Article",
                clean_content="Solid-state battery progress is accelerating in pilot lines.",
                metadata={"engine": "fake"},
                tokens=tokenize("Example Article Solid-state battery progress is accelerating in pilot lines."),
            )
        ]

    monkeypatch.setattr(service, "search_searxng", fake_search)
    monkeypatch.setattr(service, "scrape_documents", fake_scrape)

    response = await service.deep_research("solid-state battery progress")

    assert response.query == "solid-state battery progress"
    assert len(response.results) == 1
    assert response.results[0].url == "https://example.com/article"


@pytest.mark.asyncio
async def test_ensure_searxng_ready_starts_and_schedules_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    service = DeepResearchService(
        searxng_idle_timeout_seconds=600,
        searxng_startup_timeout_seconds=1,
    )
    calls: list[str] = []
    readiness = iter([False, True])

    async def fake_ready() -> bool:
        calls.append("ready")
        return next(readiness)

    def fake_start() -> bool:
        calls.append("start")
        return True

    async def fake_wait() -> None:
        calls.append("wait")

    class DummyTask:
        def __init__(self) -> None:
            self._done = False

        def cancel(self) -> None:
            calls.append("cancel")
            self._done = True

        def done(self) -> bool:
            return self._done

    monkeypatch.setattr(service, "_searxng_endpoint_ready", fake_ready)
    monkeypatch.setattr(service, "_start_searxng_runtime", fake_start)
    monkeypatch.setattr(service, "_wait_for_searxng_ready", fake_wait)
    monkeypatch.setattr(
        "server.asyncio.create_task",
        lambda coro: (coro.close(), DummyTask())[1],
    )

    await service.ensure_searxng_ready()

    assert calls[:3] == ["ready", "start", "wait"]
    assert service._owns_searxng_runtime is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_ensure_searxng_ready_reschedules_existing_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    service = DeepResearchService()
    service._owns_searxng_runtime = True  # noqa: SLF001
    calls: list[str] = []

    class OldTask:
        def __init__(self) -> None:
            self._done = False

        def cancel(self) -> None:
            calls.append("cancel-old")
            self._done = True

        def done(self) -> bool:
            return self._done

    class NewTask:
        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            calls.append("cancel-new")

    service._searxng_stop_task = OldTask()  # noqa: SLF001

    async def fake_ready() -> bool:
        calls.append("ready")
        return True

    monkeypatch.setattr(service, "_searxng_endpoint_ready", fake_ready)
    monkeypatch.setattr("server.asyncio.create_task", lambda coro: (coro.close(), NewTask())[1])

    await service.ensure_searxng_ready()

    assert calls == ["cancel-old", "ready"]
    assert isinstance(service._searxng_stop_task, NewTask)  # noqa: SLF001
