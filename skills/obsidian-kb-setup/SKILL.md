---
name: obsidian-kb-setup
description: Set up a claude-memory-compiler knowledge base vault for the current project. Clones the patched fork into a self-contained ./claude-memory-compiler subdirectory, checks prerequisites (uv, Claude credentials, Obsidian, Python, git), wires Claude Code hooks at the project root (non-destructively), seeds project-local .env auth, and registers the vault in Obsidian. Use when starting a new project that should have automatic conversation capture and an Obsidian-compatible knowledge base. macOS only.
---

# Setup KB Vault

## Purpose

Bootstraps the [claude-memory-compiler](https://github.com/yzhilova/claude-memory-compiler)
system (a patched fork — see "Why this fork") for the current project.
Conversations are automatically captured into daily logs, compiled into a
structured knowledge base, and injected back into future sessions via Claude
Code hooks.

**Layout — nested, not flat.** The vault is a self-contained
`./claude-memory-compiler/` subdirectory of your project (tool + `knowledge/` +
`daily/` all live inside it). The only thing added to the project root is a
`.claude/settings.json` with the hooks. This keeps your project clean, matches
the proven setup, and lets you pull tool updates with
`cd claude-memory-compiler && git pull`.

macOS only. Run from the **project root**.

### Why this fork (not coleam00 upstream)

`yzhilova/claude-memory-compiler` carries fixes upstream lacks; cloning upstream
re-introduces these bugs:
- **Capture fires on Claude Desktop.** `SessionEnd` never runs in the desktop
  app, so upstream's "flush on session end" silently never happens. The fork
  drives capture from `SessionStart` via a bounded, detached `backfill --sweep`.
- **Sweep is project-scoped** (reads the active project from the hook's
  `transcript_path`) so it can't ingest other repos' sessions.
- **Headless auth** via project-local `.env` (`CLAUDE_CODE_OAUTH_TOKEN`).
- **Compile prompt is O(1)**, not O(articles), so compile doesn't fail as the
  KB grows.

### Before you start — one-vault-per-machine vs per-project

This skill sets up a **per-project** vault (project-level hooks). Do not also
run the compiler hooks at **user level** (`~/.claude/settings.json`) — if both
are active, every session double-fires and sweeps into the wrong vault. The
per-project model means: each project captures into its own
`claude-memory-compiler/` vault, and there are no global compiler hooks.

---

## Phase 1: Prerequisite Checks

Run all checks. Fix failures before Phase 2. Do not ask the user to run commands
manually — handle everything in tool calls.

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
After installing, **prefix subsequent `uv` calls with `export PATH="$HOME/.local/bin:$PATH" &&`** for the rest of this session.

### Check 2 — Claude credentials available

Background flush/compile/backfill spawn a `claude` CLI that **cannot see** the
desktop app's host-managed OAuth, so they need their own long-lived token, read
from `claude-memory-compiler/.env` (`CLAUDE_CODE_OAUTH_TOKEN`). Check whether one
is available:

```bash
echo "$CLAUDE_CODE_OAUTH_TOKEN"
[ -f claude-memory-compiler/.env ] && grep -q 'CLAUDE_CODE_OAUTH_TOKEN=.\+' claude-memory-compiler/.env && echo "token in .env"
```

**Pass:** prints a token / "token in .env". Capture the value as `token`.
**Fail:** the user must generate one. Tell them:
> In your own terminal (not Claude Code), run: `claude setup-token`
> It opens a browser (requires Claude Pro/Max) and prints a `sk-ant-oat01-…`
> value. If `claude` isn't installed, create an API key at
> console.anthropic.com → API Keys instead. Paste it back here.

Capture the pasted value as `token`; it is written to `.env` in Phase 2 Step 3
— never to `~/.zshrc` (non-interactive hook shells don't source it), never
committed (`.env` is gitignored).

### Check 3 — Obsidian installed

```bash
ls /Applications/Obsidian.app 2>/dev/null && echo "found" || echo "not found"
```

**Pass:** "found".
**Fail:** tell the user to download from https://obsidian.md/download — optional for hooks, required for the vault UI.

### Check 4 — Python 3.12+ available via `uv`

```bash
export PATH="$HOME/.local/bin:$PATH" && uv python list --only-installed | grep -E "3\.(1[2-9]|[2-9][0-9])"
```

**Pass:** at least one 3.12+ version listed.
**Fail:** `export PATH="$HOME/.local/bin:$PATH" && uv python install 3.12`

### Check 5 — `git` installed

```bash
git --version
```

**Pass:** prints a version string.
**Fail:** `xcode-select --install`

---

## Phase 2: Scaffold Setup

All paths below are relative to the **project root** (the current directory).

### Step 1 — Clone the fork into a `claude-memory-compiler/` subdir

```bash
# Idempotent: skip if it already exists (re-run safety)
if [ -d claude-memory-compiler/.git ]; then
  echo "vault already present; pulling latest" && git -C claude-memory-compiler pull --ff-only
else
  git clone https://github.com/yzhilova/claude-memory-compiler claude-memory-compiler
fi
```

The clone keeps its `.git` (origin = the fork) so you can `git -C claude-memory-compiler pull` for future tool updates. Its `knowledge/` and `daily/` are gitignored, so your captured content stays local and is never pushed.

### Step 2 — Create vault dirs and clean runtime artifacts

```bash
mkdir -p claude-memory-compiler/daily \
         claude-memory-compiler/knowledge/concepts \
         claude-memory-compiler/knowledge/connections \
         claude-memory-compiler/knowledge/qa \
         claude-memory-compiler/reports \
         .claude
rm -rf claude-memory-compiler/scripts/__pycache__
rm -f  claude-memory-compiler/scripts/state.json \
       claude-memory-compiler/scripts/last-flush.json \
       claude-memory-compiler/scripts/flush.log \
       claude-memory-compiler/scripts/compile.log \
       claude-memory-compiler/scripts/backfill-state.json \
       claude-memory-compiler/scripts/.backfill.lock
```

### Step 3 — Seed `.env` auth (inside the vault)

Write the `token` from Check 2 into `claude-memory-compiler/.env`, copying from
`.env.example` so its comments survive. Never `echo >>` (long tokens get split).

```python
import pathlib
token = "<token from Check 2>"  # the sk-ant-oat01-… string
vault = pathlib.Path("claude-memory-compiler")
example = vault / ".env.example"
env = vault / ".env"
base = example.read_text() if example.exists() else "CLAUDE_CODE_OAUTH_TOKEN=\n"
lines, done = [], False
for line in base.splitlines():
    if line.startswith("CLAUDE_CODE_OAUTH_TOKEN=") and not done:
        lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={token}"); done = True
    else:
        lines.append(line)
if not done:
    lines.append(f"CLAUDE_CODE_OAUTH_TOKEN={token}")
env.write_text("\n".join(lines) + "\n")
```

### Step 4 — Wire Claude Code hooks at the project root (non-destructive merge)

Write **absolute-path** hooks into `.claude/settings.json` (computed from the
current project path, so they work regardless of where Claude Code is launched
from). Merge into any existing settings rather than overwriting. Run this Python:

```python
import json, pathlib, shutil
project = pathlib.Path.cwd().resolve()
vault = project / "claude-memory-compiler"
uv = shutil.which("uv") or str(pathlib.Path.home() / ".local" / "bin" / "uv")

def hook(script, timeout):
    cmd = f"{uv} run --directory {vault} python {vault}/hooks/{script}"
    return {"matcher": "", "hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}

settings_path = project / ".claude" / "settings.json"
settings = {}
if settings_path.exists():
    try:
        settings = json.loads(settings_path.read_text() or "{}")
    except json.JSONDecodeError:
        settings = {}
settings.setdefault("hooks", {})
settings["hooks"]["SessionStart"] = [hook("session-start.py", 15)]
settings["hooks"]["PreCompact"]   = [hook("pre-compact.py", 10)]
settings["hooks"]["SessionEnd"]   = [hook("session-end.py", 10)]
settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, indent=2) + "\n")
```

> On Claude Desktop, `SessionEnd`/`PreCompact` may not fire — the `SessionStart`
> hook also drives a bounded `backfill --sweep`, so capture still happens. All
> three are wired for portability (CLI sessions do fire `SessionEnd`).

### Step 5 — Name the vault's Python project

```python
import pathlib, re
folder = pathlib.Path.cwd().name.lower()
name = re.sub(r'[^a-z0-9]+', '-', folder).strip('-') + '-kb'
toml_path = pathlib.Path('claude-memory-compiler/pyproject.toml')
content = toml_path.read_text()
content = re.sub(r'(?m)^name = ".*"', f'name = "{name}"', content)
toml_path.write_text(content)
```

### Step 6 — Ignore the vault in the host project (if it's a git repo)

The vault is its own git clone, so the host project should not track it. Append
non-destructively only if the host is a git repo:

```python
import pathlib
if pathlib.Path(".git").exists():
    gi = pathlib.Path(".gitignore")
    entry = "claude-memory-compiler/"
    existing = gi.read_text().splitlines() if gi.exists() else []
    if entry not in existing:
        prefix = (gi.read_text().rstrip() + "\n") if gi.exists() else ""
        gi.write_text(prefix + "\n# claude-memory-compiler vault (own repo + local content)\n" + entry + "\n")
```

### Step 7 — Initialize knowledge base index files

Create `claude-memory-compiler/knowledge/index.md`:
```markdown
# Knowledge Base Index

| Article | Summary | Compiled From | Updated |
|---------|---------|---------------|---------|
```

Create `claude-memory-compiler/knowledge/log.md`:
```markdown
# Build Log
```

### Step 8 — Install Python dependencies

```bash
export PATH="$HOME/.local/bin:$PATH" && uv sync --directory claude-memory-compiler
```

---

## Phase 3: Obsidian Vault Registration

The Obsidian vault is the `claude-memory-compiler/` directory (where `knowledge/`
and `daily/` live).

Read the current vault config:
```bash
cat ~/Library/Application\ Support/obsidian/obsidian.json
```

Generate values for the new entry:
```bash
echo "$(pwd)/claude-memory-compiler"                      # vault path
python3 -c "import time; print(int(time.time()*1000))"    # timestamp ms
openssl rand -hex 8                                        # new vault ID
```

Add the new entry under `vaults`, preserving all existing entries, and write the file back with the Write tool (never `echo`/`>>` for JSON):
```json
"<new-hex-id>": {
  "path": "<pwd>/claude-memory-compiler",
  "ts": <timestamp>,
  "open": false
}
```

---

## Phase 4: Verification

1. **Close and reopen Claude Code** in the project root — the SessionStart hook
   should inject a "Knowledge Base Catalog" block into the session.

2. **Confirm auth** (background capture depends on it):
   ```bash
   export PATH="$HOME/.local/bin:$PATH" && uv run --directory claude-memory-compiler python -c "from dotenv import load_dotenv; import os; load_dotenv('.env'); print('token present:', bool(os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')))"
   ```
   Expect `token present: True`.

3. **Open Obsidian** — the new vault appears in the switcher. Open it; Obsidian
   creates `.obsidian/` on first open.

4. **After a few sessions**, compile daily logs into articles:
   ```bash
   export PATH="$HOME/.local/bin:$PATH" && uv run --directory claude-memory-compiler python scripts/compile.py
   ```

5. **Health check** any time:
   ```bash
   export PATH="$HOME/.local/bin:$PATH" && uv run --directory claude-memory-compiler python scripts/lint.py --structural-only
   ```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| SessionStart hook doesn't fire | `uv` not on PATH at hook time | Hooks use an absolute `uv` path computed at setup; if uv moved, re-run Step 4 |
| `flush.py` logs `FLUSH_ERROR` in `claude-memory-compiler/scripts/flush.log` | Missing/invalid token | Confirm `claude-memory-compiler/.env` has a valid `CLAUDE_CODE_OAUTH_TOKEN`; regenerate with `claude setup-token` |
| Context injected twice / sweeps into the wrong vault | Both user-level AND project-level compiler hooks active | Remove the user-level hooks (see "Before you start") |
| Sweep captures nothing on Claude Desktop | Vault is upstream, not the fork | `git -C claude-memory-compiler remote -v` should be `yzhilova/...`; re-clone if not |
| `uv sync` fails with a Python version error | No 3.12+ installed | `export PATH="$HOME/.local/bin:$PATH" && uv python install 3.12` |
| Obsidian vault doesn't appear | `obsidian.json` invalid | Verify JSON; restart Obsidian |
| Hooks fire but daily log stays empty | Session too short | Normal — `MIN_TURNS_TO_FLUSH = 1` in `session-end.py` |
