# The round-table skill

A tournament finds the strong answers. A table merges them. One Claude Code skill, MIT, no signup,
no API key, nothing to connect.

> Forked from [arena-skill](https://github.com/Jakeschincariol/arena-skill) by Jake Schincariol
> (MIT), https://github.com/Jakeschincariol/arena-skill. The bracket, the strategy cards, the rubric
> and the attack, defend and judge briefs are his. The stop-at-K bracket, the table, the red team,
> the final check, per-role models and `--auto` are new in this fork.

`/round-table` gives N sub-agents (default 100) the exact same task, word for word, each with a
different strategy card: a way of reasoning, a workflow and a strategy. They fight a
single-elimination bracket (each attacks the other's solution, each defends and revises its own, a
judge scores the match on a written rubric) until **K are left** (default 10), not one. Then:

1. **Pitch.** Each of the K writes at most 300 words: its three strongest ideas, the attacks it
   conceded and how it fixed them, and what the others likely missed.
2. **Table.** Up to R rounds (default 3, max 5). Each seat reads the current draft, the open
   objections and every pitch, and contributes up to 5 items: ADOPT another seat's idea, AMEND a
   named section, or OBJECT with a concrete counterexample. A scribe merges them into the next draft
   and logs every accepted and rejected item with a reason; no idea disappears without one. Then
   every seat votes ACCEPT, or OBJECT with exactly one concrete objection. The table stops early when
   no objection is standing.
3. **Red team.** Two fresh attackers who never sat at the table attack the final draft. The seats
   defend in one round, and the scribe writes the final version.
4. **Final check.** A blind judge compares the table's answer with the bracket champion (the
   survivor with the best judge score). The better one is delivered to `<run>/solution.md`, so the
   table never delivers something worse than the best single competitor.

Anti-groupthink: a seat keeps its strongest idea unless an objection against it is proven, and the
scribe lists every dropped idea in `table/log.md`.

**It never touches your files.** Every sub-agent writes inside `.round-table/`. The answer comes
back to you, and using it is your call.

## Install

```bash
git clone <this repo>
cp -r round-table/skills/round-table ~/.claude/skills/
```

Or as a plugin, from a clone or its GitHub repo:

```
/plugin marketplace add IcaruzSoftware/round-table-skill
/plugin install round-table@round-table
```

As a plugin it shows up as `/round-table:round-table`; copy the folder for plain `/round-table`. It
needs Claude Code (it spawns sub-agents with the Agent tool) and Python 3.8 or newer. Nothing to pip
install.

## Use it

```
/round-table
/round-table --quick write the headline for our pricing page
/round-table --n 32 --k 6 --table-rounds 2 fix the flaky test in tests/test_api.py
/round-table --auto plan my launch week, I have 6 hours a day
```

| flag | what it does |
| --- | --- |
| `--n N` (alias `--agents N`) | N competitors. Default 100. Must be more than K. |
| `--k K` | K survivors sit at the table. Default 10, at least 2. |
| `--table-rounds R` | Table rounds. Default 3, max 5. |
| `--quick` | 16 competitors, K unchanged. |
| `--auto` | A sizer sub-agent picks N, K and R from the task. No confirmation step. |
| `--max-n M` | The cap on N for `--auto`. Default 100. |
| `--seed S` | Same seed, same cards and same bracket. Default random, and recorded. |
| `--wave W` | Sub-agents per wave. Default 10. |
| `--model-<role> M` | haiku, sonnet or opus. Defaults: competitor haiku, defender haiku, attacker sonnet, judge sonnet, seat opus, scribe opus, redteam sonnet, final opus, sizer sonnet. |

All defaults live in one block at the top of `skills/round-table/bracket.py`.

**`--auto`** runs one sizer sub-agent first. Its JSON suggestion is clamped: N to 8 .. `--max-n`,
K to 2 .. min(10, N / 2), R to 1 .. 5. Invalid or missing JSON falls back to 100, 10, 3, and the
output says so. Any flag you pass yourself wins over the suggestion.

## The bracket stops at K

A round with `alive` competitors plays `min(alive // 2, alive - K)` matches, and everyone else gets
a bye. So an odd round has one bye as before, and the round that would overshoot K plays only as
many matches as it takes to land on exactly K. Byes go to the competitors with the fewest byes so
far. 100 with K 10 runs 100 -> 50 -> 25 -> 13 -> 10, the last round being 3 matches and 7 byes.
21 with K 10 runs 21 -> 11 -> 10. 20 with K 10 is one round of 10 matches.

## What it costs

`python3 skills/round-table/bracket.py plan --n 100 --k 10`:

```
      step  alive  matches  byes  sub-agent calls  waves
     spawn    100        -     -              100     10
   round 1    100       50     -              250     25
   round 2     50       25     -              125     13
   round 3     25       12     1               60      8
   round 4     13        3     7               15      3
     pitch     10        -     -               10      1
   table 1     10        -     -               21      3
   table 2     10        -     -               21      3
   table 3     10        -     -               21      3
  red team     10        -     -               13      3
     final      -        -     -                1      1
     total                                     637     73
```

| run | bracket rounds | sub-agent calls (max) | if the table agrees in round 1 |
| --- | --- | --- | --- |
| `--n 100 --k 10`, the default | 4 | 637 | 595 |
| `--n 64 --k 8` | 3 | 415 | 381 |
| `--n 32 --k 6 --table-rounds 2` | 3 | 204 | 191 |
| `--quick` (16, K 10) | 1 | 133 | 91 |
| `--n 16 --k 4 --table-rounds 1` | 2 | 97 | 97 |

Add one call for the baseline check when there is a rejected answer to beat, and one for the
sizer with `--auto`. `plan` also prints the calls per role and per model.

## The tool

```bash
python3 bracket.py plan --n 100 --k 10       # steps, calls, waves. Writes nothing
python3 bracket.py init --n 100 --k 10 --seed 7 --task-file task.md [--baseline-file old.md]
python3 bracket.py next                      # what to do now, the model, the exact command
python3 bracket.py prompts attack            # write every brief, list the jobs and models in waves
python3 bracket.py pairings                  # this round's matches, including the byes
python3 bracket.py collect                   # record the verdicts (or apply the --auto sizing)
python3 bracket.py record r3-m07 a042 --reason "..."   # or record a match by hand
python3 bracket.py advance                   # close the bracket round or the table round
python3 bracket.py status                    # alive and eliminated, the table, what was delivered
python3 bracket.py winner                    # champion, table, final check, delivered answer
```

The run lives in `.round-table/<run>/`: `round-table.json` (the state), `r<n>/` (the bracket),
`table/` (pitches, `draft.md`, `objections.md`, `log.md`, one folder per table round, the red team,
`final.md`), and `solution.md` (the delivered answer).

```bash
python3 -m unittest discover -s tests -v
```

## The fine print

**"100 versions of Claude" means 100 sub-agents.** What makes them different is the card, and the
model per role (`--model-<role>`). The dealer guarantees no two agents get the same card.

**The bracket champion is a proxy.** The bracket stops at K, so there is no single winner; the
champion is the survivor with the highest judge total in the last match it won. Totals from
different judges are only roughly comparable.

**"The best answer" means the one that won the final check.** The judges are Claude too, scoring
against a written rubric in this repo. What you get is the strongest answer this run found, not a
proof that it is right.

**Sub-agents cannot see your chat.** The skill writes a standalone task file, and that file is all
the agents ever know. It is at `.round-table/<run>/task.md`.

**Expect permission prompts unless you allow edits.** Every sub-agent writes a file into
`.round-table/`. Run it in accept-edits mode (Shift+Tab).

## Files

```
skills/round-table/SKILL.md          the orchestration steps and the exact brief every sub-agent gets
skills/round-table/bracket.py        the state machine, standard library only
skills/round-table/strategies.json   15 reasoning modes, 12 workflows, 12 strategies. Edit freely
skills/round-table/rubric.md         the five criteria every judge scores on
tests/test_bracket.py                the tests
```

## License

MIT, see [LICENSE](LICENSE). Original work copyright Jake Schincariol.
