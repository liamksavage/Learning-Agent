# Learning Agent

A teacher agent for a self-taught curriculum. Every morning it sends a Claude push notification with the day's lesson, publishes a Today page with the material, and records the reply. Sundays are seminars.

- Design: [SPEC.md](SPEC.md)
- How the morning session works: [agent/MORNING.md](agent/MORNING.md)
- The plan: [curricula/philosophy.yaml](curricula/philosophy.yaml)

```bash
python3 tools/la.py today            # what's due today
python3 tools/la.py status           # where you are in the plan
python3 tools/la.py render           # build/today.html
python3 -m pytest -q                 # tests
```
