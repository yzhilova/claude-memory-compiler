# Skills

Claude Code skills that ship with this fork. They live here (version-controlled)
and are activated by installing them at the user level.

## `obsidian-kb-setup`

Bootstraps a fresh claude-memory-compiler vault in the current project: checks
prerequisites, clones **this fork**, wires Claude Code hooks (non-destructively),
seeds project-local `.env` auth, and registers the vault in Obsidian. macOS only.

### Install (user-level, available in every project)

Symlink it into `~/.claude/skills/` so it stays in sync with the repo:

```bash
mkdir -p ~/.claude/skills
ln -sfn "$(pwd)/skills/obsidian-kb-setup" ~/.claude/skills/obsidian-kb-setup
```

(Use `cp -r` instead of `ln -sfn` if you'd rather have a detached copy.)

Then in any new project, invoke `/obsidian-kb-setup`.

> **Heads up:** this skill sets up *per-project* vaults via project-level hooks.
> If you also run the compiler hooks at user level (`~/.claude/settings.json`),
> both fire on every session — remove one. See the skill's "Before you start".
