# Release review evidence — v2.10.0 (`bb120471`) → `f0c9de69`

Archived 2026-08-31 from `/tmp`, where all of it was about to be reaped. This is the
**evidence**; the plan built on it is `docs/RELEASE_FIXES_2026-08-31.md`, and that is
what you want if you are here to fix something.

## What is here

- `jms-release-review.md` — the main review: 10 findings across playback, browser,
  client lifecycle and offline sync, plus the validation notes and stated limits.
- `jms-input-binding-review.md` — the supplement: 4 findings in mpvtk / HUD / input
  claims, plus the list of things it cleared.
- `probes/` — 15 reproduction scripts. Every finding in both documents has one.
- `logs/` — the small run logs. `LARGE_LOGS_SUMMARY.md` holds the result lines from the
  three ~1 MB raw logs, which were deliberately not committed.

Six further findings came from three pattern-hunting audits run against the same tree
and are **not** in these two documents — they are recorded in
`docs/RELEASE_FIXES_2026-08-31.md` as F11-F16 and F21-F26.

## Running a probe

They were written to be run in place and have not been rewritten, so that what is
archived is exactly what produced the reported output:

- **Paths are absolute** (`/home/izzie/bookmarks/scripts/jellyfin-mpv-shim`,
  `/home/izzie/.venv/bin/python`). Edit the `sys.path.insert` lines for another
  checkout.
- **`xvfb-run -a` is required**, as for the test suite — several import `player.py`,
  which opens a real mpv window at import time.
- Several add `tests/integration` to the path and use the repo's own fake-mpv harness.
- Credentials are synthetic throughout (`old-server:8096`,
  `not-a-real-network.invalid`, `cdn.example.invalid`). No real server address, token
  or account appears in any file here — checked before archiving.

## Provenance and limits

Findings were reproduced with controlled local probes, not against a live Jellyfin
server. The review states its own coverage limits at the end of
`jms-release-review.md`; the short version is that Windows/macOS runtime behaviour,
real installer and Flatpak builds, and exhaustive reader/Live TV behaviour were not
verified. Read that section before treating this as a certification.
