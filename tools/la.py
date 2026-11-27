#!/usr/bin/env python3
"""la — the Learning Agent's deterministic core.

The Claude session that runs each morning is the teacher; this script is its
record-keeper. It decides which lesson is due, records what you did, and
renders the Today page. Everything it knows lives in plain files in the repo:

    curricula/<topic>.yaml   the plan (ordered lessons, never dated)
    state/progress.yaml      what has been served, done, skipped
    notes/<topic>/<date>.md  your daily notes
    state/today_page.yaml    the stable URL of the Today page

Usage (run from the repo root):

    python3 tools/la.py today  [--date YYYY-MM-DD]        what is due (read-only)
    python3 tools/la.py serve  [--date YYYY-MM-DD]        same, and record that it was served
    python3 tools/la.py done   LESSON [--date D] [--note TEXT | --note-file F]
    python3 tools/la.py skip   LESSON [--date D] --reason TEXT
    python3 tools/la.py defer  LESSON [--date D]          "not today": serve it again next time
    python3 tools/la.py feedback --unit U --worked .. --dragged .. --hour .. --change ..
    python3 tools/la.py seminar LESSON --summary-file F [--date D]
    python3 tools/la.py status
    python3 tools/la.py render [--date D] [--out build/today.html]
    python3 tools/la.py set-page-url URL
    python3 tools/la.py validate

All commands accept --root to point at another repo checkout (used by tests).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
from pathlib import Path

import yaml

PASSED = {"done", "skipped"}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DRAFT_AHEAD_THRESHOLD = 10  # remaining weekday lessons before we ask to draft the next unit


# ──────────────────────────────────────────────────────────────────────────────
# Loading and saving
# ──────────────────────────────────────────────────────────────────────────────

class Repo:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.curricula_dir = self.root / "curricula"
        self.progress_path = self.root / "state" / "progress.yaml"
        self.page_path = self.root / "state" / "today_page.yaml"

    def topics(self) -> list[str]:
        return sorted(p.stem for p in self.curricula_dir.glob("*.yaml"))

    def curriculum(self, topic: str) -> dict:
        with open(self.curricula_dir / f"{topic}.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def progress(self) -> dict:
        if not self.progress_path.exists():
            return {}
        with open(self.progress_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def save_progress(self, progress: dict) -> None:
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Progress state. Written by tools/la.py; safe to edit by hand.\n"
            "# Lesson statuses: done | skipped | pending (served, no reply yet)\n"
        )
        body = yaml.safe_dump(progress, sort_keys=False, allow_unicode=True, width=100)
        self.progress_path.write_text(header + body, encoding="utf-8")

    def topic_progress(self, progress: dict, topic: str) -> dict:
        tp = progress.setdefault(topic, {})
        tp.setdefault("lessons", {})
        tp.setdefault("served", [])
        tp.setdefault("feedback", [])
        tp.setdefault("seminars", [])
        return tp

    def page_url(self) -> str | None:
        if not self.page_path.exists():
            return None
        with open(self.page_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("url")

    def set_page_url(self, url: str) -> None:
        self.page_path.write_text(
            "# The stable \"Today\" Claude page. The morning run republishes to this URL.\n"
            + yaml.safe_dump({"url": url}),
            encoding="utf-8",
        )

    def notes_dir(self, topic: str) -> Path:
        return self.root / "notes" / topic


# ──────────────────────────────────────────────────────────────────────────────
# Plan navigation
# ──────────────────────────────────────────────────────────────────────────────

def flatten(curriculum: dict) -> list[dict]:
    """All lessons in plan order, each annotated with its unit and phase."""
    out = []
    for phase in curriculum.get("phases", []):
        for unit in phase.get("units", []):
            for i, lesson in enumerate(unit.get("lessons", []) or []):
                out.append({**lesson, "_unit": unit, "_phase": phase, "_index": i})
    return out


def first_undrafted_unit(curriculum: dict) -> dict | None:
    for phase in curriculum.get("phases", []):
        for unit in phase.get("units", []):
            if unit.get("status") == "undrafted" or not unit.get("lessons"):
                return {"id": unit["id"], "title": unit.get("title"), "phase": phase["id"]}
    return None


def status_of(tp: dict, lesson_id: str) -> str | None:
    rec = tp["lessons"].get(lesson_id)
    return rec.get("status") if rec else None


def is_passed(tp: dict, lesson_id: str) -> bool:
    return status_of(tp, lesson_id) in PASSED


def mode_for(date: dt.date, curriculum: dict) -> str:
    week = curriculum.get("week", {})
    wd = WEEKDAYS[date.weekday()]
    if wd == week.get("rest_day", "sat"):
        return "rest"
    if wd == week.get("seminar_day", "sun"):
        return "seminar"
    return "work"


def pick(curriculum: dict, tp: dict, mode: str) -> dict | None:
    """The first lesson in plan order that is due for this mode and not yet passed."""
    for lesson in flatten(curriculum):
        is_sem = lesson.get("kind") == "seminar"
        if mode == "seminar" and not is_sem:
            continue
        if mode == "work" and is_sem:
            continue
        if not is_passed(tp, lesson["id"]):
            return lesson
    return None


def remaining_work_lessons(curriculum: dict, tp: dict) -> int:
    return sum(
        1 for l in flatten(curriculum)
        if l.get("kind") != "seminar" and not is_passed(tp, l["id"])
    )


def latest_note(repo: Repo, topic: str, before: dt.date) -> dict | None:
    d = repo.notes_dir(topic)
    if not d.exists():
        return None
    candidates = []
    for p in d.glob("*.md"):
        try:
            nd = dt.date.fromisoformat(p.stem[:10])
        except ValueError:
            continue
        if nd < before:
            candidates.append((nd, p))
    if not candidates:
        return None
    nd, p = max(candidates)
    return {"date": nd.isoformat(), "path": str(p.relative_to(repo.root)), "text": p.read_text(encoding="utf-8")}


def public(lesson: dict) -> dict:
    """A lesson without its private annotations."""
    return {k: v for k, v in lesson.items() if not k.startswith("_")}


def unit_view(unit: dict, tp: dict) -> list[dict]:
    rows = []
    for l in unit.get("lessons", []) or []:
        rows.append({
            "id": l["id"],
            "kind": l.get("kind"),
            "title": l.get("title"),
            "status": status_of(tp, l["id"]) or "upcoming",
        })
    return rows


def plan_for(repo: Repo, topic: str, date: dt.date, progress: dict) -> dict:
    cur = repo.curriculum(topic)
    tp = repo.topic_progress(progress, topic)
    mode = mode_for(date, cur)
    out: dict = {
        "topic": topic,
        "title": cur.get("title", topic),
        "mode": mode,
        "hour": cur.get("hour", {}),
        "week_shape": cur.get("week", {}),
        "primer": bool(cur.get("page", {}).get("primer", False)),
    }

    # Yesterday: what was last served, and was it settled?
    served = tp["served"]
    last = None
    for entry in reversed(served):
        if entry["date"] < date.isoformat():
            last = entry
            break
    if last:
        st = status_of(tp, last["lesson"])
        out["last_served"] = {**last, "status": st or "unrecorded", "settled": st in PASSED}
    else:
        out["last_served"] = None
    out["yesterday_note"] = latest_note(repo, topic, date)

    if mode == "rest":
        out["lesson"] = None
        return out

    lesson = pick(cur, tp, mode)
    if lesson is None:
        out["lesson"] = None
        out["exhausted"] = True
        out["next_undrafted_unit"] = first_undrafted_unit(cur)
        return out

    unit, phase = lesson["_unit"], lesson["_phase"]
    reqs = lesson.get("requires", []) or []
    out["lesson"] = public(lesson)
    out["lesson"]["requires_met"] = all(is_passed(tp, r) for r in reqs)
    out["lesson"]["unmet_requires"] = [r for r in reqs if not is_passed(tp, r)]
    out["lesson"]["position"] = {
        "index": lesson["_index"] + 1,
        "of": len(unit.get("lessons", [])),
    }
    out["unit"] = {
        "id": unit["id"],
        "title": unit.get("title"),
        "goal": unit.get("goal"),
        "have_ready": unit.get("have_ready", []),
        "audio": unit.get("audio", []),
        "lessons": unit_view(unit, tp),
    }
    out["phase"] = {"id": phase["id"], "title": phase.get("title")}
    remaining = remaining_work_lessons(cur, tp)
    out["remaining_work_lessons"] = remaining
    out["draft_ahead"] = remaining <= DRAFT_AHEAD_THRESHOLD
    out["next_undrafted_unit"] = first_undrafted_unit(cur)
    out["feedback_due_for_unit"] = (
        lesson.get("kind") == "seminar"
        and not any(f.get("unit") == unit["id"] for f in tp["feedback"])
    )
    return out


def today(repo: Repo, date: dt.date, topics: list[str] | None = None, record: bool = False) -> dict:
    progress = repo.progress()
    result = {
        "date": date.isoformat(),
        "weekday": date.strftime("%A"),
        "page_url": repo.page_url(),
        "topics": [],
    }
    for topic in topics or repo.topics():
        plan = plan_for(repo, topic, date, progress)
        if record and plan.get("lesson"):
            tp = repo.topic_progress(progress, topic)
            lid = plan["lesson"]["id"]
            if not any(s["date"] == date.isoformat() and s["lesson"] == lid for s in tp["served"]):
                tp["served"].append({"date": date.isoformat(), "lesson": lid})
            tp["lessons"].setdefault(lid, {"status": "pending", "date": date.isoformat()})
        result["topics"].append(plan)
    if record:
        repo.save_progress(progress)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Recording what you did
# ──────────────────────────────────────────────────────────────────────────────

def find_lesson(repo: Repo, lesson_id: str) -> tuple[str, dict]:
    for topic in repo.topics():
        for l in flatten(repo.curriculum(topic)):
            if l["id"] == lesson_id:
                return topic, l
    raise SystemExit(f"error: no lesson with id {lesson_id!r} in any curriculum")


def mark_done(repo: Repo, lesson_id: str, date: dt.date, note: str | None) -> dict:
    topic, lesson = find_lesson(repo, lesson_id)
    progress = repo.progress()
    tp = repo.topic_progress(progress, topic)
    rec = {"status": "done", "date": date.isoformat()}
    if note and note.strip():
        notes_dir = repo.notes_dir(topic)
        notes_dir.mkdir(parents=True, exist_ok=True)
        path = notes_dir / f"{date.isoformat()}.md"
        body = (
            f"# {date.isoformat()} — {lesson.get('title', lesson_id)}\n\n"
            f"Lesson: {lesson_id}\n\n{note.strip()}\n"
        )
        path.write_text(body, encoding="utf-8")
        rec["note"] = str(path.relative_to(repo.root))
    tp["lessons"][lesson_id] = rec
    repo.save_progress(progress)
    return {"lesson": lesson_id, **rec}


def mark_skipped(repo: Repo, lesson_id: str, date: dt.date, reason: str) -> dict:
    topic, _ = find_lesson(repo, lesson_id)
    progress = repo.progress()
    tp = repo.topic_progress(progress, topic)
    rec = {"status": "skipped", "date": date.isoformat(), "reason": reason}
    tp["lessons"][lesson_id] = rec
    repo.save_progress(progress)
    return {"lesson": lesson_id, **rec}


def mark_deferred(repo: Repo, lesson_id: str, date: dt.date) -> dict:
    topic, _ = find_lesson(repo, lesson_id)
    progress = repo.progress()
    tp = repo.topic_progress(progress, topic)
    rec = {"status": "pending", "date": date.isoformat(), "deferred": True}
    tp["lessons"][lesson_id] = rec
    repo.save_progress(progress)
    return {"lesson": lesson_id, **rec}


def add_feedback(repo: Repo, topic: str, row: dict) -> dict:
    progress = repo.progress()
    tp = repo.topic_progress(progress, topic)
    tp["feedback"] = [f for f in tp["feedback"] if f.get("unit") != row["unit"]] + [row]
    repo.save_progress(progress)
    return row


def record_seminar(repo: Repo, lesson_id: str, date: dt.date, summary_file: str) -> dict:
    topic, _ = find_lesson(repo, lesson_id)
    progress = repo.progress()
    tp = repo.topic_progress(progress, topic)
    tp["lessons"][lesson_id] = {"status": "done", "date": date.isoformat(), "summary": summary_file}
    tp["seminars"].append({"date": date.isoformat(), "lesson": lesson_id, "summary_file": summary_file})
    repo.save_progress(progress)
    return tp["seminars"][-1]


def status_report(repo: Repo) -> dict:
    progress = repo.progress()
    out = {}
    for topic in repo.topics():
        cur = repo.curriculum(topic)
        tp = repo.topic_progress(progress, topic)
        lessons = flatten(cur)
        counts = {"done": 0, "skipped": 0, "pending": 0, "upcoming": 0}
        for l in lessons:
            counts[status_of(tp, l["id"]) or "upcoming"] += 1
        out[topic] = {
            "title": cur.get("title"),
            "drafted_lessons": len(lessons),
            **counts,
            "next_work_lesson": (pick(cur, tp, "work") or {}).get("id"),
            "next_seminar": (pick(cur, tp, "seminar") or {}).get("id"),
            "next_undrafted_unit": first_undrafted_unit(cur),
            "feedback_rows": len(tp["feedback"]),
            "seminars_held": len(tp["seminars"]),
        }
    return out


def validate(repo: Repo) -> list[str]:
    problems = []
    for topic in repo.topics():
        cur = repo.curriculum(topic)
        seen = set()
        lessons = flatten(cur)
        ids = [l["id"] for l in lessons]
        for l in lessons:
            if l["id"] in seen:
                problems.append(f"{topic}: duplicate lesson id {l['id']}")
            seen.add(l["id"])
            if l.get("kind") not in {"read", "listen", "exercise", "write", "seminar"}:
                problems.append(f"{topic}: {l['id']} has unknown kind {l.get('kind')!r}")
            for r in l.get("requires", []) or []:
                if r not in ids:
                    problems.append(f"{topic}: {l['id']} requires unknown lesson {r}")
                elif ids.index(r) > ids.index(l["id"]):
                    problems.append(f"{topic}: {l['id']} requires {r}, which comes later in the plan")
            for s in l.get("sources", []) or []:
                if s.get("kind") not in {"free-online", "owned-book", "paid-online", "audio", "video"}:
                    problems.append(f"{topic}: {l['id']} source has unknown kind {s.get('kind')!r}")
                if s.get("kind") in {"free-online", "paid-online", "audio", "video"} and not s.get("url"):
                    problems.append(f"{topic}: {l['id']} source {s.get('title')!r} needs a url")
        tp = repo.topic_progress(repo.progress(), topic)
        for lid in tp["lessons"]:
            if lid not in ids:
                problems.append(f"{topic}: progress records unknown lesson {lid}")
    return problems


# ──────────────────────────────────────────────────────────────────────────────
# Rendering the Today page
# ──────────────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _para(text: str | None) -> str:
    if not text:
        return ""
    return "".join(f"<p>{_e(p.strip())}</p>" for p in str(text).strip().split("\n\n") if p.strip())


KIND_LABEL = {"read": "Read", "listen": "Listen", "exercise": "Exercises", "write": "Write", "seminar": "Seminar"}


def _source_html(s: dict) -> str:
    kind = s.get("kind")
    title = _e(s.get("title"))
    if kind == "owned-book":
        loc = f" <span class='loc'>{_e(s.get('locator'))}</span>" if s.get("locator") else ""
        return f"<li class='src book'><span class='tag'>Your copy</span> {title}{loc}</li>"
    tag = {"free-online": "Online", "paid-online": "Paid", "audio": "Audio", "video": "Video"}.get(kind, "Link")
    link = f"<a href='{_e(s.get('url'))}' target='_blank' rel='noopener'>{title}</a>" if s.get("url") else title
    extract = s.get("extract")
    ex = ""
    if isinstance(extract, dict):
        ex = " <span class='loc'>" + _e(", ".join(f"{k.capitalize()} {v}" for k, v in extract.items())) + "</span>"
    return f"<li class='src'><span class='tag'>{tag}</span> {link}{ex}</li>"


def render_topic(plan: dict, date: dt.date) -> str:
    mode = plan["mode"]
    parts = [f"<section class='topic'><h2 class='topic-title'>{_e(plan['title'])}</h2>"]

    if mode == "rest":
        parts.append("<div class='card rest'><p class='big'>Rest day.</p><p>No reading. The plan picks up tomorrow.</p></div></section>")
        return "".join(parts)

    if plan.get("exhausted"):
        nxt = plan.get("next_undrafted_unit") or {}
        parts.append(
            "<div class='card warn'><p class='big'>The detailed plan has run out.</p>"
            f"<p>Next up is <strong>{_e(nxt.get('title'))}</strong>, which hasn't been drafted day by day yet. "
            "Reply in the session and we'll draft it together.</p></div></section>"
        )
        return "".join(parts)

    L = plan["lesson"]
    U = plan["unit"]
    pos = L.get("position", {})
    kind = L.get("kind")
    hour = plan.get("hour", {})

    # Header line
    parts.append(
        f"<p class='eyebrow'>{_e(U['title'])} · {KIND_LABEL.get(kind, kind)} · lesson {pos.get('index')} of {pos.get('of')}</p>"
        f"<h1 class='lesson-title'>{_e(L.get('title'))}</h1>"
    )

    # Unsettled yesterday
    ls = plan.get("last_served")
    if ls and not ls.get("settled"):
        parts.append(
            "<div class='card nudge'><p><strong>Yesterday isn't recorded.</strong> "
            f"You were given <em>{_e(ls['lesson'])}</em> on {_e(ls['date'])} and I have no reply. "
            "If you did it, reply <code>done</code> (and your note) in the session and I'll mark it. "
            "Otherwise today's lesson stands.</p></div>"
        )

    # Hour
    if hour and kind != "seminar":
        r, w, n = hour.get("reenter_min", 10), hour.get("work_min", 40), hour.get("write_min", 10)
        tot = r + w + n
        parts.append(
            "<div class='hour' aria-label='The hour'>"
            f"<span style='flex:{r}' title='Re-enter'>{r}′ re-enter</span>"
            f"<span style='flex:{w}' title='Work'>{w}′ {('write' if kind == 'write' else 'work')}</span>"
            f"<span style='flex:{n}' title='Note'>{n}′ note</span>"
            f"</div><p class='muted small'>{tot} minutes. Reread yesterday's note first; the lesson is the middle block; the note closes it.</p>"
        )

    # Task
    parts.append(f"<div class='card task'><h3>Today</h3>{_para(L.get('task'))}")
    if kind in {"write", "seminar"} and not L.get("requires_met", True):
        parts.append(
            "<p class='warn-inline'>Some of the reading this depends on isn't recorded as done: "
            + _e(", ".join(L.get("unmet_requires", []))) + ". Do what you can and say so in your reply.</p>"
        )
    srcs = L.get("sources") or []
    if srcs:
        parts.append("<h4>Material</h4><ul class='sources'>" + "".join(_source_html(s) for s in srcs) + "</ul>")
    if L.get("excerpt"):
        parts.append(f"<div class='excerpt'>{L['excerpt']}</div>")  # stage 2 fills this with cut pages
    if L.get("note_prompt"):
        parts.append(f"<h4>Your note tonight</h4><p class='prompt'>{_e(L['note_prompt'])}</p>")
    parts.append("</div>")

    # Yesterday's note
    yn = plan.get("yesterday_note")
    if yn:
        text = yn["text"].split("\n\n", 2)[-1] if yn["text"].startswith("#") else yn["text"]
        parts.append(f"<div class='card note'><h3>Re-enter: your note from {_e(yn['date'])}</h3>{_para(text)}</div>")
    elif kind != "seminar":
        parts.append("<div class='card note muted'><h3>Re-enter</h3><p>No note on file yet. Reread the last paragraph you read instead.</p></div>")

    # Unit goal + week list
    parts.append(f"<div class='card week'><h3>{_e(U['title'])}</h3>")
    if U.get("goal"):
        parts.append(f"<p class='goal'>{_e(U['goal'])}</p>")
    parts.append("<ol class='lessons'>")
    for row in U.get("lessons", []):
        cls = row["status"]
        if row["id"] == L["id"]:
            cls += " current"
        parts.append(
            f"<li class='{cls}'><span class='dot' aria-hidden='true'></span>"
            f"<span class='k'>{KIND_LABEL.get(row['kind'], row['kind'])}</span> {_e(row['title'])}"
            f"<span class='st'>{_e(row['status'] if row['id'] != L['id'] else 'today')}</span></li>"
        )
    parts.append("</ol>")
    if U.get("have_ready"):
        parts.append("<h4>Have ready this unit</h4><ul class='sources'>" + "".join(_source_html(s) for s in U["have_ready"]) + "</ul>")
    parts.append("</div>")

    # Audio
    if U.get("audio"):
        parts.append("<div class='card audio'><h3>Audio, outside the hour</h3><ul class='sources'>")
        for a in U["audio"]:
            link = f"<a href='{_e(a.get('url'))}' target='_blank' rel='noopener'>{_e(a.get('title'))}</a>" if a.get("url") else _e(a.get("title"))
            note = f" <span class='loc'>{_e(a.get('note'))}</span>" if a.get("note") else ""
            parts.append(f"<li class='src'><span class='tag'>Listen</span> {link}{note}</li>")
        parts.append("</ul></div>")

    if plan.get("draft_ahead") and plan.get("next_undrafted_unit"):
        nxt = plan["next_undrafted_unit"]
        parts.append(
            f"<p class='muted small'>About {plan['remaining_work_lessons']} lessons of detailed plan remain. "
            f"Next to draft: {_e(nxt.get('title'))}. I'll raise it on Sunday.</p>"
        )
    parts.append("</section>")
    return "".join(parts)


CSS = """
/* Layout: one column of cards, reading-first; the lesson is the hero, the week is the map. */
:root{
  --bg:#f4f5f1; --surface:#ffffff; --fg:#1d2420; --muted:#5c6660; --line:#dfe3dc;
  --accent:#2d5f4e; --accent-ink:#ffffff; --soft:#e7efe9; --warn:#8a5a14; --warn-bg:#fbf1dd;
  --done:#2d5f4e; --skipped:#9aa39d; --pending:#b7791f;
  --font-ui:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
  --font-read:'Literata',Georgia,'Times New Roman',serif;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#141917; --surface:#1d2320; --fg:#e8ebe6; --muted:#9aa69f; --line:#2c3531;
  --accent:#7fc3a6; --accent-ink:#0f1a15; --soft:#22312a; --warn:#e3b56a; --warn-bg:#2d2616;
  --done:#7fc3a6; --skipped:#6b756f; --pending:#d9a441; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#141917; --surface:#1d2320; --fg:#e8ebe6; --muted:#9aa69f; --line:#2c3531;
  --accent:#7fc3a6; --accent-ink:#0f1a15; --soft:#22312a; --warn:#e3b56a; --warn-bg:#2d2616;
  --done:#7fc3a6; --skipped:#6b756f; --pending:#d9a441; color-scheme:dark}
body{background:var(--bg);color:var(--fg);font-family:var(--font-ui);line-height:1.5;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:1.25rem 3rem}
.wrap{max-width:40rem;margin:0 auto;display:flex;flex-direction:column;gap:1.25rem}
header.top{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:.75rem}
header.top .date{font-weight:600;letter-spacing:.01em}
header.top .brand{color:var(--muted);font-size:.85rem;text-transform:uppercase;letter-spacing:.08em}
.topic{display:flex;flex-direction:column;gap:1rem}
.topic-title{font-size:.8rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin:0;font-weight:600}
.eyebrow{margin:0;color:var(--accent);font-weight:600;font-size:.9rem}
.lesson-title{font-family:var(--font-read);font-weight:600;font-size:clamp(1.6rem,5vw,2.2rem);line-height:1.15;margin:0;text-wrap:balance}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:1rem 1.1rem;min-width:0}
.card h3{margin:0 0 .5rem;font-size:1rem;font-weight:600}
.card h4{margin:1rem 0 .35rem;font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}
.card.task{border-color:var(--accent)}
.card.task p{font-family:var(--font-read);font-size:1.08rem;line-height:1.55;margin:.4rem 0}
.card.note p{font-family:var(--font-read);font-size:1.02rem;margin:.4rem 0}
.card.nudge{background:var(--warn-bg);border-color:transparent;color:var(--fg)}
.card.warn{background:var(--warn-bg);border-color:transparent}
.card.rest .big,.card.warn .big{font-family:var(--font-read);font-size:1.4rem;margin:0 0 .3rem}
.warn-inline{color:var(--warn);font-size:.95rem !important;font-family:var(--font-ui) !important}
.prompt{font-style:italic}
.hour{display:flex;gap:3px;height:2rem;border-radius:6px;overflow:hidden;font-size:.75rem;font-weight:600}
.hour span{display:flex;align-items:center;justify-content:center;background:var(--soft);color:var(--fg);white-space:nowrap;overflow:hidden;padding:0 .25rem}
.hour span:nth-child(2){background:var(--accent);color:var(--accent-ink)}
.muted{color:var(--muted)} .small{font-size:.85rem;margin:.25rem 0 0}
.goal{color:var(--muted);margin:0 0 .75rem;font-size:.95rem}
ul.sources{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.4rem}
li.src{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap;font-size:.95rem}
.tag{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;background:var(--soft);color:var(--fg);padding:.1rem .45rem;border-radius:999px;font-weight:600;white-space:nowrap}
li.book .tag{background:transparent;border:1px solid var(--line)}
.loc{color:var(--muted);font-size:.9rem}
ol.lessons{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.35rem}
ol.lessons li{display:grid;grid-template-columns:auto auto 1fr auto;gap:.5rem;align-items:baseline;font-size:.95rem;padding:.3rem 0;border-bottom:1px dashed var(--line)}
ol.lessons li:last-child{border-bottom:0}
ol.lessons .dot{width:.6rem;height:.6rem;border-radius:50%;background:var(--line);align-self:center}
ol.lessons .done .dot{background:var(--done)} ol.lessons .skipped .dot{background:var(--skipped)} ol.lessons .pending .dot{background:var(--pending)}
ol.lessons .current{font-weight:600} ol.lessons .current .dot{background:var(--accent);box-shadow:0 0 0 3px var(--soft)}
ol.lessons .k{font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
ol.lessons .st{font-size:.75rem;color:var(--muted);font-variant-numeric:tabular-nums}
ol.lessons .done,.lessons .skipped{color:var(--muted)}
.excerpt{font-family:var(--font-read);font-size:1.05rem;line-height:1.6;border-left:3px solid var(--soft);padding:.25rem 0 .25rem 1rem;margin:1rem 0}
footer{color:var(--muted);font-size:.85rem;border-top:1px solid var(--line);padding-top:.75rem}
a{color:var(--accent);text-underline-offset:2px} a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
code{background:var(--soft);padding:.05rem .35rem;border-radius:4px;font-size:.9em}
@media (prefers-reduced-motion:no-preference){.card{transition:border-color .2s}}
"""


def render(payload: dict) -> str:
    date = dt.date.fromisoformat(payload["date"])
    nice = date.strftime("%A %-d %B %Y") if sys.platform != "win32" else date.strftime("%A %d %B %Y")
    body = [
        "<title>Today's Lesson</title>",
        "<meta name=\"claude:viewport-fit-sides\" content=\"cover\">",
        "<link rel=\"stylesheet\" href=\"https://fonts.googleapis.com/css2?family=Inter:wght@400;600&family=Literata:ital,wght@0,400;0,600;1,400&display=swap\">",
        f"<style>{CSS}</style>",
        "<div class='wrap'>",
        f"<header class='top'><span class='date'>{_e(nice)}</span><span class='brand'>Learning agent</span></header>",
    ]
    for plan in payload["topics"]:
        body.append(render_topic(plan, date))
    body.append(
        "<footer>When you've done the hour, reply in the Claude session that sent this: your 3–5 sentence note marks the day done. "
        "<code>skip</code>, <code>not today</code>, <code>too hard</code> and questions about the reading all work too.</footer>"
    )
    body.append("</div>")
    return "\n".join(body)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _date(s: str | None) -> dt.date:
    return dt.date.fromisoformat(s) if s else dt.date.today()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="la", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent), help="repo root")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("today"); p.add_argument("--date"); p.add_argument("--topic", action="append")
    p = sub.add_parser("serve"); p.add_argument("--date"); p.add_argument("--topic", action="append")
    p = sub.add_parser("done"); p.add_argument("lesson"); p.add_argument("--date")
    g = p.add_mutually_exclusive_group(); g.add_argument("--note"); g.add_argument("--note-file")
    p = sub.add_parser("skip"); p.add_argument("lesson"); p.add_argument("--date"); p.add_argument("--reason", required=True)
    p = sub.add_parser("defer"); p.add_argument("lesson"); p.add_argument("--date")
    p = sub.add_parser("feedback"); p.add_argument("--topic", default=None); p.add_argument("--unit", required=True)
    for f in ("worked", "dragged", "hour", "change"):
        p.add_argument(f"--{f}", default="")
    p = sub.add_parser("seminar"); p.add_argument("lesson"); p.add_argument("--date"); p.add_argument("--summary-file", required=True)
    sub.add_parser("status")
    p = sub.add_parser("render"); p.add_argument("--date"); p.add_argument("--out", default="build/today.html")
    p = sub.add_parser("set-page-url"); p.add_argument("url")
    sub.add_parser("validate")

    a = ap.parse_args(argv)
    repo = Repo(Path(a.root))

    def out(obj):
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))

    if a.cmd in {"today", "serve"}:
        out(today(repo, _date(a.date), a.topic, record=(a.cmd == "serve")))
    elif a.cmd == "done":
        note = a.note
        if a.note_file:
            note = Path(a.note_file).read_text(encoding="utf-8")
        out(mark_done(repo, a.lesson, _date(a.date), note))
    elif a.cmd == "skip":
        out(mark_skipped(repo, a.lesson, _date(a.date), a.reason))
    elif a.cmd == "defer":
        out(mark_deferred(repo, a.lesson, _date(a.date)))
    elif a.cmd == "feedback":
        topic = a.topic or find_unit_topic(repo, a.unit)
        out(add_feedback(repo, topic, {"unit": a.unit, "worked": a.worked, "dragged": a.dragged, "hour": a.hour, "change": a.change}))
    elif a.cmd == "seminar":
        out(record_seminar(repo, a.lesson, _date(a.date), a.summary_file))
    elif a.cmd == "status":
        out(status_report(repo))
    elif a.cmd == "render":
        payload = today(repo, _date(a.date))
        html_text = render(payload)
        outp = Path(a.out)
        if not outp.is_absolute():
            outp = repo.root / outp
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(html_text, encoding="utf-8")
        out({"written": str(outp), "bytes": len(html_text.encode("utf-8"))})
    elif a.cmd == "set-page-url":
        repo.set_page_url(a.url)
        out({"url": a.url})
    elif a.cmd == "validate":
        problems = validate(repo)
        out({"ok": not problems, "problems": problems})
        return 1 if problems else 0
    return 0


def find_unit_topic(repo: Repo, unit_id: str) -> str:
    for topic in repo.topics():
        for phase in repo.curriculum(topic).get("phases", []):
            for unit in phase.get("units", []):
                if unit["id"] == unit_id:
                    return topic
    raise SystemExit(f"error: no unit with id {unit_id!r}")


if __name__ == "__main__":
    sys.exit(main())
