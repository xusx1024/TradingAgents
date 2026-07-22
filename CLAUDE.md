# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

TradingAgents is a multi-agent LLM financial trading framework that mirrors a real-world trading firm. Specialized agents (analysts, researchers, trader, risk management, portfolio manager) collaboratively evaluate markets and produce trading decisions. The framework uses **LangGraph** for agent orchestration and supports many LLM providers (OpenAI, Anthropic, Google, DeepSeek, Qwen, GLM, MiniMax, OpenRouter, Ollama, Azure, Bedrock, and any OpenAI-compatible endpoint).

## Build / test / lint

```bash
# Install (editable + dev deps)
pip install -e ".[dev]"

# Run all tests
pytest

# Run specific test files or patterns
pytest tests/test_memory_log.py
pytest -k "memory"

# Lint (ruff)
ruff check tradingagents/ cli/ tests/
ruff format --check tradingagents/ cli/ tests/

# Run the CLI
tradingagents              # installed entry point
python -m cli.main         # from source
tradingagents analyze --checkpoint --clear-checkpoints
```

Tests use markers (`unit`, `integration`, `smoke`) registered in `pyproject.toml` and `tests/conftest.py`. The conftest auto-injects placeholder API keys so tests don't hang. All test fixtures reset the global dataflows config between tests.

## Architecture

### LangGraph workflow pipeline

The graph (`tradingagents/graph/`) is the backbone. Nodes execute in fixed sequence:

1. **Analyst Team** (user-selectable, in order: market → sentiment → news → fundamentals) — each analyst uses `ToolNode` wrappers that route data calls through the vendor interface. Analysts loop on tool calls via conditional edges, then a "Msg Clear" node wipes messages and injects the resolved instrument context so the next analyst starts clean.
2. **Research Team** — Bull and Bear Researchers debate back and forth (configurable rounds via `max_debate_rounds`), then the Research Manager (structured output → `ResearchPlan`) renders a decision.
3. **Trader** — translates the research plan into a concrete transaction proposal (structured output → `TraderProposal`).
4. **Risk Management** — Aggressive / Conservative / Neutral analysts debate (configurable rounds via `max_risk_discuss_rounds`).
5. **Portfolio Manager** — final approval/rejection (structured output → `PortfolioDecision`), then `END`.

Key files:
- `tradingagents/graph/trading_graph.py` — `TradingAgentsGraph` class, the main entry point. Creates LLM clients, tool nodes, compiles the graph, manages checkpointing and the memory log.
- `tradingagents/graph/setup.py` — `GraphSetup.setup_graph()` builds the `StateGraph`, adds nodes, and wires conditional edges.
- `tradingagents/graph/conditional_logic.py` — `ConditionalLogic` controls graph routing (tool loops, debate rounds, risk rounds).
- `tradingagents/graph/propagation.py` — `Propagator` creates the initial `AgentState` dict and graph invocation args.
- `tradingagents/graph/analyst_execution.py` — Defines the analyst execution plan (`AnalystNodeSpec`), maps analyst keys to node names, and provides `AnalystWallTimeTracker`.

### Agent implementations

Each agent is a factory function that returns a callable node for the LangGraph pipeline. They live under `tradingagents/agents/`:

- **Analysts** (`analysts/`): `market_analyst`, `sentiment_analyst` (ingests news + StockTwits + Reddit), `news_analyst`, `fundamentals_analyst`. All use the quick-thinking LLM.
- **Researchers** (`researchers/`): `bull_researcher`, `bear_researcher` — adversarial debate pair.
- **Managers** (`managers/`): `research_manager`, `portfolio_manager` — structured-output decision makers using the deep-thinking LLM.
- **Risk** (`risk_mgmt/`): `aggressive_debator`, `conservative_debator`, `neutral_debator` — three-perspective risk debate.
- **Trader** (`trader/`): produces structured `TraderProposal`.

Agents are registered in `tradingagents/agents/__init__.py` and consumed by `GraphSetup`.

### Structured output (schemas)

Three decision-making agents produce typed Pydantic output via the provider's native structured-output mode:

- `ResearchPlan` (Research Manager) — recommendation (5-tier rating), rationale, strategic actions
- `TraderProposal` (Trader) — action (Buy/Hold/Sell), reasoning, optional price/sizing fields
- `PortfolioDecision` (Portfolio Manager) — rating, executive summary, investment thesis, optional price target/time horizon
- `SentimentReport` (Sentiment Analyst) — overall band (6-tier), score (0-10), confidence, narrative

Each schema has a `render_*()` function that produces the markdown format consumed by reports, memory log, and downstream agents. Defined in `tradingagents/agents/schemas.py`.

### Data vendor system

Data fetching goes through a vendor abstraction in `tradingagents/dataflows/interface.py`:

- Tools are organized into categories: `core_stock_apis`, `technical_indicators`, `fundamental_data`, `news_data`, `macro_data`, `prediction_markets`.
- Each tool is registered with vendor-specific implementations in `VENDOR_METHODS`.
- `route_to_vendor()` dispatches calls. Users configure vendors per-category or per-tool via config keys `data_vendors` / `tool_vendors`. The configured vendor chain IS the chain — no silent fallback to unconfigured vendors.
- Vendors: `yfinance` (default for most), `alpha_vantage`, `akshare` (China A-shares), `fred` (macro data), `polymarket` (prediction markets).
- Optional categories (`macro_data`, `prediction_markets`) degrade gracefully on failure instead of aborting the run.

### LLM client abstraction

`tradingagents/llm_clients/` provides a unified interface over multiple providers:

- `factory.py` — `create_llm_client()` dispatches by provider name. Lazy imports keep test collection lightweight.
- Native clients: `AnthropicClient`, `GoogleClient`, `AzureOpenAIClient`, `BedrockClient`.
- Everything OpenAI-compatible (OpenAI, DeepSeek, xAI, Qwen, GLM, MiniMax, OpenRouter, Ollama) routes through `OpenAIClient` with the provider registry from `openai_client.py`.
- `model_catalog.py` — curated model lists per provider (used by CLI model selection).
- `capabilities.py` — provider feature flags (e.g., structured output support).

### Configuration

`tradingagents/default_config.py` is the single source of truth:

- `DEFAULT_CONFIG` dict with all settings (LLM provider, models, debate rounds, data vendors, benchmarks, output language, etc.).
- `_ENV_OVERRIDES` maps `TRADINGAGENTS_*` env vars to config keys. Applied at import time so they work in both CLI and programmatic use.
- `_apply_env_overrides()` coerces env strings to the type of the existing default (bool, int, float, str).
- The graph reads config via `tradingagents/dataflows/config.py` (module-level global, set at init).

### Memory / persistence

Two persistence mechanisms:

1. **Decision log** (`tradingagents/agents/utils/memory.py` — `TradingMemoryLog`): Append-only markdown log at `~/.tradingagents/memory/trading_memory.md`. Each run stores a pending entry; on the next same-ticker run, `_resolve_pending_entries()` fetches realized returns, runs a reflection LLM call, and writes outcomes back atomically. Past context is injected into the Portfolio Manager prompt.

2. **Checkpoint resume** (`tradingagents/graph/checkpointer.py`): Opt-in via `--checkpoint` or `checkpoint_enabled: True`. Uses SQLite via `langgraph-checkpoint-sqlite`. State saved after each node; crashed runs resume from the last successful step. Checkpoints auto-cleared on success.

### Reporting

`tradingagents/reporting.py` — `write_report_tree()` writes per-section markdown files under a timestamped directory (`1_analysts/`, `2_research/`, `3_trading/`, `4_risk/`, `5_portfolio/`) plus a consolidated `complete_report.md`. Both the CLI and `TradingAgentsGraph.save_reports()` call this.

### CLI

`cli/main.py` is a `typer` app with a `Rich` TUI. The `analyze` command runs an interactive 8-step questionnaire (ticker, date, language, analysts, research depth, LLM provider, thinking agents, provider-specific config). Most steps are skippable via `TRADINGAGENTS_*` env vars for non-interactive/headless runs. Streams LangGraph output with live progress display. Entry point: `tradingagents = "cli.main:app"`.

## Key patterns

- **Two-tier LLM**: `quick_think_llm` for analysts, researchers, debaters, trader; `deep_think_llm` for Research Manager and Portfolio Manager. Configured via `deep_think_llm` / `quick_think_llm` config keys.
- **Instrument identity anchoring**: Before any agent runs, `resolve_instrument_identity()` does a deterministic yfinance lookup (cached via `@lru_cache`) to get the real company name/sector/exchange. This is injected into every agent's context to prevent the market analyst from pattern-matching price action to a wrong company. The symbol is normalized first (e.g., `XAUUSD` → `GC=F`).
- **Message clearing between agents**: `create_msg_delete()` removes all prior messages and injects a placeholder anchored to the resolved instrument context, preventing context-leak between analysts.
- **Idempotency guards**: Memory log entries check for duplicate pending entries before appending. Checkpoint thread IDs use `(ticker, date)` tuples so same ticker+date resumes.
- **Fail-open for optional data**: Instrument identity resolution and optional data categories degrade gracefully — they never block a run.
