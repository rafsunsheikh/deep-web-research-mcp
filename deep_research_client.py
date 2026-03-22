from __future__ import annotations

import argparse
import asyncio
import json
import sys

from server import DEFAULT_MAX_RESULTS, DeepResearchService, ResearchResponse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Terminal client for the Deep Research MCP service.",
    )
    parser.add_argument(
        "query",
        nargs="+",
        help="Question or search query to research.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"Maximum number of ranked results to return (default: {DEFAULT_MAX_RESULTS}).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of an LLM-friendly context block.",
    )
    parser.add_argument(
        "--include-metadata",
        action="store_true",
        help="Include selected metadata in the text output.",
    )
    return parser


async def run_query(query: str, max_results: int) -> ResearchResponse:
    service = DeepResearchService()
    return await service.deep_research(query=query, max_results=max_results)


def render_context(response: ResearchResponse, include_metadata: bool = False) -> str:
    lines = [
        f"Deep research context for: {response.query}",
        "",
    ]

    if not response.results:
        lines.append("No relevant web results were found.")
        return "\n".join(lines)

    for index, result in enumerate(response.results, start=1):
        lines.append(f"[Source {index}] {result.title}")
        lines.append(f"URL: {result.url}")
        lines.append(f"Rank score: {result.rank_score}")
        lines.append("Content:")
        lines.append(result.clean_content)
        if include_metadata:
            lines.append("Metadata:")
            for key in ("engine", "category", "published_date", "canonical_url", "meta_description"):
                value = result.metadata.get(key)
                if value:
                    lines.append(f"- {key}: {value}")
        lines.append("")

    lines.append("Use the source URLs above when citing or double-checking facts.")
    return "\n".join(lines).strip()


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    query = " ".join(args.query).strip()

    if not query:
        parser.error("query must not be empty")

    try:
        response = asyncio.run(run_query(query, max_results=args.max_results))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"deep_research_client error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(response.model_dump(), indent=2, ensure_ascii=False))
    else:
        print(render_context(response, include_metadata=args.include_metadata))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
