"""
Backfill past Claude Code transcripts into per-date daily logs.

One-shot recovery for the period when flushing was broken (no standalone
CLI auth) and/or hooks were project-scoped. Unlike flush.py (which always
appends to *today's* log), this routes each session's summary to the daily
log for the session's own date, derived from the transcript timestamps.

Safety:
  - Backs up the existing daily/ dir before writing.
  - Resumable: records processed session ids in backfill-state.json so an
    interrupted run does not re-spend on already-flushed sessions.
  - --dry-run prints the full plan (counts, dates, rough cost) with no API.

Usage:
  uv run python scripts/backfill.py --dry-run
  uv run python scripts/backfill.py                 # process all transcripts
  uv run python scripts/backfill.py --since 2026-04-29
  uv run python scripts/backfill.py --project -Users-yulianovozhilova-react-email
"""
from __future__ import annotations

# Recursion guard for the claude subprocess the SDK spawns (mirrors flush.py).
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_backfill"

import argparse
import asyncio
import fcntl
import json
import logging
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DAILY_DIR = ROOT / "daily"
STATE_FILE = SCRIPTS_DIR / "backfill-state.json"
LOCK_FILE = SCRIPTS_DIR / ".backfill.lock"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

MAX_TURNS = 30
MAX_CONTEXT_CHARS = 15_000
MIN_TURNS = 2

# Reuse flush.py's Agent SDK extraction (identical prompt/options).
sys.path.insert(0, str(SCRIPTS_DIR))
from flush import run_flush  # noqa: E402


def extract_context_and_date(transcript_path: Path) -> tuple[str, int, str]:
    """Return (context_markdown, turn_count, YYYY-MM-DD) for a transcript."""
    turns: list[str] = []
    max_ts: datetime | None = None

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts_raw = entry.get("timestamp")
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    if max_ts is None or ts > max_ts:
                        max_ts = ts
                except ValueError:
                    pass

            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                role = entry.get("role", "")
                content = entry.get("content", "")

            if role not in ("user", "assistant"):
                continue

            if isinstance(content, list):
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        parts.append(block)
                content = "\n".join(parts)

            if isinstance(content, str) and content.strip():
                label = "User" if role == "user" else "Assistant"
                turns.append(f"**{label}:** {content.strip()}\n")

    recent = turns[-MAX_TURNS:]
    context = "\n".join(recent)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1:]

    if max_ts is not None:
        date_str = max_ts.astimezone().strftime("%Y-%m-%d")
    else:
        date_str = datetime.fromtimestamp(
            transcript_path.stat().st_mtime, tz=timezone.utc
        ).astimezone().strftime("%Y-%m-%d")

    return context, len(recent), date_str


def append_dated(date_str: str, content: str, when: str) -> None:
    """Append a section to daily/<date_str>.md (creating it with a header)."""
    log_path = DAILY_DIR / f"{date_str}.md"
    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {date_str}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"### {when}\n\n{content}\n\n")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"processed": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def collect_transcripts(project: str | None) -> list[Path]:
    if not PROJECTS_DIR.exists():
        return []
    if project:
        dirs = [PROJECTS_DIR / project]
    else:
        dirs = [d for d in PROJECTS_DIR.iterdir() if d.is_dir()]
    out: list[Path] = []
    for d in dirs:
        out.extend(sorted(d.glob("*.jsonl")))
    return out


def acquire_lock(blocking: bool):
    """Serialize backfill runs via an exclusive flock so they can't race on
    backfill-state.json. The sweep is spawned detached on every session start;
    without this, overlapping sweeps each read state before any has persisted
    its progress, judge the same session un-processed, and append duplicate
    blocks (the cause of the repeated '### Backfill <id>' clusters). Returns the
    held fd — keep it referenced for the process lifetime; the OS releases it on
    exit — or None if a non-blocking attempt found another run already holding it."""
    fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        return None
    return fd


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--since", help="YYYY-MM-DD lower bound on session date")
    ap.add_argument("--project", help="single ~/.claude/projects subdir name")
    ap.add_argument("--all", action="store_true",
                    help="ignore resume state; reprocess everything")
    ap.add_argument("--sweep", action="store_true",
                    help="forward drip for the SessionStart hook: process at most "
                         "--max most-recent un-flushed sessions within --recent-days, "
                         "skipping --exclude (the active session). Append-only, "
                         "no daily/ backup (transcripts are immutable / re-runnable). "
                         "Bounded so it never blasts the rate limit on a backlog; "
                         "the large historical catch-up stays an explicit manual run.")
    ap.add_argument("--max", type=int, default=3,
                    help="[--sweep] cap sessions processed per run (default 3)")
    ap.add_argument("--recent-days", type=int, default=2,
                    help="[--sweep] only consider sessions dated within N days "
                         "(default 2); older un-flushed sessions are left for an "
                         "explicit `backfill.py` run")
    ap.add_argument("--exclude", default="",
                    help="session id to skip (the currently-active session, whose "
                         "transcript is still being written)")
    args = ap.parse_args()

    # A sweep that loses the race just exits (the holder covers recent sessions);
    # a manual run blocks until any sweep finishes so it never bails silently.
    lock_fd = acquire_lock(blocking=not args.sweep)
    if lock_fd is None:
        logging.info("[sweep] another backfill holds the lock; skipping")
        return

    transcripts = collect_transcripts(args.project)
    state = {"processed": []} if args.all else load_state()
    done = set(state.get("processed", []))

    plan: list[tuple[Path, str, int, int]] = []  # path, date, turns, chars
    skipped_done = skipped_small = skipped_since = 0

    for tp in transcripts:
        sid = tp.stem
        if args.exclude and sid == args.exclude:
            continue
        if sid in done:
            skipped_done += 1
            continue
        try:
            ctx, turns, date_str = extract_context_and_date(tp)
        except Exception as e:
            print(f"  ! skip {sid}: extract failed: {e}")
            continue
        if turns < MIN_TURNS or not ctx.strip():
            skipped_small += 1
            continue
        if args.since and date_str < args.since:
            skipped_since += 1
            continue
        plan.append((tp, date_str, turns, len(ctx)))

    plan.sort(key=lambda r: r[1])  # oldest date first

    if args.sweep:
        cutoff = (datetime.now().astimezone()
                  - timedelta(days=args.recent_days)).strftime("%Y-%m-%d")
        in_window = [r for r in plan if r[1] >= cutoff]
        older = len(plan) - len(in_window)
        # take the most-recent --max, then restore oldest-first so the daily
        # notes get appended in chronological order
        plan = sorted(in_window, key=lambda r: r[1], reverse=True)[: args.max]
        plan.sort(key=lambda r: r[1])
        logging.info(
            "[sweep] candidates=%d in_window=%d older_deferred=%d "
            "selected=%d (max=%d recent_days=%d exclude=%s)",
            len(transcripts), len(in_window), older, len(plan),
            args.max, args.recent_days, args.exclude or "-",
        )

    total_chars = sum(r[3] for r in plan)
    by_date: dict[str, int] = {}
    for _, d, _, _ in plan:
        by_date[d] = by_date.get(d, 0) + 1

    print(f"Transcripts found:        {len(transcripts)}")
    print(f"Already processed (skip): {skipped_done}")
    print(f"Too few turns (skip):     {skipped_small}")
    print(f"Before --since (skip):    {skipped_since}")
    print(f"To backfill:              {len(plan)} sessions "
          f"across {len(by_date)} dates")
    print(f"Total context chars:      {total_chars:,} "
          f"(~{total_chars // 4:,} input tokens, very rough)")
    print("Per-date counts:")
    for d in sorted(by_date):
        print(f"  {d}: {by_date[d]}")

    if args.dry_run:
        print("\n[dry-run] no API calls, no writes.")
        return

    if not plan:
        print("\nNothing to backfill.")
        return

    # Back up daily/ once before the first real write. Skipped for --sweep:
    # the sweep is append-only and bounded, runs on every session start, and
    # would otherwise litter the repo with a daily.bak-* dir each time. The
    # data source (JSONL transcripts) is immutable and the run is resumable.
    if not args.sweep:
        backup = DAILY_DIR.parent / f"daily.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.copytree(DAILY_DIR, backup)
        print(f"\nBacked up daily/ -> {backup}")

    ok = err = 0
    for i, (tp, date_str, turns, _) in enumerate(plan, 1):
        sid = tp.stem
        print(f"[{i}/{len(plan)}] {date_str} {sid} ({turns} turns) ...", flush=True)
        ctx, _, _ = extract_context_and_date(tp)
        try:
            result = await run_flush(ctx)
        except Exception as e:
            result = f"FLUSH_ERROR: {type(e).__name__}: {e}"

        when = f"Backfill {sid[:8]}"
        if "FLUSH_OK" in result:
            print("    FLUSH_OK (nothing worth saving)")
        elif "FLUSH_ERROR" in result:
            err += 1
            print(f"    ERROR: {result.splitlines()[0]}")
            append_dated(date_str, result, when + " — ERROR")
        else:
            ok += 1
            append_dated(date_str, result, when)
            print(f"    saved ({len(result)} chars) -> daily/{date_str}.md")

        done.add(sid)
        state["processed"] = sorted(done)
        save_state(state)  # persist after every session (resumable)
        if args.sweep:
            logging.info(
                "[sweep] %s %s -> %s",
                date_str, sid[:8],
                "FLUSH_OK" if "FLUSH_OK" in result
                else ("ERROR" if "FLUSH_ERROR" in result
                      else f"saved {len(result)}c daily/{date_str}.md"),
            )

    if args.sweep:
        logging.info("[sweep] done saved=%d error=%d total=%d", ok, err, len(plan))
    print(f"\nDone. saved={ok} error={err} "
          f"total={len(plan)}. State: {STATE_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
