"""
SessionStart hook - injects knowledge base context AND drives capture.

Two jobs, in order of importance:

1. Context injection (must never break): read knowledge/index.md and the
   most recent daily log, print them as additionalContext so Claude always
   "remembers" what it has learned.

2. Capture trigger (the reliability fix): the SessionEnd hook does NOT fire
   in the Claude Desktop app (hook/CLI parity gap, anthropics/claude-code
   #45514) — so the intended "flush on session end" never happens here and
   the knowledge base goes stale. SessionStart, by contrast, fires reliably
   on every session. So this hook also spawns a bounded, detached
   `backfill.py --sweep` that flushes the few most-recent *prior*
   un-flushed sessions (excluding the active one, whose transcript is still
   being written). It is capped (--max / --recent-days) so it can never
   blast the rate limit on a backlog; the large historical catch-up stays
   an explicit manual `backfill.py` run. The sweep is fire-and-forget and
   never delays context injection.

Configure in ~/.claude/settings.json (user-level, so it fires for ALL
sessions, not just ones opened in the compiler dir):
{
    "hooks": {
        "SessionStart": [{
            "matcher": "",
            "hooks": [{ "type": "command",
                        "command": "<uv> run --directory <root> python hooks/session-start.py",
                        "timeout": 15 }]
        }]
    }
}
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Recursion guard: if spawned by flush.py / backfill.py (Agent SDK -> claude
# subprocess sets CLAUDE_INVOKED_BY), do nothing — neither inject KB context
# into the extraction prompt nor spawn another sweep.
if os.environ.get("CLAUDE_INVOKED_BY"):
    sys.exit(0)

# Paths relative to project root
ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"
DAILY_DIR = ROOT / "daily"
INDEX_FILE = KNOWLEDGE_DIR / "index.md"
SCRIPTS_DIR = ROOT / "scripts"

MAX_CONTEXT_CHARS = 20_000
MAX_LOG_LINES = 30

# Bounded forward-drip: how many prior un-flushed sessions to capture per
# session start, and how far back to look. Small on purpose (rate limits).
SWEEP_MAX = 3
SWEEP_RECENT_DAYS = 2


def get_recent_log() -> str:
    """Read the most recent daily log (today or yesterday)."""
    today = datetime.now(timezone.utc).astimezone()

    for offset in range(2):
        date = today - timedelta(days=offset)
        log_path = DAILY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8").splitlines()
            # Return last N lines to keep context small
            recent = lines[-MAX_LOG_LINES:] if len(lines) > MAX_LOG_LINES else lines
            return "\n".join(recent)

    return "(no recent daily log)"


# Injection budgets. The hard ceiling is the smaller of this hook's cap and
# the harness's own per-injection load budget (~24 KB observed). Dumping the
# full annotated knowledge/index.md (now ~53 KB at 214+ articles) would blow
# straight past it and get TRUNCATED — which is exactly the failure that made
# the old auto-memory MEMORY.md useless. So we inject a COMPACT catalog
# (every article, descriptive slug, no verbose per-row summary) and keep the
# full annotated index.md on disk for query.py/compile.py and on-demand reads.
CATALOG_BUDGET = 15_000
DAILY_BUDGET = 3_000


def _fm_field(text: str, key: str) -> str:
    """Cheap single-line frontmatter read (no YAML dep)."""
    end = text.find("\n---", 4)
    head = text[:end] if end != -1 else text[:600]
    for line in head.splitlines():
        if line.startswith(key + ":"):
            return line[len(key) + 1:].strip().strip('"').strip()
    return ""


def _bucket(slug: str, tags: str) -> str:
    """Coarse domain bucket so the catalog is scannable, not one flat wall."""
    s = (slug + " " + tags).lower()
    for key, label in (
        ("feedback", "Feedback / working agreements"),
        ("process", "Process / workflow"),
        ("project", "Project / initiatives"),
        ("chunker", "Chunker"),
        ("scor", "Scoring / fidelity"),
        ("fidelity", "Scoring / fidelity"),
        ("button", "Buttons / CTAs"),
        ("css", "CSS / Tailwind"),
        ("tailwind", "CSS / Tailwind"),
        ("react-email", "React Email output"),
        ("width", "Layout / width"),
        ("column", "Layout / columns"),
        ("section", "Sections / containers"),
        ("container", "Sections / containers"),
        ("spacer", "Spacers / dividers"),
        ("img", "Images"),
        ("margin", "Box model"),
        ("padding", "Box model"),
    ):
        if key in s:
            return label
    return "Email parser — other"


def build_compact_catalog() -> str:
    """Every article as `[[concepts/slug]]`, grouped, within CATALOG_BUDGET.

    The descriptive-slug convention makes this a usable retrieval surface on
    its own; the agent reads any article (or the full annotated
    knowledge/index.md) on demand. Complete coverage is the priority — if the
    budget is ever hit, only the trailing groups are summarised as a count,
    never silently dropped.
    """
    concepts_dir = KNOWLEDGE_DIR / "concepts"
    connections_dir = KNOWLEDGE_DIR / "connections"

    groups: dict[str, list[str]] = {}
    total = 0
    for base, prefix in ((concepts_dir, "concepts"), (connections_dir, "connections")):
        if not base.exists():
            continue
        for md in sorted(base.glob("*.md")):
            slug = md.stem
            try:
                head = md.read_text(encoding="utf-8")[:600]
            except OSError:
                head = ""
            tags = _fm_field(head, "tags")
            groups.setdefault(_bucket(slug, tags), []).append(f"{prefix}/{slug}")
            total += 1

    lines = [
        f"{total} articles. This is the retrieval map — read any article in "
        "full with the Read tool, or the full annotated catalog at "
        "`knowledge/index.md`, when a topic is relevant.",
        "",
    ]
    used = 0
    for grp in sorted(groups):
        block = [f"### {grp}"] + [f"- [[{p}]]" for p in sorted(groups[grp])]
        chunk = "\n".join(block)
        if used + len(chunk) > CATALOG_BUDGET:
            remaining = sum(len(v) for v in groups.values()) - sum(
                1 for ln in lines if ln.startswith("- [[")
            )
            lines.append(
                f"\n_(catalog budget reached — {remaining}+ more articles on "
                "disk in `knowledge/concepts/`; full list in `knowledge/index.md`)_"
            )
            break
        lines.append(chunk)
        lines.append("")
        used += len(chunk) + 1
    return "\n".join(lines)


def build_context() -> str:
    """Assemble the context to inject. Engineered to land well under the
    harness load budget so it is NEVER truncated (the whole point)."""
    parts = []

    today = datetime.now(timezone.utc).astimezone()
    parts.append(f"## Today\n{today.strftime('%A, %B %d, %Y')}")

    if INDEX_FILE.exists():
        parts.append("## Knowledge Base Catalog\n\n" + build_compact_catalog())
    else:
        parts.append("## Knowledge Base Catalog\n\n(empty - nothing compiled yet)")

    # Recent daily log tail, capped (oldest content trimmed first so the
    # newest activity always survives).
    recent_log = get_recent_log()
    if len(recent_log) > DAILY_BUDGET:
        recent_log = "...(earlier entries trimmed)\n" + recent_log[-DAILY_BUDGET:]
    parts.append(f"## Recent Daily Log\n\n{recent_log}")

    context = "\n\n---\n\n".join(parts)

    # Safety net only — by construction we should be ~15-17 KB here. If this
    # ever fires, drop the daily log (keep the catalog) rather than cut blindly.
    if len(context) > MAX_CONTEXT_CHARS:
        context = "\n\n---\n\n".join(parts[:-1]) + "\n\n---\n\n## Recent Daily Log\n\n(omitted to stay within injection budget)"
        if len(context) > MAX_CONTEXT_CHARS:
            context = context[:MAX_CONTEXT_CHARS] + "\n\n...(truncated)"

    return context


def read_session_info() -> tuple[str, str]:
    """Best-effort: read the SessionStart hook's stdin JSON ONCE and return
    (session_id, project_dir). stdin can only be consumed once, so both fields
    come from a single read. Never raises.

    - session_id lets the sweep --exclude the still-being-written active
      session. Falls back to the CLAUDE_CODE_SESSION_ID env var.
    - project_dir is the `~/.claude/projects/<dir>` name for THIS session,
      taken from the hook's `transcript_path` (ground truth — it literally is
      that transcript file's parent dir). This scopes the sweep correctly no
      matter where the vault sits relative to the project root, instead of
      assuming a fixed nesting depth. "" when unavailable (caller defaults).
    """
    session_id = ""
    project_dir = ""
    try:
        raw = sys.stdin.read()
        if raw and raw.strip():
            data = json.loads(raw)
            sid = data.get("session_id")
            if isinstance(sid, str) and sid:
                session_id = sid
            tp = data.get("transcript_path")
            if isinstance(tp, str) and tp:
                # ~/.claude/projects/<project_dir>/<session_id>.jsonl
                project_dir = Path(tp).parent.name
    except Exception:
        pass
    if not session_id:
        session_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    return session_id, project_dir


def spawn_sweep(active_session_id: str, project_dir: str = "") -> None:
    """Fire-and-forget the bounded prior-session flush. Best-effort."""
    backfill = SCRIPTS_DIR / "backfill.py"
    if not backfill.exists():
        return
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    # Scope the sweep to THIS session's project. Without --project, backfill.py
    # globs every ~/.claude/projects/* dir and captures unrelated repos'
    # sessions into this vault. Prefer the project dir from the hook's
    # transcript_path (ground truth); fall back to deriving it from the vault
    # path, which assumes the vault is nested one level under the project root.
    project = project_dir or str(ROOT.parent).replace("/", "-")
    cmd = [
        uv, "run", "--directory", str(ROOT), "python", str(backfill),
        "--sweep",
        # `=` form: the project name starts with '-' (derived from an absolute
        # path), which argparse rejects as a value in the space-separated form.
        f"--project={project}",
        "--max", str(SWEEP_MAX),
        "--recent-days", str(SWEEP_RECENT_DAYS),
    ]
    if active_session_id:
        cmd += ["--exclude", active_session_id]
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach: survives this hook's exit
            cwd=str(ROOT),
        )
    except Exception:
        # Capture is best-effort; never let it break the session.
        pass


def main():
    # Read stdin FIRST (it carries session_id + transcript_path) before any
    # stdout write.
    active_session_id, project_dir = read_session_info()

    # Job 1 — context injection (critical path, must always happen).
    context = build_context()
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    print(json.dumps(output))
    sys.stdout.flush()

    # Job 2 — capture trigger (best-effort, detached, after the JSON is out).
    spawn_sweep(active_session_id, project_dir)


if __name__ == "__main__":
    main()
