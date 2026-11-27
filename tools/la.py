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
                if s.get("kind") not in {"free-online", "book", "paid-online", "audio", "video"}:
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


def _para(text: str | None, cls: str = "") -> str:
    if not text:
        return ""
    c = f" class='{cls}'" if cls else ""
    return "".join(f"<p{c}>{_e(p.strip())}</p>" for p in str(text).strip().split("\n\n") if p.strip())


KIND_LABEL = {"read": "Read", "listen": "Listen", "exercise": "Exercises", "write": "Write", "seminar": "Seminar"}
STATUS_GLYPH = {"done": "✓", "skipped": "–", "pending": "·", "upcoming": "", "today": "●"}


def _source_html(s: dict) -> str:
    kind = s.get("kind")
    title = _e(s.get("title"))
    note = f"<div class='src-note'>{_e(s.get('note'))}</div>" if s.get("note") else ""
    if kind == "book":
        loc = f"<div class='src-loc'>{_e(s.get('locator'))}</div>" if s.get("locator") else ""
        return f"<li class='src'><span class='tag book'>Book</span><div class='src-body'><div>{title}</div>{loc}{note}</div></li>"
    tag = {"free-online": "Online", "paid-online": "Paid", "audio": "Audio", "video": "Video"}.get(kind, "Link")
    if s.get("verified") is False:
        tag += " · unverified"
    link = f"<a href='{_e(s.get('url'))}' target='_blank' rel='noopener'>{title}</a>" if s.get("url") else title
    extract = s.get("extract")
    loc = ""
    if isinstance(extract, dict):
        loc = "<div class='src-loc'>" + _e(", ".join(f"{k.capitalize()} {v}" for k, v in extract.items())) + "</div>"
    return f"<li class='src'><span class='tag'>{tag}</span><div class='src-body'><div>{link}</div>{loc}{note}</li>"


def _step(n: int, title: str, mins, body: str, cls: str = "") -> str:
    mins_html = f"<span class='mins'>{mins} min</span>" if mins else ""
    return (
        f"<li class='step {cls}'><div class='step-rail'><span class='step-n'>{n}</span></div>"
        f"<div class='step-body'><h3>{_e(title)}{mins_html}</h3>{body}</div></li>"
    )


def _work_steps(plan: dict, L: dict) -> str:
    hour = plan.get("hour", {})
    kind = L.get("kind")
    yn = plan.get("yesterday_note")

    # 1. Re-enter
    if yn:
        text = yn["text"].split("\n\n", 2)[-1] if yn["text"].startswith("#") else yn["text"]
        body1 = f"<p class='lead-in'>Your note from {_e(yn['date'])}:</p>" + _para(text, "read")
    else:
        body1 = "<p class='read'>No note on file yet. Reread the last paragraph you read, then begin.</p>"

    # 2. Work
    body2 = _para(L.get("task"), "read")
    if kind in {"write", "seminar"} and not L.get("requires_met", True):
        body2 += ("<p class='warn-inline'>The reading this depends on isn't recorded as done: "
                  + _e(", ".join(L.get("unmet_requires", []))) + ". Do what you can and say so in your reply.</p>")
    srcs = L.get("sources") or []
    if srcs:
        body2 += "<h4>Material</h4><ul class='sources'>" + "".join(_source_html(s) for s in srcs) + "</ul>"
    if L.get("excerpt"):
        body2 += f"<div class='excerpt'>{L['excerpt']}</div>"

    # 3. Note
    prompt = L.get("note_prompt")
    body3 = (f"<p class='read prompt'>{_e(prompt)}</p>" if prompt else "<p class='read'>Three to five sentences: what was claimed, what supported it, what you don't yet buy.</p>")
    body3 += "<p class='muted small'>Reply with it in the Claude session that sent this; that marks the day done.</p>"

    label2 = {"write": "Write", "exercise": "Exercises", "listen": "Listen"}.get(kind, "Read")
    return (
        "<ol class='hour'>"
        + _step(1, "Re-enter", hour.get("reenter_min", 10), body1)
        + _step(2, label2, hour.get("work_min", 40), body2, "main")
        + _step(3, "Note", hour.get("write_min", 10), body3)
        + "</ol>"
    )


def _seminar_steps(plan: dict, L: dict) -> str:
    reqs = L.get("requires") or []
    bring = ("<p class='read'>Bring " + _e(", ".join(reqs)) + ". Open the Claude session when you have 45–60 minutes.</p>") if reqs else "<p class='read'>Open the Claude session when you have 45–60 minutes.</p>"
    if not L.get("requires_met", True):
        bring += "<p class='warn-inline'>The reconstruction isn't recorded as done. We'll run the seminar on your notes instead.</p>"
    body2 = _para(L.get("task"), "read")
    body3 = "<p class='read'>Two things to change, agreed together, saved to the repo. Then the week's feedback row.</p>"
    return (
        "<ol class='hour'>"
        + _step(1, "Bring", None, bring)
        + _step(2, "Seminar", "45–60", body2, "main")
        + _step(3, "Afterwards", None, body3)
        + "</ol>"
    )


def render_topic(plan: dict, date: dt.date) -> str:
    mode = plan["mode"]
    parts = ["<section class='topic'>"]

    if mode == "rest":
        parts.append(f"<p class='eyebrow'>{_e(plan['title'])}</p><h1 class='lesson-title'>Rest day</h1><p class='read'>No reading. The plan picks up tomorrow.</p></section>")
        return "".join(parts)

    if plan.get("exhausted"):
        nxt = plan.get("next_undrafted_unit") or {}
        parts.append(
            f"<p class='eyebrow'>{_e(plan['title'])}</p><h1 class='lesson-title'>The detailed plan has run out</h1>"
            f"<p class='read'>Next up is <strong>{_e(nxt.get('title'))}</strong>, which hasn't been drafted day by day yet. "
            "Reply in the session and we'll draft it together.</p></section>"
        )
        return "".join(parts)

    L, U = plan["lesson"], plan["unit"]
    pos = L.get("position", {})
    kind = L.get("kind")

    parts.append(
        f"<p class='eyebrow'>{_e(plan['title'])} <span class='sep'>·</span> {_e(U['title'])} <span class='sep'>·</span> lesson {pos.get('index')} of {pos.get('of')}</p>"
        f"<h1 class='lesson-title'>{_e(L.get('title'))}</h1>"
    )

    ls = plan.get("last_served")
    if ls and not ls.get("settled"):
        parts.append(
            "<div class='nudge'><strong>Last time isn't recorded.</strong> "
            f"You were given <em>{_e(ls['lesson'])}</em> on {_e(ls['date'])} and I have no reply. "
            "If you did it, reply <code>done</code> with your note and I'll mark it. Otherwise today's lesson stands.</div>"
        )

    parts.append(_seminar_steps(plan, L) if kind == "seminar" else _work_steps(plan, L))

    # Where you are
    parts.append(f"<section class='block'><h2>Where you are</h2><p class='unit-title'>{_e(U['title'])}</p>")
    if U.get("goal"):
        parts.append(f"<p class='goal'>{_e(U['goal'])}</p>")
    parts.append("<ol class='lessons'>")
    for row in U.get("lessons", []):
        st = "today" if row["id"] == L["id"] else row["status"]
        parts.append(
            f"<li class='{st}'><span class='glyph' aria-hidden='true'>{STATUS_GLYPH.get(st, '')}</span>"
            f"<div><div class='l-title'>{_e(row['title'])}</div>"
            f"<div class='l-meta'>{KIND_LABEL.get(row['kind'], row['kind'])} · {_e(st)}</div></div></li>"
        )
    parts.append("</ol>")
    if U.get("have_ready"):
        parts.append("<details class='have-ready'><summary>Everything this unit uses</summary><ul class='sources'>"
                     + "".join(_source_html(s) for s in U["have_ready"]) + "</ul></details>")
    parts.append("</section>")

    if U.get("audio"):
        parts.append("<section class='block'><h2>Audio, outside the hour</h2><ul class='sources'>")
        for a in U["audio"]:
            link = f"<a href='{_e(a.get('url'))}' target='_blank' rel='noopener'>{_e(a.get('title'))}</a>" if a.get("url") else _e(a.get("title"))
            note = f"<div class='src-note'>{_e(a.get('note'))}</div>" if a.get("note") else ""
            parts.append(f"<li class='src'><span class='tag'>Listen</span><div class='src-body'><div>{link}</div>{note}</div></li>")
        parts.append("</ul></section>")

    if plan.get("draft_ahead") and plan.get("next_undrafted_unit"):
        nxt = plan["next_undrafted_unit"]
        parts.append(f"<p class='muted small'>About {plan['remaining_work_lessons']} lessons of detailed plan remain. Next to draft: {_e(nxt.get('title'))}. I'll raise it on Sunday.</p>")
    parts.append("</section>")
    return "".join(parts)


CSS = """
/* Layout: one calm column. The hour is the page: three numbered steps down a rail,
   the work step dominant. The week map and unit material sit below, quieter. */
:root{
  --bg:#f6f6f2; --fg:#1c231f; --muted:#6a746e; --line:#e1e4de; --rail:#d2d8d1;
  --accent:#2d5f4e; --accent-ink:#ffffff; --soft:#e4ede7; --warn:#7a4f0e; --warn-bg:#f8efdc;
  --done:#2d5f4e; --skipped:#a3aba5;
  --font-ui:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
  --font-read:'Literata',Georgia,'Times New Roman',serif;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#131816; --fg:#e9ece7; --muted:#9ba69f; --line:#283029; --rail:#303a33;
  --accent:#86c9ac; --accent-ink:#0f1a15; --soft:#1f2b25; --warn:#e6bb73; --warn-bg:#2a2416;
  --done:#86c9ac; --skipped:#5f6963; color-scheme:dark}}
:root[data-theme="dark"]{
  --bg:#131816; --fg:#e9ece7; --muted:#9ba69f; --line:#283029; --rail:#303a33;
  --accent:#86c9ac; --accent-ink:#0f1a15; --soft:#1f2b25; --warn:#e6bb73; --warn-bg:#2a2416;
  --done:#86c9ac; --skipped:#5f6963; color-scheme:dark}

body{background:var(--bg);color:var(--fg);font-family:var(--font-ui);font-size:16px;line-height:1.55;
  padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));padding-block:1.5rem 4rem}
.wrap{max-width:38rem;margin:0 auto;display:flex;flex-direction:column;gap:2.5rem}
a{color:var(--accent);text-decoration-thickness:1px;text-underline-offset:3px} a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
code{background:var(--soft);padding:.05rem .4rem;border-radius:4px;font-size:.9em}
p{margin:0}
h1,h2,h3,h4{margin:0}

header.top{display:flex;justify-content:space-between;align-items:baseline;gap:1rem;flex-wrap:wrap}
header.top .date{font-weight:600;font-size:1.05rem}
header.top .brand{color:var(--muted);font-size:.75rem;text-transform:uppercase;letter-spacing:.1em}

.topic{display:flex;flex-direction:column;gap:2.25rem;min-width:0}
.eyebrow{color:var(--muted);font-size:.85rem;margin:0 0 .6rem}
.eyebrow .sep{opacity:.5;margin:0 .25rem}
.lesson-title{font-family:var(--font-read);font-weight:600;font-size:clamp(1.75rem,6vw,2.4rem);line-height:1.15;text-wrap:balance;margin:0}
.nudge{background:var(--warn-bg);color:var(--fg);border-radius:10px;padding:.9rem 1rem;font-size:.95rem;line-height:1.5}

/* The hour */
ol.hour{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:0}
.step{display:grid;grid-template-columns:2.25rem 1fr;gap:.9rem;min-width:0}
.step-rail{display:flex;flex-direction:column;align-items:center}
.step-n{width:2rem;height:2rem;border-radius:50%;background:var(--soft);color:var(--fg);display:flex;align-items:center;justify-content:center;font-weight:600;font-size:.9rem;flex:none;font-variant-numeric:tabular-nums}
.step.main .step-n{background:var(--accent);color:var(--accent-ink)}
.step-rail::after{content:"";flex:1;width:2px;background:var(--rail);margin:.4rem 0}
.step:last-child .step-rail::after{display:none}
.step-body{padding:.2rem 0 2.25rem;min-width:0;display:flex;flex-direction:column;gap:.75rem}
.step:last-child .step-body{padding-bottom:0}
.step h3{font-size:1rem;font-weight:600;display:flex;align-items:baseline;gap:.6rem}
.step .mins{color:var(--muted);font-weight:400;font-size:.85rem;font-variant-numeric:tabular-nums}
.step.main h3{font-size:1.1rem}
.read{font-family:var(--font-read);font-size:1.1rem;line-height:1.6}
.step.main .read{font-size:1.15rem}
.lead-in{color:var(--muted);font-size:.85rem}
.prompt{font-style:italic}
.warn-inline{color:var(--warn);font-size:.95rem}
h4{font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:600;margin-top:.5rem}

/* Sources */
ul.sources{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:.85rem}
li.src{display:grid;grid-template-columns:auto 1fr;gap:.7rem;align-items:start;min-width:0;line-height:1.45}
.src-body{min-width:0;display:flex;flex-direction:column;gap:.15rem}
.tag{font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;background:var(--soft);color:var(--fg);padding:.2rem .5rem;border-radius:999px;font-weight:600;white-space:nowrap;margin-top:.15rem}
.tag.book{background:transparent;border:1px solid var(--line);color:var(--muted)}
.src-loc,.src-note{color:var(--muted);font-size:.9rem}
.excerpt{font-family:var(--font-read);font-size:1.08rem;line-height:1.65;border-left:3px solid var(--rail);padding:.25rem 0 .25rem 1rem;margin-top:.5rem}

/* Blocks below the hour */
.block{border-top:1px solid var(--line);padding-top:1.5rem;display:flex;flex-direction:column;gap:.9rem;min-width:0}
.block h2{font-size:.75rem;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);font-weight:600}
.unit-title{font-weight:600;font-size:1.05rem}
.goal{color:var(--muted);font-size:.95rem;max-width:34rem}
ol.lessons{list-style:none;padding:0;margin:.25rem 0 0;display:flex;flex-direction:column}
ol.lessons li{display:grid;grid-template-columns:1.5rem 1fr;gap:.6rem;align-items:start;padding:.65rem 0;border-bottom:1px solid var(--line)}
ol.lessons li:last-child{border-bottom:0}
ol.lessons .glyph{width:1.5rem;text-align:center;color:var(--muted);font-weight:600;line-height:1.45}
ol.lessons .done .glyph{color:var(--done)} ol.lessons .today .glyph{color:var(--accent)}
ol.lessons .l-title{line-height:1.4}
ol.lessons .l-meta{color:var(--muted);font-size:.8rem;margin-top:.1rem}
ol.lessons .done .l-title,ol.lessons .skipped .l-title{color:var(--muted)}
ol.lessons .today .l-title{font-weight:600}
details.have-ready summary{cursor:pointer;color:var(--muted);font-size:.9rem;padding:.25rem 0}
details.have-ready[open] summary{margin-bottom:.75rem}
.muted{color:var(--muted)} .small{font-size:.85rem}
footer{color:var(--muted);font-size:.9rem;border-top:1px solid var(--line);padding-top:1.25rem;line-height:1.55}
@media (prefers-reduced-motion:reduce){*{transition:none !important}}
"""


def render(payload: dict) -> str:
    date = dt.date.fromisoformat(payload["date"])
    nice = f"{date.strftime('%A')} {date.day} {date.strftime('%B %Y')}"
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
        "<footer>When the hour is done, reply in the Claude session that sent this. Your 3–5 sentence note marks the day done. "
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
