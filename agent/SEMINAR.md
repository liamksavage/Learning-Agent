# Sunday seminar

Stage 3 fleshes this out. Until then, the Sunday run follows this minimal procedure.

1. The morning `serve` output names the seminar lesson and its `requires` (the reconstruction). Read the reconstruction from `reconstructions/<topic>/unit-<unit id>/` (latest version), and the week's notes from `notes/<topic>/`.
2. When the user opens the session, run the seminar as the lesson's `task` describes: state the thesis you will argue against, argue it step by step, let them defend or concede. 45–60 minutes of exchange; keep each turn short.
3. End by proposing the two things to change. Let them edit or accept.
4. Write `seminars/<topic>/<date>.md`: a 10-line summary and the two changes. Then:

```bash
python3 tools/la.py seminar <seminar lesson id> --summary-file seminars/<topic>/<date>.md
```

5. If `feedback_due_for_unit` was true, draft the unit's feedback row from the week's notes and replies, show it, ask them to correct it, then:

```bash
python3 tools/la.py feedback --unit <unit id> --worked "..." --dragged "..." --hour "right|long|short" --change "..."
```

6. Commit and push `state seminars`.

If the user does not show up, leave the seminar lesson pending; it is served again next Sunday. Still do step 5 if you can from the notes alone, and say so in the message.
