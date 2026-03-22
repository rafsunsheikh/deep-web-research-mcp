# Deep Web Research MCP

`deep_web_research_mcp` is a local web-research system built around an MCP server and a terminal client. It uses SearXNG for search discovery, Trafilatura for article extraction, and BM25 for ranking the cleaned content against the user query.

The project supports two ways of using the same backend:

- an MCP server for Codex, Inspector, or any MCP-compatible host
- a terminal client for direct local use or for piping context into a local LLM such as Ollama

## What It Does

Given a query, the system:

1. queries SearXNG for candidate web results
2. fetches and extracts article text with Trafilatura
3. cleans up noisy live-page output
4. ranks documents with BM25
5. returns structured, cleaned research results

Each result includes:

- `rank_score`
- `url`
- `title`
- `clean_content`
- `metadata`

## Project Files

- `server.py`: FastMCP server and core research service
- `deep_research_client.py`: terminal client for direct research queries
- `scripts/install_local.sh`: first-time installer for local setup
- `scripts/run_server_ready.sh`: starts the MCP server after verifying local readiness
- `tests/test_server.py`: server and lifecycle tests
- `tests/test_client.py`: client formatting tests

## Requirements

The local setup scripts expect:

- `python3`
- `docker`
- `curl`

Optional but useful:

- `node` and `npm` for the MCP Inspector

## Quick Start

Clone the repo and run the installer:

```bash
git clone https://github.com/rafsunsheikh/deep-web-research-mcp.git 
cd deep-web-research-mcp
./scripts/install_local.sh
```

The installer will:

- create `.venv`
- install Python dependencies
- pull and configure a SearXNG Docker container
- enable SearXNG JSON output
- run the test suite

After setup, start the MCP server:

```bash
./scripts/run_server_ready.sh
```

The server runs on MCP stdio transport. It is expected to wait for requests and appear idle in the terminal.

## How SearXNG Is Managed

This project uses SearXNG as the search discovery layer.

Current lifecycle behavior:

- the MCP server checks whether SearXNG is reachable when a query arrives
- if SearXNG is not running, the server tries to start the Docker container automatically
- after the last request, SearXNG stays alive for 10 minutes
- after 10 minutes of inactivity, the server stops the SearXNG container

This means a user can keep only `server.py` running and let the project manage SearXNG on demand.

The default container name is:

```text
deep-research-searxng
```

The default SearXNG endpoint is:

```text
http://localhost:8888
```

## Using the Terminal Client

The terminal client calls the same research service used by the MCP server.

Basic usage:

```bash
./.venv/bin/python deep_research_client.py "latest breakthroughs in solid-state batteries"
```

This prints a clean text context block suitable for reading directly or pasting into a prompt.

Useful options:

```bash
./.venv/bin/python deep_research_client.py --max-results 5 "solid-state battery breakthroughs"
./.venv/bin/python deep_research_client.py --include-metadata "local LLM context windows"
./.venv/bin/python deep_research_client.py --json "state of the art local LLM context windows"
```

Recommended:

- keep `--max-results` at the default or higher
- avoid `--max-results 1` unless you explicitly want only one source

## Using the MCP Server

Start the server:

```bash
./scripts/run_server_ready.sh
```

Or directly:

```bash
./.venv/bin/python server.py
```

The MCP tool exposed by the server is:

```text
deep_web_research
```

It accepts:

- `query: str`
- `max_results: int = 5`

## Testing the System

Run the automated tests:

```bash
./.venv/bin/python -m pytest -q
```

Run a direct pipeline test without MCP:

```bash
./.venv/bin/python - <<'PY'
import asyncio
from server import DeepResearchService

async def main():
    service = DeepResearchService()
    response = await service.deep_research("state of the art local LLM context windows", max_results=5)
    print(response.model_dump_json(indent=2))

asyncio.run(main())
PY
```

Test the server with MCP Inspector:

```bash
npx @modelcontextprotocol/inspector ./.venv/bin/python server.py
```

If Inspector says the port is already in use:

```bash
pkill -f '@modelcontextprotocol/inspector'
```

## Codex MCP Configuration

To connect the server to Codex, add this to `~/.codex/config.toml`:

```toml
[mcp_servers.deep_research]
command = "/absolute/path/to/deep_web_research_mcp/.venv/bin/python"
args = ["/absolute/path/to/deep_web_research_mcp/server.py"]
```

Then restart Codex and verify with `/mcp`.

## Using With Ollama

The simplest Ollama integration is to generate context with the terminal client and inject it into the prompt.

Example:

```bash
CONTEXT="$(./.venv/bin/python deep_research_client.py 'state of the art local LLM context windows')"

ollama run llama3.1 "$(cat <<EOF
Use the research context below to answer the question.

$CONTEXT

Question: What is the current state of the art for local LLM context windows?
EOF
)"
```

If a wrapper script or agent needs structured output, use:

```bash
./.venv/bin/python deep_research_client.py --json "state of the art local LLM context windows"
```

## Output Quality Notes

The system includes a cleanup pass for live/update pages. It:

- preserves paragraph breaks
- removes common live-page noise like `Latest Updates` and timestamp-only lines
- keeps article content more readable than a raw flattened scrape

Still, output quality depends on the source page. Some sites are cleaner than others.

## Troubleshooting

### SearXNG returns `403 Forbidden`

That usually means JSON output is not enabled in SearXNG. The installer handles this automatically for the Docker container used by this repo.

### `curl http://localhost:8888/search?q=test&format=json` fails

Possible causes:

- Docker is not running
- the SearXNG container was never created
- the container is stopped
- port `8888` is already used by another service

Try:

```bash
docker ps -a
docker start deep-research-searxng
curl "http://localhost:8888/search?q=test&format=json"
```

### MCP server returns empty results

Possible causes:

- stale Inspector or server process
- one or more candidate pages were filtered out after scraping
- the query is too narrow

Try:

```bash
pkill -f 'server.py'
pkill -f '@modelcontextprotocol/inspector'
./scripts/run_server_ready.sh
```

Then retry the query.

### The server prints logs and breaks MCP transport

This server must not print operational output to `stdout`. Logging should go to `stderr` only.

## Development Notes

This project is currently designed for local use and local MCP integration. It is not yet packaged as a public production service.

The test suite currently covers:

- text tokenization
- BM25 ranking order
- Trafilatura-based extraction shaping
- MCP tool discovery over stdio
- client formatting
- local setup script syntax
- SearXNG auto-start and idle shutdown behavior

Run all tests with:

```bash
./.venv/bin/python -m pytest -q
```

## Publishing Checklist

Before pushing the repo:

- verify the absolute Codex paths in your local config are not committed
- confirm Docker is installed on the target machine
- run `./scripts/install_local.sh`
- run `./.venv/bin/python -m pytest -q`
- test one terminal client query
- test one MCP query

## License

This project is licensed under the MIT License. See `LICENSE`.
