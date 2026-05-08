# Interactive REPL

`labflow repl` opens a stdlib-only shell against your team's database.
It reads and writes the same store as the API — changes are
immediately visible everywhere.

```text
$ labflow repl
LabFlow REPL — type 'help' for commands, 'quit' to exit.
Connected to the same database as the API server.
labflow> tasks
id | title                              | status | prio | owner
---+------------------------------------+--------+------+------
42 | Deploy staging                     | open   | high | 7
57 | Write design doc                   | open   |      | 7
labflow> task 42 done
#42 marked done
labflow> meetings
id | title          | type    | final
---+----------------+---------+------
1  | Kickoff        | standup | True
labflow> ingest /tmp/standup.txt "Standup 2026-05-08"
created meeting #2 ('Standup 2026-05-08')
labflow> quit
```

## Commands

| Command | Description |
| --- | --- |
| `tasks` | List the 20 most recent open tasks |
| `task <id> show` | Show one task's fields |
| `task <id> done` | Mark a task as done (audited as `task.status_changed`) |
| `meetings` | List the 20 most recent meetings |
| `decisions` | List the 20 most recent decisions |
| `ingest <path> [title]` | Create a meeting from a transcript file |
| `team [slug]` | Show the active team or switch to another |
| `help` / `?` | List commands |
| `quit` / `exit` / `EOF` | Leave |

Up/down arrow history works wherever Python's `readline` does (every
platform CPython supports out of the box).

## Scripting

The REPL is also drivable from Python — useful for one-off scripts
and for the test suite:

```python
from labflow.repl import run_script
print(run_script(["tasks", "decisions", "task 42 done"]))
```

`run_script` collects the REPL's stdout into a string so you can
assert on it.
