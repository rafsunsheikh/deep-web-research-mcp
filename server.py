from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import trafilatura
from bs4 import BeautifulSoup
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field
from rank_bm25 import BM25Okapi

DEFAULT_USER_AGENT = "DeepResearchMCP/0.1 (+https://localhost; contact=local-operator)"
DEFAULT_SEARXNG_URL = "http://localhost:8888"
DEFAULT_RATE_LIMIT_SECONDS = 0.75
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESULTS = 5
DEFAULT_MAX_CONTENT_CHARS = 4000
DEFAULT_SCORE_THRESHOLD = 0.05
BACKOFF_DELAYS = (0.5, 1.0, 2.0)
LIVE_PAGE_HINTS = ("live", "updates", "latest news", "breaking")
DEFAULT_SEARXNG_CONTAINER_NAME = "deep-research-searxng"
DEFAULT_SEARXNG_IDLE_TIMEOUT_SECONDS = 600
DEFAULT_SEARXNG_STARTUP_TIMEOUT_SECONDS = 30.0


class ResearchResult(BaseModel):
    rank_score: float
    url: str
    title: str
    clean_content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchResponse(BaseModel):
    query: str
    results: list[ResearchResult]


class SearxSearchResult(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class ScrapedDocument:
    url: str
    title: str
    clean_content: str
    metadata: dict[str, Any]
    tokens: list[str]


class DeepResearchService:
    def __init__(
        self,
        searxng_base_url: str = DEFAULT_SEARXNG_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        searxng_container_name: str = DEFAULT_SEARXNG_CONTAINER_NAME,
        searxng_idle_timeout_seconds: int = DEFAULT_SEARXNG_IDLE_TIMEOUT_SECONDS,
        searxng_startup_timeout_seconds: float = DEFAULT_SEARXNG_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self.searxng_base_url = searxng_base_url.rstrip("/")
        self.user_agent = user_agent
        self.rate_limit_seconds = rate_limit_seconds
        self.timeout_seconds = timeout_seconds
        self.max_content_chars = max_content_chars
        self.score_threshold = score_threshold
        self.searxng_container_name = searxng_container_name
        self.searxng_idle_timeout_seconds = searxng_idle_timeout_seconds
        self.searxng_startup_timeout_seconds = searxng_startup_timeout_seconds
        self._last_request_started = 0.0
        self._robots_cache: dict[str, RobotFileParser] = {}
        self._robots_lock = asyncio.Lock()
        self._searxng_lock = asyncio.Lock()
        self._searxng_stop_task: asyncio.Task[None] | None = None
        self._owns_searxng_runtime = False

    async def deep_research(
        self,
        query: str,
        ctx: Context | None = None,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> ResearchResponse:
        if not query.strip():
            raise ValueError("query must not be empty")

        await self.ensure_searxng_ready(ctx=ctx)
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
        ) as client:
            search_results = await self.search_searxng(client, query=query, max_results=max_results, ctx=ctx)
            scraped = await self.scrape_documents(client, search_results, ctx=ctx)

        ranked = self.rank_documents(query=query, documents=scraped)
        return ResearchResponse(query=query, results=ranked)

    async def ensure_searxng_ready(self, ctx: Context | None = None) -> None:
        async with self._searxng_lock:
            self._cancel_pending_shutdown()
            if await self._searxng_endpoint_ready():
                self._schedule_shutdown_if_owned()
                return

            if ctx:
                ctx.info("SearXNG is unavailable, attempting to start it")

            started = await asyncio.to_thread(self._start_searxng_runtime)
            if not started:
                raise RuntimeError(
                    f"SearXNG is not reachable at {self.searxng_base_url} and could not be auto-started."
                )

            await self._wait_for_searxng_ready()
            self._owns_searxng_runtime = True
            self._schedule_shutdown_if_owned()
            if ctx:
                ctx.info("SearXNG is ready")

    async def search_searxng(
        self,
        client: httpx.AsyncClient,
        query: str,
        max_results: int,
        ctx: Context | None = None,
    ) -> list[SearxSearchResult]:
        if ctx:
            ctx.info(f"Searching SearXNG for query: {query}")

        params = {
            "q": query,
            "format": "json",
            "language": "auto",
            "safesearch": 0,
        }
        response = await self._request_with_backoff(
            client,
            "GET",
            f"{self.searxng_base_url}/search",
            params=params,
        )
        payload = response.json()
        raw_results = payload.get("results", [])[:max_results]
        parsed: list[SearxSearchResult] = []
        for item in raw_results:
            url = item.get("url")
            if not url:
                continue
            metadata = {
                "engine": item.get("engine"),
                "category": item.get("category"),
                "published_date": item.get("publishedDate"),
                "score": item.get("score"),
                "thumbnail": item.get("thumbnail"),
                "favicon": item.get("favicon"),
            }
            parsed.append(
                SearxSearchResult(
                    url=url,
                    title=item.get("title") or "",
                    snippet=item.get("content") or "",
                    metadata={key: value for key, value in metadata.items() if value is not None},
                )
            )
        return parsed

    async def scrape_documents(
        self,
        client: httpx.AsyncClient,
        search_results: list[SearxSearchResult],
        ctx: Context | None = None,
    ) -> list[ScrapedDocument]:
        documents: list[ScrapedDocument] = []
        total = max(len(search_results), 1)
        for index, result in enumerate(search_results, start=1):
            if ctx:
                ctx.report_progress(index - 1, total, f"Scraping {result.url}")
            document = await self._scrape_single_document(client, result, ctx=ctx)
            if document is not None:
                documents.append(document)
            if ctx:
                ctx.report_progress(index, total, f"Processed {result.url}")
        return documents

    async def _scrape_single_document(
        self,
        client: httpx.AsyncClient,
        result: SearxSearchResult,
        ctx: Context | None = None,
    ) -> ScrapedDocument | None:
        allowed = await self._is_allowed_by_robots(client, result.url)
        if not allowed:
            if ctx:
                ctx.info(f"Skipping robots-disallowed URL: {result.url}")
            return None

        try:
            response = await self._request_with_backoff(client, "GET", result.url)
            html = response.text
        except Exception as exc:
            if ctx:
                ctx.log("warning", f"HTTP fetch failed for {result.url}; falling back to trafilatura.fetch_url: {exc}")
            html = await asyncio.to_thread(trafilatura.fetch_url, result.url)

        if not html:
            return None

        clean_text = trafilatura.extract(
            html,
            url=result.url,
            include_comments=False,
            include_tables=False,
            include_links=False,
            with_metadata=False,
            favor_precision=True,
            output_format="txt",
        )
        if not clean_text:
            return None

        metadata = dict(result.metadata)
        metadata.update(self._extract_metadata_from_html(html, result.url))
        if result.snippet:
            metadata["search_snippet"] = result.snippet

        title = metadata.get("page_title") or result.title or result.url
        normalized_text = self._postprocess_extracted_text(
            clean_text,
            url=result.url,
            title=title,
        )[: self.max_content_chars]
        tokens = tokenize(f"{title} {normalized_text} {result.snippet}")
        return ScrapedDocument(
            url=result.url,
            title=title,
            clean_content=normalized_text,
            metadata=metadata,
            tokens=tokens,
        )

    async def _request_with_backoff(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        attempts = len(BACKOFF_DELAYS) + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                await self._respect_rate_limit()
                response = await client.request(method, url, params=params)
                response.raise_for_status()
                return response
            except Exception as exc:
                last_error = exc
                if attempt >= len(BACKOFF_DELAYS):
                    break
                await asyncio.sleep(BACKOFF_DELAYS[attempt])
        assert last_error is not None
        raise last_error

    async def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_started
        remaining = self.rate_limit_seconds - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_request_started = time.monotonic()

    async def _is_allowed_by_robots(self, client: httpx.AsyncClient, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False

        base = f"{parsed.scheme}://{parsed.netloc}"
        async with self._robots_lock:
            parser = self._robots_cache.get(base)
            if parser is None:
                robots_url = urljoin(base, "/robots.txt")
                parser = RobotFileParser()
                try:
                    response = await self._request_with_backoff(client, "GET", robots_url)
                    parser.parse(response.text.splitlines())
                except Exception:
                    parser.parse([])
                self._robots_cache[base] = parser
        return parser.can_fetch(self.user_agent, url)

    def rank_documents(self, query: str, documents: list[ScrapedDocument]) -> list[ResearchResult]:
        if not documents:
            return []

        corpus = [document.tokens for document in documents]
        bm25 = BM25Okapi(corpus)
        query_tokens = tokenize(query)
        raw_scores = bm25.get_scores(query_tokens)
        max_score = max(raw_scores) if len(raw_scores) else 0.0

        ranked: list[ResearchResult] = []
        for document, raw_score in sorted(
            zip(documents, raw_scores, strict=True),
            key=lambda item: item[1],
            reverse=True,
        ):
            normalized_score = 0.0 if max_score <= 0 else float(raw_score / max_score)
            if normalized_score < self.score_threshold:
                continue
            ranked.append(
                ResearchResult(
                    rank_score=round(normalized_score, 4),
                    url=document.url,
                    title=document.title,
                    clean_content=document.clean_content,
                    metadata=document.metadata,
                )
            )
        return ranked

    @staticmethod
    def _extract_metadata_from_html(html: str, url: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        title = ""
        if soup.title and soup.title.string:
            title = unescape(soup.title.string.strip())

        description = ""
        meta_description = soup.find("meta", attrs={"name": "description"})
        if meta_description and meta_description.get("content"):
            description = meta_description["content"].strip()

        canonical = ""
        canonical_link = soup.find("link", attrs={"rel": lambda value: value and "canonical" in value})
        if canonical_link and canonical_link.get("href"):
            canonical = canonical_link["href"].strip()

        backlinks = sorted(
            {
                urljoin(url, anchor["href"])
                for anchor in soup.find_all("a", href=True)
                if anchor["href"].startswith(("http://", "https://", "/"))
            }
        )[:20]

        metadata: dict[str, Any] = {}
        if title:
            metadata["page_title"] = title
        if description:
            metadata["meta_description"] = description
        if canonical:
            metadata["canonical_url"] = canonical
        if backlinks:
            metadata["backlinks"] = backlinks
        return metadata

    @staticmethod
    def _postprocess_extracted_text(text: str, url: str, title: str) -> str:
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", normalized)
        normalized = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", normalized)
        normalized = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", normalized)

        paragraphs = []
        seen: set[str] = set()
        is_live_page = any(hint in f"{title} {url}".lower() for hint in LIVE_PAGE_HINTS)

        for raw_paragraph in normalized.split("\n"):
            paragraph = " ".join(raw_paragraph.split()).strip()
            if not paragraph:
                continue

            folded = paragraph.casefold()
            if folded in seen:
                continue

            if DeepResearchService._looks_like_boilerplate(paragraph, is_live_page=is_live_page):
                continue

            seen.add(folded)
            paragraphs.append(paragraph)

        if is_live_page:
            paragraphs = [paragraph for paragraph in paragraphs if len(paragraph) >= 40][:10]

        if not paragraphs:
            return " ".join(text.split())

        return "\n\n".join(paragraphs)

    @staticmethod
    def _looks_like_boilerplate(paragraph: str, *, is_live_page: bool) -> bool:
        lower = paragraph.lower()
        boilerplate_patterns = (
            r"^published on\b",
            r"^this live page is now closed\b",
            r"^follow our coverage here\b",
            r"^latest updates\b",
            r"^live\b$",
            r"^news\b$",
        )
        if any(re.search(pattern, lower) for pattern in boilerplate_patterns):
            return True

        if is_live_page and len(paragraph) < 40:
            if re.search(r"\b\d+\s*(min|mins|hour|hours|day|days)\s+ago\b", lower):
                return True
            if re.search(r"\b\d{1,2}:\d{2}\b", lower):
                return True

        return False

    async def _searxng_endpoint_ready(self) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=min(self.timeout_seconds, 5.0),
                headers={"User-Agent": self.user_agent},
            ) as client:
                response = await client.get(
                    f"{self.searxng_base_url}/search",
                    params={"q": "healthcheck", "format": "json"},
                )
            return response.status_code == 200
        except Exception:
            return False

    async def _wait_for_searxng_ready(self) -> None:
        deadline = time.monotonic() + self.searxng_startup_timeout_seconds
        while time.monotonic() < deadline:
            if await self._searxng_endpoint_ready():
                return
            await asyncio.sleep(1)
        raise RuntimeError(f"SearXNG did not become ready within {self.searxng_startup_timeout_seconds} seconds.")

    def _start_searxng_runtime(self) -> bool:
        if shutil.which("docker") is None:
            return False

        container = self.searxng_container_name
        inspect = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        existing = {line.strip() for line in inspect.stdout.splitlines() if line.strip()}

        if container in existing:
            result = subprocess.run(
                ["docker", "start", container],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return False
            return self._configure_searxng_container()

        parsed = urlparse(self.searxng_base_url)
        host_port = parsed.port or 8888
        result = subprocess.run(
            [
                "docker",
                "run",
                "--name",
                container,
                "-p",
                f"{host_port}:8080",
                "-d",
                "searxng/searxng",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return False
        return self._configure_searxng_container()

    def _configure_searxng_container(self) -> bool:
        command = (
            "from pathlib import Path\n"
            "settings_path = Path('/etc/searxng/settings.yml')\n"
            "text = settings_path.read_text()\n"
            "old = '  formats:\\n    - html\\n'\n"
            "new = '  formats:\\n    - html\\n    - json\\n'\n"
            "if new not in text and old in text:\n"
            "    text = text.replace(old, new, 1)\n"
            "if '  bind_address: \"127.0.0.1\"\\n' in text:\n"
            "    text = text.replace('  bind_address: \"127.0.0.1\"\\n', '  bind_address: \"0.0.0.0\"\\n', 1)\n"
            "if '  method: \"POST\"\\n' in text:\n"
            "    text = text.replace('  method: \"POST\"\\n', '  method: \"GET\"\\n', 1)\n"
            "settings_path.write_text(text)\n"
        )
        configure = subprocess.run(
            ["docker", "exec", self.searxng_container_name, "python3", "-c", command],
            capture_output=True,
            text=True,
            check=False,
        )
        if configure.returncode != 0:
            return False

        restart = subprocess.run(
            ["docker", "restart", self.searxng_container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return restart.returncode == 0

    def _schedule_shutdown_if_owned(self) -> None:
        if not self._owns_searxng_runtime:
            return
        self._cancel_pending_shutdown()
        self._searxng_stop_task = asyncio.create_task(self._shutdown_searxng_after_idle())

    def _cancel_pending_shutdown(self) -> None:
        if self._searxng_stop_task and not self._searxng_stop_task.done():
            self._searxng_stop_task.cancel()
        self._searxng_stop_task = None

    async def _shutdown_searxng_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.searxng_idle_timeout_seconds)
            await asyncio.to_thread(self._stop_owned_searxng_runtime)
        except asyncio.CancelledError:
            return

    def _stop_owned_searxng_runtime(self) -> None:
        if not self._owns_searxng_runtime or shutil.which("docker") is None:
            return
        subprocess.run(
            ["docker", "stop", self.searxng_container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        self._owns_searxng_runtime = False


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in "".join(char.lower() if char.isalnum() else " " for char in text).split()
        if len(token) > 1 or token.isdigit()
    ]


service = DeepResearchService()
mcp = FastMCP("DeepResearchServer")


@mcp.tool(description="Search the web deeply via SearXNG, scrape pages, and rank the extracted content.")
async def deep_web_research(query: str, max_results: int = DEFAULT_MAX_RESULTS, ctx: Context | None = None) -> ResearchResponse:
    return await service.deep_research(query=query, max_results=max_results, ctx=ctx)


if __name__ == "__main__":
    sys.stderr.write("Starting DeepResearchServer on stdio transport\n")
    sys.stderr.flush()
    mcp.run()
