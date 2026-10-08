# round-table: a Claude Code skill for multi-agent brainstorming and consensus answers

A Claude Code skill (plugin) for tasks where the first good answer is not the best one. It runs
many sub-agents on the same task, lets them compete, and merges the best survivors into one answer.
Use it for design decisions, architecture trade-offs, game mechanics and other open questions that
need several approaches combined.

Run N sub-agents on the same task, each with a different strategy card (reasoning mode, workflow,
strategy). They attack each other's solutions in a bracket until K are left. Those K sit down at a
round table: they pitch their strongest ideas, merge them over a few rounds with a scribe who logs
every idea that gets dropped, survive a red-team attack, and finally a blind judge compares the
table's answer with the bracket champion. The better one is delivered.

The bracket gives you a shortlist. The table turns the shortlist into one answer.

## When to use it

- Open design questions with real trade-offs: game mechanics, architecture, product flows.
- Tasks where one good answer is not enough and you want the best of several approaches combined.
- Not for facts or exact logic, where a single careful pass is cheaper and just as reliable.

## Install

In Claude Code:

```
/plugin marketplace add IcaruzSoftware/round-table-skill
/plugin install round-table@round-table
```

Requires Python 3 (stdlib only, no packages).

## Use

```
/round-table <task>
/round-table --auto <task>
/round-table --n 50 --k 6 --table-rounds 2 <task>
```

`--auto` runs one sizing agent first. It reads the task and proposes n, k and table rounds. Its
values are clamped to fixed bounds, and any flag you pass explicitly wins. The chosen values are
printed in one line before the run starts.

## Settings

| Flag | Meaning | Default |
|---|---|---|
| `--n N` | competitors at the start (alias `--agents`) | 100 |
| `--k K` | survivors who sit at the table | 10 |
| `--table-rounds R` | table rounds (max 5) | 3 |
| `--quick` | sets n to 16, keeps k | off |
| `--auto` | sizing agent proposes n, k, table rounds | off |
| `--max-n N` | upper bound for `--auto` (max 2160, the number of strategy cards) | 100 |
| `--seed S` | fixes cards and pairings; random and recorded if omitted | random |
| `--wave W` | sub-agents per wave | 10 |
| `--task-file F` / `--task T` | the task, word for word | |
| `--baseline-file F` | an answer you rejected, for a blind comparison at the end | |
| `--model-<role> M` | model per role: haiku, sonnet or opus | see below |

Model defaults per role:

| Role | Model |
|---|---|
| competitor, defender | haiku |
| attacker, judge, red team, sizer | sonnet |
| table seat, scribe, final check | opus |

Keep n greater than k. k must be at least 2.

## Cost

The default run (n 100, k 10, 3 table rounds) is about 637 sub-agent calls. Run
`bracket.py plan` with your flags to see the exact count before you start. Use `--quick` first to
check how a task behaves.

## How a run works

1. **Tournament.** Single elimination from n down to k. Each round plays `min(alive // 2, alive - k)`
   matches. The rest get a bye, spread across agents with the fewest byes so far.
2. **Pitch.** Each survivor writes a short pitch: its three strongest ideas, the attacks it conceded,
   and what it thinks the others missed.
3. **Table.** Each round, every seat reads the current draft and all pitches, then contributes up to
   five items: ADOPT an idea from another seat, AMEND a section, or OBJECT with a concrete
   counterexample. A scribe merges the contributions into the next draft and logs every rejected
   item with its reason. Seats then vote ACCEPT or OBJECT. The table stops early when no objection
   stands.
4. **Red team.** Two fresh attackers, who never sat at the table, attack the draft. Seats defend
   once, and the scribe writes the final version.
5. **Final check.** A blind judge compares the table's answer with the bracket champion. The better
   one is delivered to `solution.md`.

All sub-agents write only inside the run folder `.round-table/`. The orchestrator never reads
solutions during the run and never picks a winner itself.

## What you get

- `solution.md`: the delivered answer.
- `.round-table/<run>/table/log.md`: every merge, every rejected idea and the reason, and every vote.
- `.round-table/<run>/table/pitches/`: each survivor's pitch.
- The full record of every round: attacks, defenses, verdicts.

## Forked from arena-skill

round-table is a fork of [arena-skill](https://github.com/Jakeschincariol/arena-skill) by Jake
Schincariol, released under the MIT license. The bracket, the strategy cards, the rubric and the
attack/defend/judge loop come from there. The table, the pitch, the scribe, the red team, the final
check, and the `--auto` sizing are new.

## Development

```
python3 -m unittest discover -s tests -v
python3 skills/round-table/bracket.py plan --n 100 --k 10
```

Source: `skills/round-table/bracket.py` (state machine and CLI), `skills/round-table/SKILL.md`
(orchestrator instructions), `skills/round-table/rubric.md` (judging rubric),
`skills/round-table/strategies.json` (strategy cards).

## License

MIT. See [LICENSE](LICENSE).
