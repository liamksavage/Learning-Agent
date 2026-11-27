# Learning Agent

A teacher agent that runs the user's self-taught curriculum: a Claude Code scheduled task fires a fresh session here each morning, which serves the day's lesson, publishes the Today page, sends the push notification, and records the user's reply. Read `SPEC.md` for the design.

## Layout

- `SPEC.md` — the design and the decisions behind it.
- `agent/MORNING.md`, `agent/REPLY.md`, `agent/SEMINAR.md` — the procedures the scheduled session follows. If you are the morning session, start with `agent/MORNING.md`.
- `tools/la.py` — the deterministic core: which lesson is due, recording done/skip/defer, rendering the page. Run `python3 tools/la.py -h`.
- `curricula/<topic>.yaml` — the plan. Lessons are an ordered sequence, never dated. See the schema notes at the top of `curricula/philosophy.yaml`.
- `state/progress.yaml` — what has been served, done, skipped. Change it through `la.py`, not by hand, unless repairing.
- `state/today_page.yaml` — the stable URL of the Today page.
- `notes/<topic>/<date>.md`, `reconstructions/<topic>/unit-<id>/`, `seminars/<topic>/<date>.md` — the user's work.
- `tests/` — `python3 -m pytest -q`.

## Rules

- The branch that holds state is the repository's default branch. Pull before reading state; commit and push after every change to `state/`, `notes/`, `reconstructions/` or `seminars/`.
- Never mark a lesson done, skipped or deferred without the user saying so in the session. Confirm every state change in one line.
- The plan is a sequence: a missed day is done the next working day. Never skip content on the user's behalf, never double up.
- A `write` lesson waits until the readings it `requires` are done. If the user keeps hitting this, the remedy is to add a writing day, not to write from half-read material.
- Fetch only material that is publicly reachable without a login. Owned books are named by chapter.
- Never append lessons to a curriculum file without the user's approval of the draft in the session.
- Keep messages short. The user reads them on a phone before 7am.
