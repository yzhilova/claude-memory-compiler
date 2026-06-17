# LLM Personal Knowledge Base

**Your AI conversations compile themselves into a searchable knowledge base.**

> **Fork note.** This is a patched fork of [coleam00/claude-memory-compiler](https://github.com/coleam00/claude-memory-compiler) maintained for reuse across multiple projects. Fixes on top of upstream:
> - **Capture actually fires on Claude Desktop** — `SessionEnd` never runs there ([claude-code#45514](https://github.com/anthropics/claude-code/issues/45514)), so capture is driven from `SessionStart` via a bounded, detached `backfill --sweep`.
> - **Sweep is scoped to the current project** — without `--project` the sweep ingested sessions from *every* repo under `~/.claude/projects/`; it now derives and passes the project id (using the `--project=` form, since the id starts with `-`).
> - **Headless auth** — `flush.py`/`compile.py` load `CLAUDE_CODE_OAUTH_TOKEN` from `.env` so the spawned CLI authenticates outside the desktop app.
> - **Compile prompt no longer scales O(N)** — it stopped inlining every article body (which blew past the input window as the KB grew) in favor of index-guided retrieval.
> - Concurrency lock against duplicate daily-log blocks; captured CLI stderr in `FLUSH_ERROR`; compact, never-truncated context catalog at session start.
>
> Bootstrapping a new project? See [`skills/obsidian-kb-setup`](skills/obsidian-kb-setup/SKILL.md) — a Claude Code skill that sets all of this up automatically ([install notes](skills/README.md)).

Adapted from [Karpathy's LLM Knowledge Base](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) architecture, but instead of clipping web articles, the raw data is your own conversations with Claude Code. When a session ends (or auto-compacts mid-session), Claude Code hooks capture the conversation transcript and spawn a background process that uses the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk) to extract the important stuff - decisions, lessons learned, patterns, gotchas - and appends it to a daily log. You then compile those daily logs into structured, cross-referenced knowledge articles organized by concept. Retrieval uses a simple index file instead of RAG - no vector database, no embeddings, just markdown.

Anthropic has clarified that personal use of the Claude Agent SDK is covered under your existing Claude subscription (Max, Team, or Enterprise) - no separate API credits needed. Unlike OpenClaw, which requires API billing for its memory flush, this runs on your subscription.

## Quick Start

This fork ships a Claude Code skill that does the whole setup for you. Install
it once, then run it in any project.

### 1. Install the skill (once per machine)

Clone this fork somewhere permanent, then symlink the skill into Claude Code's
user-level skills directory so it's available in every project:

```bash
git clone https://github.com/yzhilova/claude-memory-compiler ~/claude-memory-compiler
mkdir -p ~/.claude/skills
ln -sfn ~/claude-memory-compiler/skills/obsidian-kb-setup ~/.claude/skills/obsidian-kb-setup
```

The symlink keeps the skill in sync when you `git pull`. (Use `cp -r` instead for a detached copy.)

### 2. Run it in a project

From the project root, in Claude Code:

```
/obsidian-kb-setup
```

It clones the fork into a self-contained `./claude-memory-compiler/` subdirectory,
wires the project hooks, seeds `.env` auth (it asks you for a `claude setup-token`
value if one isn't already in your environment), and registers the Obsidian
vault. The only thing added to your project root is `.claude/settings.json`.

### 3. Verify it's set up correctly

Reopen Claude Code in the project (hooks load at session start), then confirm:

- **Context injection** — a "Knowledge Base Catalog" block appears in the new session. That's the SessionStart hook firing.
- **Auth** (background capture depends on it):
  ```bash
  uv run --directory claude-memory-compiler python -c "from dotenv import load_dotenv; import os; load_dotenv('.env'); print('token present:', bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')))"
  ```
  Expect `token present: True`.
- **Structural health**:
  ```bash
  uv run --directory claude-memory-compiler python scripts/lint.py --structural-only
  ```
- **Obsidian** — the vault appears in the switcher as `<project>/claude-memory-compiler`.

From there your conversations accumulate in `claude-memory-compiler/daily/`. After
6 PM local time, the next flush auto-triggers compilation of that day's logs into
knowledge articles; you can also run it manually any time:

```bash
uv run --directory claude-memory-compiler python scripts/compile.py
```

> **Prefer to wire it up by hand?** Tell your AI coding agent: *"Clone
> https://github.com/yzhilova/claude-memory-compiler into a
> `claude-memory-compiler/` subfolder of this project and `uv sync` it. Add a
> project-root `.claude/settings.json` whose SessionStart/PreCompact/SessionEnd
> hooks run that subfolder's `hooks/*.py` via `uv run --directory
> claude-memory-compiler` (merge, don't overwrite). Create
> `claude-memory-compiler/.env` with my `CLAUDE_CODE_OAUTH_TOKEN`. Read its
> AGENTS.md for the full technical reference."*

## How It Works

```
Conversation -> SessionEnd/PreCompact hooks -> flush.py extracts knowledge
    -> daily/YYYY-MM-DD.md -> compile.py -> knowledge/concepts/, connections/, qa/
        -> SessionStart hook injects index into next session -> cycle repeats
```

- **Hooks** capture conversations automatically — `SessionEnd` + `PreCompact` on CLI, and `SessionStart` (a bounded `backfill --sweep`) on Claude Desktop, where `SessionEnd` doesn't fire
- **flush.py** calls the Claude Agent SDK to decide what's worth saving, and after 6 PM triggers end-of-day compilation automatically
- **compile.py** turns daily logs into organized concept articles with cross-references (triggered automatically or run manually)
- **query.py** answers questions using index-guided retrieval (no RAG needed at personal scale)
- **lint.py** runs 7 health checks (broken links, orphans, contradictions, staleness)

## Key Commands

Run these from inside the `claude-memory-compiler/` vault directory, or prefix each with `uv run --directory claude-memory-compiler …` from the project root.

```bash
uv run python scripts/compile.py                    # compile new daily logs
uv run python scripts/query.py "question"            # ask the knowledge base
uv run python scripts/query.py "question" --file-back # ask + save answer back
uv run python scripts/lint.py                        # run health checks
uv run python scripts/lint.py --structural-only      # free structural checks only
```

## Why No RAG?

Karpathy's insight: at personal scale (50-500 articles), the LLM reading a structured `index.md` outperforms vector similarity. The LLM understands what you're really asking; cosine similarity just finds similar words. RAG becomes necessary at ~2,000+ articles when the index exceeds the context window.

## Technical Reference

See **[AGENTS.md](AGENTS.md)** for the complete technical reference: article formats, hook architecture, script internals, cross-platform details, costs, and customization options. AGENTS.md is designed to give an AI agent everything it needs to understand, modify, or rebuild the system.
