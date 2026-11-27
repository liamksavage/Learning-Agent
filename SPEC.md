# Learning Agent — Specification

Status: draft v1, 2026-11-27. Built from the Q&A in the first design session. Items marked **[confirm]** are decisions I made on your behalf that you should sanity-check before stage 1 is finished.

## 1. Purpose

A teacher agent that runs your self-taught curriculum for you. Every morning it tells you what to learn, hands you the material, and later takes your note and marks the day done. On Sundays it runs the seminar. Once a week it checks that next week's material actually exists and drafts new weeks with you before you run out.

The first curriculum is the philosophy plan that currently lives in the Claude Doc "Self-Taught Philosophy Curriculum". That doc is retired as the working plan once its contents are converted into the repo (section 4). The agent, not the doc, is the thing you talk to.

## 2. Principles

1. **Everything happens inside Claude.** No email, no Drive, no Slack. The reminder is a Claude push notification, the material is a Claude page, and you reply in the Claude session that sent the notification. Nothing routes you through a work tool first thing in the morning.
2. **The plan is a sequence, not a calendar.** Lessons are ordered; dates are not fixed. If you miss a day you simply do that lesson the next working day. Nothing is skipped and nothing is doubled.
3. **The habit matters more than the content.** The 10 / 40 / 10 hour and the Friday write and Sunday seminar are preserved exactly. Texts can be swapped; the rhythm cannot.
4. **Know before you need it.** Material for the coming week is verified a week ahead. You are never told at 6:30am that a text can't be found.
5. **The repo is the single source of truth.** Curriculum, progress, notes and reconstructions live here in plain files that both you and the agent edit. No state lives only in a chat.
6. **Fetch liberally.** Anything publicly reachable without a login is fair game. Owned books are referenced by chapter, and a free online equivalent is offered whenever one exists.
7. **Generic model, one curriculum.** The data model holds any number of topics. Philosophy is the first and, for now, only one.

## 3. Architecture

```
  Claude Code scheduled task (daily, 6:30 ET, Sun–Fri)
        │  fires a fresh cloud session on this repo
        ▼
  Morning session ─── reads curriculum + progress ───► picks today's lesson
        │                                                   │
        │   fetches sources, cuts pages, builds the page    │
        ▼                                                   ▼
  "Today" Claude page (stable link, republished daily)   Push notification
        ▲                                                   │
        │                                                   ▼
        └──── you open the session, read, reply "done" + your note
                      │
                      ▼
             session records note + progress, commits, pushes
```

**Runtime.** A Claude Code *scheduled task* that starts a fresh cloud session in this repository each morning. That session is the agent. There is no server, no API key, and no separate hosting; it runs on your Claude subscription in the same environment this session runs in.

**Why this and not GitHub Actions or your laptop.** Actions would need an Anthropic API key, a separate delivery channel (email, SMS) and an outside place for you to reply. Your laptop would need to be awake at 6:30 and has the same delivery problem. A scheduled Claude session gives you the push notification, a chat to reply in, and the Artifact tools for the reading page, all for free. The trade-off is that each run is a fresh session, so everything the agent needs to remember must be committed to the repo (principle 5).

**Push notification.** Scheduled tasks can send a push to your phone when a run finishes with something noteworthy. Every morning run ends with a short summary so the push fires. Saturdays have no run, so no notification.

**The "Today" page.** One Claude Artifact page with a stable URL, republished each morning. Pin it. It shows today's lesson at the top and the rest of the week below it. Reading excerpts are embedded in the page, so on a phone you open one link and read.

**Replying.** You reply in the morning's session. The agent in that session parses your reply, writes the note and progress to the repo, commits and pushes. Replies can arrive any time that day or the next morning.

**State across sessions.** Each morning session begins by pulling the branch, so it sees yesterday's reply. Every write to progress or notes is committed immediately so nothing is lost if the session is reclaimed.

## 4. Data model

All files are plain text in the repo and are meant to be edited by hand as well as by the agent.

```
curricula/
  philosophy.yaml          # the whole plan: phases → units → lessons
state/
  progress.yaml            # cursor + per-lesson status
  availability.yaml        # cache of verified source links and page maps
notes/
  philosophy/2026-11-27.md # your daily 3–5 sentence note, one file per day
reconstructions/
  philosophy/unit-00/      # one folder per unit, as the plan already asks
seminars/
  philosophy/2026-11-30.md # transcript summary + "two things to change"
```

### Curriculum (`curricula/<topic>.yaml`)

```yaml
topic: philosophy
title: Self-Taught Philosophy
hour: {reenter_min: 10, work_min: 40, write_min: 10}
week: {work_days: [mon, tue, wed, thu], write_day: fri, seminar_day: sun, rest_day: sat}
phases:
  - id: phase-0
    title: Orientation
    units:
      - id: p0-w1
        title: "Week 1: learning to read an argument"
        goal: "Take a passage and write out what it claims, what supports it, where the inference happens."
        lessons:
          - id: p0-w1-l1
            kind: read            # read | listen | write | seminar | exercise
            title: "Pryor, Guidelines on Reading Philosophy"
            sources:
              - kind: free-online
                url: http://www.jimpryor.net/teaching/guidelines/reading.html
                extract: all
            task: "Read all (~20 min). Then the 'Vocabulary Describing Arguments' page of his glossary."
            note_prompt: "In your own words: the difference between a valid argument and a sound one."
          - id: p0-w1-l4
            kind: read
            title: "Plato, Republic 514a–517a (the Cave)"
            sources:
              - kind: free-online
                url: https://pressbooks.ulib.csuohio.edu/republic/?p=37
                extract: {stephanus: "514a-517a"}
              - kind: owned-book
                title: "Republic, Grube/Reeve (Hackett)"
                locator: "Book VII, 514a–517a"
            task: "Read twice, slowly. Mark every so / therefore / because / must."
            note_prompt: "List every candidate for what the Cave argues for; at least three."
        audio:                      # optional, outside the hour
          - title: "Philosophize This! eps. 1–5"
            url: https://www.philosophizethis.org/podcasts
```

Source kinds: `free-online` (fetchable), `owned-book` (name chapter only), `paid-online` (link, no fetch), `audio`, `video`. `extract` tells the fetcher what to cut: `all`, a page range, a Stephanus or section range, or a chapter title.

### Progress (`state/progress.yaml`)

```yaml
philosophy:
  cursor: p0-w1-l4            # next lesson to serve
  lessons:
    p0-w1-l1: {status: done, date: 2026-11-08, note: notes/philosophy/2026-11-08.md}
    p0-w1-l2: {status: done, date: 2026-11-12}
    p0-w1-l3: {status: skipped, reason: "not needed for the Cave"}
  seminars:
    - {date: 2026-11-15, reconstruction: reconstructions/philosophy/unit-p0/r1-cave-v1.md, changes: [...]}
  feedback:                   # the weekly table from the doc, one row per week
    - {week: p0-w1, worked: "...", dragged: "...", hour: right, change: "..."}
```

## 5. The daily run

Runs Sunday to Friday at 6:30am Eastern. Saturday has no run.

1. Pull the branch. Load curriculum, progress and availability.
2. Settle yesterday. If yesterday's lesson has a reply recorded, it is already done. If not, mark it `pending` and ask about it in today's message (section 6).
3. Pick today's lesson by the weekday and the cursor:
   - Mon–Thu: the next `read`, `listen` or `exercise` lesson at the cursor.
   - Fri: the unit's `write` lesson, but only if every reading it depends on is done. If readings are still outstanding (because days were missed earlier in the week), Friday serves the next unfinished reading instead and says why. The write then lands on the first day after the readings are complete, even if that is next week. You never write a reconstruction of a text you haven't read. If this happens often, the fix is to add a second writing day to the week, not to write from half-read material.
   - Sun: seminar (section 8).
4. Fetch the material (section 7) from the availability cache, refreshing anything stale.
5. Build the "Today" page: lesson, exact task, note prompt, embedded excerpt or link, yesterday's note in full for the re-enter, audio for the week, then the rest of the week's lessons in order.
6. Post the morning message in the session: two or three lines, the page link, and the line "Reply with your note when you're done, or 'skip' / 'not today'." This summary is what triggers the push.
7. Commit anything that changed (availability cache, progress).

Message shape, for reference:

> **Thu 27 Nov — Week 3, lesson 4 of 6.** Smith ch. 5, the counterexample method, plus exercises. Yesterday's note is on the page.
> Open: [Today's lesson]
> Reply here with your 3–5 sentences when done.

## 6. Replies

You reply in the session in plain language. The agent recognises:

| You say | Agent does |
| --- | --- |
| A paragraph of prose | Saves it as today's note, marks the lesson done, advances the cursor |
| `done` with no note | Marks done, asks once for the note, does not block on it |
| `skip` | Marks skipped, advances the cursor; asks for a one-line reason |
| `not today` / `tomorrow` | Leaves the cursor; the same lesson is served next working day |
| `too hard` / `too easy` / `too long` | Records the feedback against the unit; mentions it in the Sunday review |
| A question about the reading | Answers it, as a teacher would, without changing state |
| A reconstruction (pasted or a file) | Saves it to the unit's folder as the next version |

Unrecorded days are treated as not done and the lesson is served again **[confirm]**. One word the next morning fixes it.

The agent never advances the cursor on its own guess. It confirms every state change in one line, e.g. "Marked Wed 25 done, note saved."

## 7. Fetching material

Done the week before (section 9) and refreshed the morning of.

**What "fetch" means, per source kind:**

- `free-online` text: download it, cut exactly the assigned range (pages, Stephanus numbers, section headings or chapter titles), and embed the excerpt in the Today page. Keep the original link beside it. For PDFs (Early Modern Texts, Peter Smith's logic book) the cut is by page range; for HTML the cut is by heading or Stephanus marker.
- `owned-book`: name the chapter or page range exactly as the plan does. If a free online copy of the same text exists (e.g. Jowett's Republic on Gutenberg beside your Grube/Reeve), fetch that as well and label it as the alternative.
- `audio` / `video`: the direct episode or lecture link with its length, and a timestamp range when the plan names one.
- Primer (toggle, default off): a one-paragraph framing of why today's passage matters and a 5-term glossary for it, generated by the agent and labelled as such. Turn on per topic or per unit in the curriculum file.

**Copyright line.** As lenient as possible: anything publicly reachable without a login or a payment is fetched. Nothing is downloaded from behind a paywall, and nothing fetched is republished anywhere other than your private Today page.

**Availability check.** For each lesson in the coming seven days the agent fetches every `free-online` source once, confirms the assigned range can be located, and records the result in `state/availability.yaml` with a timestamp. Failures surface in the Sunday review and in the morning message two days before the lesson, with the agent's best substitute already found. A lesson is never served with a dead link unannounced.

**Network.** The cloud environment this runs in currently allows no outside hosts, so stage 2 needs its own environment with an allow-list of the source domains (Early Modern Texts, Project Gutenberg, Logic Matters, jimpryor.net, pressbooks.ulib.csuohio.edu, historyofphilosophy.net, plato.stanford.edu, archive.org, youtube.com, and whatever later units add). I will propose that environment when stage 2 starts; you approve it once.

## 8. Seminar mode (Sundays)

The Sunday run is the seminar. The morning message says what the seminar is about and asks you to open the session when you have 45–60 minutes.

1. The agent has already read the week's reconstruction from the unit folder (or asks you to paste it), the week's daily notes and any `too hard` flags.
2. It opens by stating the thesis it will argue against, as the plan describes ("the Cave isn't an argument at all but an image"), then argues it step by step. You defend or concede.
3. It ends by proposing the two things to change, you edit or accept, and it writes `seminars/<topic>/<date>.md` with a short summary and the two changes.
4. It fills the weekly feedback row (section 9) from the week's replies, shows it to you, and asks you to correct it before saving. It double-checks its own row against the notes files, not just the chat.

Week 4's different format (bring three exercises you got wrong, then a cold argument) is a `seminar` lesson with its own `task` text; the agent follows whatever the lesson says.

## 9. Weekly review and planning (part of the Sunday run)

After the seminar, or on its own if you don't show up:

- **Availability look-ahead** for the next seven days (section 7).
- **Feedback row** for the week just finished, saved to progress.
- **Drafting ahead.** When the cursor is within one unit (roughly two weeks) of the end of the detailed plan, the agent drafts the next unit day by day from the unit tables and the plan's rules (lecture → read → write; 5–15 pages a day; Friday write; one reconstruction per unit) and asks you to approve or edit it in the session. Approved drafts are appended to the curriculum file and committed. Nothing unapproved is ever served.
- **Pace check.** If a unit has spilled past its planned length, it says so and proposes the plan's own remedy: cut the reading, never the writing.

## 10. Converting the existing doc

A one-time job at the start of stage 1: transcribe the Curriculum and Weeks 1–4 tabs into `curricula/philosophy.yaml`, with weeks 1–4 as detailed lessons and the Phase 1 and Phase 2 tables as units with sources but no lessons yet. Record what you've already done (Sun 8, Wed 11 done; Mon 9 folded into Thu 12; Tue 10 skipped) in progress. The doc stays as a historical record; the agent does not read it again.

## 11. Multiple topics

Everything above is keyed by `topic`. A second curriculum is a second YAML file and a second progress block. Topics can share a morning run (one message covering both) or get separate scheduled tasks; decide when the second topic arrives.

## 12. What you need to set up

| Item | Why | When |
| --- | --- | --- |
| Approve one scheduled task (daily 6:30 ET, Sun–Fri, push on) | The morning run | Stage 1 |
| Approve a dedicated cloud environment with the source-site allow-list | Fetching texts | Stage 2 |
| Nothing else | No API key, email service, Drive or Slack needed | — |

Cost: runs on your Claude subscription. If the agent ever needs anything that costs money (a paid text, a service), it asks first and the ceiling is $20/month.

## 13. Delivery stages

1. **Stage 1 — the morning loop.** Curriculum converted to YAML; progress tracking; the scheduled task; the Today page with links only; reply handling for notes, done, skip, not today. Success: for one full week you get a push each morning, read from the page, reply, and the repo shows the right state.
2. **Stage 2 — material.** Dedicated environment; the fetcher that cuts exact ranges into the page; the availability look-ahead; owned-book alternatives; primer toggle.
3. **Stage 3 — teaching.** Seminar mode; weekly feedback row; drafting the next unit ahead; pace check.
4. **Stage 4 — second topic.** Only when you have one.

## 14. Decisions to confirm

Settled: the writing task waits until its readings are done (section 5, Friday rule). Decided 2026-11-27.

- **[confirm]** Scheduled sessions pushing to this repo: I believe a fresh scheduled session can commit and push, but I'll verify it on the first run of stage 1 and fall back to a dedicated branch if needed.
- **[confirm]** Unreplied days count as not done and are served again the next working day.
- **[confirm]** One stable Today page republished daily, rather than one page per day. Past days remain in notes and progress, not as pages.
- **[confirm]** Run time 6:30am Eastern. The scheduler advises nudging off the half hour to avoid delays, so the actual cron will be 6:27.
