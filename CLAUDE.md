# Ramblr (formerly gus-ears): project rules for Claude sessions

## Standing rule for this approved build (Fred, 2026-10-24)

Don't use AskUserQuestion. Decide within the spec yourself. If something is truly outside it
(deletes of non-project data, public, paid, secret values, system packages, extra sudo,
DNS/Cloudflare, force push), write `BLOCKED.md` in the project root with the exact question plus
options, then continue any other in-spec work. Only stop if nothing else is left.

## Fixed facts

- Transcripts are never deleted (no retention, pruning or cleanup; a SQLite trigger blocks DELETE on `segments`).
- Raw audio is kept for a rolling 24h, compressed (Opus or similar, ~100 MB/day), in ~/.local/share/ramblr/audio-24h (chmod 700, outside the repo, never backed up or synced, size cap), then auto-deleted. Transcripts, voiceprints and the DB are never deleted. First retention run is dry-run only; deletes turn on after the log shows only audio files older than 24h. The tmpfs spool is still used for live processing.
- Web binds 127.0.0.1:8798 only; tailnet exposure is the existing `tailscale serve` HTTPS :8798 entry. Never funnel, never 0.0.0.0, never touch other serve entries.
- Repo stays PRIVATE until Fred approves going public after a history scrub. No history rewrites or force pushes without his yes.
- Commit straight to main, no PRs. Python deps in the project `.venv` only, no system packages.
- The nixfred brand files in `assets/brand/` are locked: copied byte-for-byte, never edited.
- Tests and smoke checks NEVER touch the live DB (Fred, 2026-10-24 4:38 AM ET). Use a temp path or a `RAMBLR_DB` override (plus `RAMBLR_STATE`, `RAMBLR_SPOOL`, `RAMBLR_SOCK`, `RAMBLR_IDEAS_SOCK`, and a non-8798 `RAMBLR_PORT` for a second web). `tests/conftest.py` points every test at a tmp DB and fails if one reaches the live path, and `db.connect()` refuses the live DB while pytest runs. Uploads, ideas and corrections go to a test server, never the live one.
