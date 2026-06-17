---
name: obsidian-kb-setup
description: Set up a claude-memory-compiler knowledge base vault in the current project folder. Checks prerequisites (uv, Claude credentials, Obsidian, Python, git), clones the patched fork from GitHub, creates the directory structure, wires Claude Code hooks (non-destructively), seeds project-local .env auth, and registers the vault in Obsidian. Use when starting a new project that should have automatic conversation capture and an Obsidian-compatible knowledge base. macOS only.
---

# Setup KB Vault

## Purpose

Bootstraps the [claude-memory-compiler](https://github.com/yzhilova/claude-memory-compiler)
system (a patched fork — see "Why this fork" below) in the current working
directory. Conversations are automatically captured into daily logs, compiled
into a structured knowledge base, and injected back into future sessions via
Claude Code hooks.

All steps run from the **current working directory** (the new project folder).
macOS only.

### Why this fork (not coleam00 upstream)

`yzhilova/claude-memory-compiler` carries fixes that upstream lacks. Cloning
upstream re-introduces these bugs, so always clone the fork:
- **Capture fires on Claude Desktop.** `SessionEnd` never runs in the desktop
  app, so upstream's "flush on session end" silently never happens. The fork
  drives capture from `SessionStart` via a bounded, detached `backfill --sweep`.
- **Sweep is project-scoped** (reads the active project from the hook's
  `transcript_path`) so it can't ingest other repos' sessions.
- **Headless auth** via project-local `.env` (`CLAUDE_CODE_OAUTH_TOKEN`).
- **Compile prompt is O(1)**, not O(articles), so compile doesn't fail as the
  KB grows.

## Before you start — read this

- **Intended for a fresh project or a dedicated vault folder.** This skill adds
  `hooks/`, `scripts/`, `knowledge/`, `daily/`, `AGENTS.md`, etc. to the
  **current folder** and wires hooks into its `.claude/settings.json`. It
  merges into existing `.claude/settings.json` / `.gitignore` rather than
  overwriting them, but it is still cleanest in a folder dedicated to the vault.
- **Do not double-wire hooks.** If you already run the compiler hooks at
  **user level** (`~/.claude/settings.json`), adding **project-level** hooks
  here makes both fire on every session — double capture, double context
  injection, and sweeps into the wrong vault. Pick ONE model:
  - *Per-project vaults* (what this skill sets up): remove the user-level
    compiler hooks first, so each project captures into its own vault.
  - *One shared vault*: don't run this skill; keep the user-level hooks.

---

## Phase 1: Prerequisite Checks

Run all checks. Fix failures before moving to Phase 2. Do not ask the user to
run commands manually — handle everything in tool calls.

### Check 1 — `uv` installed

```bash
uv --version 2>/dev/null || $HOME/.local/bin/uv --version 2>/dev/null
```

**Pass:** prints a version string.
**Fail:** install and source immediately (do NOT tell the user to restart their terminal):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```
After installing, **always prefix subsequent `uv` calls with `export PATH="$HOME/.local/bin:$PATH" &&`** for the rest of this session, since the shell PATH won't update automatically.

### Check 2 — Claude credentials available

The background flush/compile/backfill processes spawn a `claude` CLI that
**cannot see** the desktop app's host-managed OAuth, so they need their own
long-lived token. The fork's scripts read it from a project-local `.env`
(`CLAUDE_CODE_OAUTH_TOKEN`). Check whether a token is already available:

```bash
# Already exported in the environment?
echo "$CLAUDE_CODE_OAUTH_TOKEN"
# Already saved in this project's .env (only exists after Phase 2 on re-runs)?
[ -f .env ] && grep -q 'CLAUDE_CODE_OAUTH_TOKEN=.\+' .env && echo "token in .env"
```

**Pass:** either prints a token / "token in .env". Capture the value as `token`.
**Fail:** the user must generate one. Tell them:
> In your own terminal (not Claude Code), run: `claude setup-token`
> It opens a browser (requires a Claude Pro/Max subscription) and prints a
> `sk-ant-oat01-…` value. If `claude` isn't installed, create an API key at
> console.anthropic.com → API Keys instead.
> Paste the token back here.

Capture the pasted value as `token`. **It is written to `.env` in Phase 2**
(after `.env.example` is in place) — never to `~/.zshrc` (non-interactive hook
shells don't source it) and never committed (`.env` is gitignored).

### Check 3 — Obsidian installed

```bash
ls /Applications/Obsidian.app 2>/dev/null && echo "found" || echo "not found"
```

**Pass:** "found".
**Fail:** tell the user to download from https://obsidian.md/download — Obsidian is optional for hooks to work but required for the vault UI.

### Check 4 — Python 3.12+ available via `uv`

```bash
export PATH="$HOME/.local/bin:$PATH" && uv python list --only-installed | grep -E "3\.(1[2-9]|[2-9][0-9])"
```

**Pass:** at least one installed 3.12+ version listed.
**Fail:** install automatically without asking the user:
```bash
export PATH="$HOME/.local/bin:$PATH" && uv python install 3.12
```

### Check 5 — `git` installed

```bash
git --version
```

**Pass:** prints a version string.
**Fail:** `xcode-select --install`

---

## Phase 2: Scaffold Setup

### Step 1 — Clone scaffold (the fork)

Clear any previous attempt first, then clone the **fork**:
```bash
rm -rf /tmp/claude-memory-compiler-scaffold
git clone --depth 1 https://github.com/yzhilova/claude-memory-compiler /tmp/claude-memory-compiler-scaffold
```

### Step 2 — Copy scaffold files

```bash
cp -r /tmp/claude-memory-compiler-scaffold/hooks .
cp -r /tmp/claude-memory-compiler-scaffold/scripts .
cp /tmp/claude-memory-compiler-scaffold/AGENTS.md .
cp /tmp/claude-memory-compiler-scaffold/pyproject.toml .
cp /tmp/claude-memory-compiler-scaffold/uv.lock .
cp /tmp/claude-memory-compiler-scaffold/.env.example .
rm -rf /tmp/claude-memory-compiler-scaffold
# Start clean: drop any runtime artifacts that rode along
rm -rf scripts/__pycache__
rm -f scripts/state.json scripts/last-flush.json scripts/flush.log \
      scripts/compile.log scripts/backfill-state.json scripts/.backfill.lock
```

### Step 3 — Create directory structure

```bash
mkdir -p daily
mkdir -p knowledge/concepts knowledge/connections knowledge/qa
mkdir -p reports
mkdir -p .claude
```

### Step 4 — Seed project-local `.env` auth

Write the `token` captured in Check 2 into `.env` (copy from `.env.example` so
its comments survive). Use the Write/Edit tools or Python — never `echo >>`
(long tokens get split across lines). `.env` is gitignored; never commit it.

```python
import pathlib
token = "<token from Check 2>"  # the sk-ant-oat01-… string
example = pathlib.Path(".env.example")
env = pathlib.Path(".env")
base = example.read_text() if example.exists() else "CLAUDE_CODE_OAUTH_TOKEN=\n"
# Fill the empty value on the CLAUDE_CODE_OAUTH_TOKEN line.
lines = []
done = False
for line in base.splitlines():
    if line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") and not done:
        lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={token}")
        done = True
    else:
        lines.append(line)
if not done:
    lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={token}")
env.write_text("\n".join(lines) + "\n")
```

### Step 5 — Wire Claude Code hooks (non-destructive merge)

**Merge** the hooks into `.claude/settings.json` instead of overwriting it, so
an existing project keeps its other settings. Run this Python:

```python
import json, pathlib
settings_path = pathlib.Path(".claude/settings.json")
settings = {}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text() or "{}")
    except json.JSONDecodeError:
        settings = {}

def hook(script):
    return {
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": f'export PATH="$HOME/.local/bin:$PATH" && uv run python hooks/{script}',
            "timeout": 15,
        }],
    }

settings.setdefault("hooks", {})
settings["hooks"]["SessionStart"] = [hook("session-start.py")]
settings["hooks"]["PreCompact"] = [hook("pre-compact.py")]
settings["hooks"]["SessionEnd"] = [hook("session-end.py")]
settings_path.write_text(json.dumps(settings, indent=2) + "\n")
```

> Note: on Claude Desktop, `SessionEnd`/`PreCompact` may not fire — the
> `SessionStart` hook also drives a bounded `backfill --sweep` of recent prior
> sessions, so capture still happens. All three are wired for portability
> (CLI sessions do fire `SessionEnd`).

### Step 6 — Update `pyproject.toml` project name

Derive the name from the current folder and write it into `pyproject.toml`:
```python
import pathlib, re
folder = pathlib.Path.cwd().name.lower()
name = re.sub(r'[^a-z0-9]+', '-', folder).strip('-') + '-kb'
toml_path = pathlib.Path('pyproject.toml')
content = toml_path.read_text()
content = re.sub(r'(?m)^name = ".*"', f'name = "{name}"', content)
toml_path.write_text(content)
```

### Step 7 — Ensure vault paths are gitignored (non-destructive)

If this folder is (or will be) a git repo, make sure generated vault content
and secrets are ignored — **append** missing entries rather than overwrite an
existing `.gitignore`:

```python
import pathlib
gi = pathlib.Path(".gitignore")
needed = ["daily/", "knowledge/", "reports/", ".env", ".env.local",
          ".obsidian/", "scripts/state.json", "scripts/last-flush.json",
          "scripts/flush.log", "scripts/compile.log",
          "scripts/backfill-state.json", "scripts/.backfill.lock",
          "scripts/__pycache__/"]
existing = gi.read_text().splitlines() if gi.exists() else []
missing = [e for e in needed if e not in existing]
if missing:
    block = "\n# claude-memory-compiler vault (generated + secrets)\n" + "\n".join(missing) + "\n"
    gi.write_text((gi.read_text().rstrip() + "\n" if gi.exists() else "") + block)
```

### Step 8 — Initialize knowledge base index files

Create `knowledge/index.md`:
```markdown
# Knowledge Base Index

| Article | Summary | Compiled From | Updated |
|---------|---------|---------------|---------|
```

Create `knowledge/log.md`:
```markdown
# Build Log
```

### Step 9 — Install Python dependencies

```bash
export PATH="$HOME/.local/bin:$PATH" && uv sync
```

---

## Phase 3: Obsidian Vault Registration

Read the current vault config:
```bash
cat ~/Library/Application\ Support/obsidian/obsidian.json
```

Generate values for the new vault entry:
```bash
pwd                                                        # absolute path
python3 -c "import time; print(int(time.time()*1000))"    # timestamp ms
openssl rand -hex 8                                        # new vault ID
```

Add the new entry under `vaults`, preserving all existing entries, and write the file back using the Write tool (never `echo` or `>>` for JSON files):
```json
"<new-hex-id>": {
  "path": "<output of pwd>",
  "ts": <output of python3 timestamp>,
  "open": false
}
```

---

## Phase 4: Verification

1. **Close and reopen Claude Code** in this project folder — the SessionStart
   hook should fire and inject a "Knowledge Base Catalog" block into the
   session context.

2. **Confirm auth works** (background capture depends on it):
   ```bash
   export PATH="$HOME/.local/bin:$PATH" && uv run python scripts/flush.py --self-test 2>/dev/null || \
   export PATH="$HOME/.local/bin:$PATH" && uv run python -c "from dotenv import load_dotenv; import os; load_dotenv('.env'); print('token present:', bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')))"
   ```
   Expect `token present: True`. (If `flush.py` later logs `FLUSH_ERROR`, the
   token is missing or invalid — see Troubleshooting.)

3. **Open Obsidian** — the new vault should appear in the vault switcher. Open
   it; Obsidian will create `.obsidian/` config on first open.

4. **After a few sessions**, compile the daily logs into articles:
   ```bash
   export PATH="$HOME/.local/bin:$PATH" && uv run python scripts/compile.py
   ```

5. **Health check** at any time:
   ```bash
   export PATH="$HOME/.local/bin:$PATH" && uv run python scripts/lint.py --structural-only
   ```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| SessionStart hook doesn't fire | `uv` not installed / not in PATH at hook time | Hook commands prefix `export PATH="$HOME/.local/bin:$PATH"`; ensure uv is in `~/.local/bin` and run `uv sync` |
| `flush.py` logs `FLUSH_ERROR` in `scripts/flush.log` | Missing/invalid token | Confirm `.env` has a valid `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…`; regenerate with `claude setup-token` |
| Context injected twice / sweeps into the wrong vault | Both user-level AND project-level compiler hooks active | Remove one set (see "Before you start") |
| Sweep captures nothing on Claude Desktop | Older session-start.py without the transcript_path fix | You're on upstream, not the fork — re-clone from `yzhilova/claude-memory-compiler` |
| `uv sync` fails with Python version error | No 3.12+ installed | `export PATH="$HOME/.local/bin:$PATH" && uv python install 3.12` |
| Obsidian vault doesn't appear | `obsidian.json` not saved correctly | Verify JSON is valid; restart Obsidian |
| Hooks fire but daily log stays empty | Session too short | Normal — `MIN_TURNS_TO_FLUSH = 1` in `session-end.py` |
| `git clone` fails with "destination path exists" | Previous failed attempt | `rm -rf /tmp/claude-memory-compiler-scaffold` then retry |
