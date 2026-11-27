import datetime as dt
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import la  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

FIXTURE = {
    "topic": "demo",
    "title": "Demo",
    "hour": {"reenter_min": 10, "work_min": 40, "write_min": 10},
    "week": {"work_days": ["mon", "tue", "wed", "thu"], "write_day": "fri", "seminar_day": "sun", "rest_day": "sat"},
    "phases": [{
        "id": "p",
        "title": "Phase",
        "units": [
            {
                "id": "u1", "title": "Unit 1", "goal": "g",
                "lessons": [
                    {"id": "u1-l1", "kind": "read", "title": "Read A", "task": "t", "sources": [
                        {"kind": "free-online", "title": "A", "url": "https://example.org/a", "extract": "all"}]},
                    {"id": "u1-l2", "kind": "read", "title": "Read B", "task": "t", "sources": [
                        {"kind": "owned-book", "title": "B", "locator": "ch. 2"}]},
                    {"id": "u1-l3", "kind": "write", "title": "Write 1", "task": "t", "requires": ["u1-l1", "u1-l2"], "sources": []},
                    {"id": "u1-s1", "kind": "seminar", "title": "Seminar 1", "task": "t", "requires": ["u1-l3"], "sources": []},
                ],
            },
            {"id": "u2", "title": "Unit 2", "status": "undrafted", "lessons": []},
        ],
    }],
}

MON, TUE, WED, THU, FRI, SAT, SUN = (dt.date(2026, 11, 23) + dt.timedelta(days=i) for i in range(7))


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "curricula").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "curricula" / "demo.yaml").write_text(yaml.safe_dump(FIXTURE), encoding="utf-8")
    return la.Repo(tmp_path)


def plan(repo, date):
    return la.today(repo, date)["topics"][0]


def test_saturday_is_rest(repo):
    p = plan(repo, SAT)
    assert p["mode"] == "rest" and p["lesson"] is None


def test_weekday_serves_first_unfinished_non_seminar(repo):
    assert plan(repo, MON)["lesson"]["id"] == "u1-l1"
    la.mark_done(repo, "u1-l1", MON, "note one")
    assert plan(repo, TUE)["lesson"]["id"] == "u1-l2"


def test_missed_days_slide_forward_not_skip(repo):
    # Nothing done Mon–Thu: Friday still serves the FIRST reading, not the write.
    p = plan(repo, FRI)
    assert p["lesson"]["id"] == "u1-l1"


def test_write_waits_for_its_readings(repo):
    la.mark_done(repo, "u1-l1", MON, None)
    # l2 not done → Friday serves l2 (a read), the write waits.
    assert plan(repo, FRI)["lesson"]["id"] == "u1-l2"
    la.mark_done(repo, "u1-l2", FRI, None)
    # Next working day serves the write, even though it's not Friday.
    nxt = plan(repo, MON + dt.timedelta(days=7))
    assert nxt["lesson"]["id"] == "u1-l3" and nxt["lesson"]["requires_met"] is True


def test_skipped_counts_as_passed(repo):
    la.mark_skipped(repo, "u1-l1", MON, "not needed")
    assert plan(repo, TUE)["lesson"]["id"] == "u1-l2"


def test_sunday_serves_seminar_and_flags_unmet_requires(repo):
    p = plan(repo, SUN)
    assert p["mode"] == "seminar"
    assert p["lesson"]["id"] == "u1-s1"
    assert p["lesson"]["requires_met"] is False
    assert p["lesson"]["unmet_requires"] == ["u1-l3"]


def test_seminar_not_served_on_weekday(repo):
    for lid in ("u1-l1", "u1-l2", "u1-l3"):
        la.mark_done(repo, lid, MON, None)
    p = plan(repo, TUE)
    assert p["lesson"] is None and p["exhausted"] is True
    assert p["next_undrafted_unit"]["id"] == "u2"


def test_serve_records_and_unsettled_yesterday_is_reported(repo):
    la.today(repo, MON, record=True)
    prog = repo.progress()["demo"]
    assert prog["served"] == [{"date": MON.isoformat(), "lesson": "u1-l1"}]
    assert prog["lessons"]["u1-l1"]["status"] == "pending"
    p = plan(repo, TUE)
    assert p["last_served"]["lesson"] == "u1-l1" and p["last_served"]["settled"] is False
    # Same lesson is served again, since it slid forward.
    assert p["lesson"]["id"] == "u1-l1"


def test_serve_is_idempotent_for_a_day(repo):
    la.today(repo, MON, record=True)
    la.today(repo, MON, record=True)
    assert len(repo.progress()["demo"]["served"]) == 1


def test_done_writes_note_file_and_next_day_reenters_it(repo):
    la.mark_done(repo, "u1-l1", MON, "Valid means the form guarantees the conclusion.")
    note_path = repo.root / "notes" / "demo" / f"{MON.isoformat()}.md"
    assert note_path.exists()
    assert "Lesson: u1-l1" in note_path.read_text()
    p = plan(repo, TUE)
    assert p["yesterday_note"]["date"] == MON.isoformat()
    assert "guarantees" in p["yesterday_note"]["text"]


def test_defer_keeps_lesson_pending(repo):
    la.today(repo, MON, record=True)
    la.mark_deferred(repo, "u1-l1", MON)
    assert plan(repo, TUE)["lesson"]["id"] == "u1-l1"


def test_feedback_replaces_row_for_same_unit(repo):
    la.add_feedback(repo, "demo", {"unit": "u1", "worked": "a", "dragged": "", "hour": "right", "change": ""})
    la.add_feedback(repo, "demo", {"unit": "u1", "worked": "b", "dragged": "", "hour": "right", "change": ""})
    rows = repo.progress()["demo"]["feedback"]
    assert len(rows) == 1 and rows[0]["worked"] == "b"


def test_render_contains_lesson_and_theme_tokens(repo):
    la.mark_done(repo, "u1-l1", MON, "my note")
    html = la.render(la.today(repo, TUE))
    assert "<title>Today's Lesson</title>" in html
    assert "Read B" in html
    assert "my note" in html
    assert "Your copy" in html  # owned book rendered as such
    assert "prefers-color-scheme: dark" in html and '[data-theme="dark"]' in html


def test_render_rest_and_exhausted(repo):
    assert "Rest day" in la.render(la.today(repo, SAT))
    for lid in ("u1-l1", "u1-l2", "u1-l3"):
        la.mark_done(repo, lid, MON, None)
    assert "run out" in la.render(la.today(repo, TUE))


def test_cli_roundtrip(repo, capsys):
    rc = la.main(["--root", str(repo.root), "serve", "--date", MON.isoformat()])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["topics"][0]["lesson"]["id"] == "u1-l1"
    rc = la.main(["--root", str(repo.root), "done", "u1-l1", "--date", MON.isoformat(), "--note", "n"])
    assert rc == 0
    assert repo.progress()["demo"]["lessons"]["u1-l1"]["status"] == "done"


def test_validate_catches_bad_requires(repo):
    bad = dict(FIXTURE)
    bad["phases"][0]["units"][0]["lessons"][0]["requires"] = ["nope"]
    (repo.root / "curricula" / "demo.yaml").write_text(yaml.safe_dump(bad), encoding="utf-8")
    problems = la.validate(repo)
    assert any("unknown lesson nope" in p for p in problems)
    del bad["phases"][0]["units"][0]["lessons"][0]["requires"]


def test_real_curriculum_validates():
    repo = la.Repo(REPO_ROOT)
    assert la.validate(repo) == []


def test_real_curriculum_next_lesson_is_after_recorded_progress():
    repo = la.Repo(REPO_ROOT)
    p = la.today(repo, dt.date(2026, 11, 30))["topics"][0]  # a Monday
    # Sun 8 and Wed 11 done, Mon 9 and Tue 10 skipped → next is Thu 12's lesson.
    assert p["lesson"]["id"] == "p0-w1-l5"
