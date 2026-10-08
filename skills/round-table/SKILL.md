---
name: round-table
description: >-
  A tournament, then a round table. Spins up N sub-agents (default 100) on the
  exact same task, each with a different strategy card, and runs a
  single-elimination bracket (attack, defend, judge) until K survivors are left
  (default 10). The K survivors pitch their best ideas, then sit at a table for
  up to R rounds: they adopt, amend and object, a scribe merges every round and
  logs every dropped idea, and they vote. Two fresh red-team attackers hit the
  final draft, and a blind judge checks it against the bracket champion, so the
  table never delivers something worse than the best single competitor. Use
  when the user says "round table", asks for a panel or a consensus answer, or
  is not satisfied with an answer and wants many approaches merged.
argument-hint: "[--auto | --n N --k K --table-rounds R | --quick] [--seed S] <task>"
---

# round-table

A tournament finds the strong answers; a table merges them. N sub-agents get the exact same task,
each attacks it with a different reasoning mode, workflow and strategy, and they fight in a bracket
until K are left. Those K pitch, then sit together for a few rounds and build one answer from the
best of each, with a scribe who has to log a reason for every idea that is dropped. A red team
attacks the result, and a blind judge compares it with the bracket champion: whichever is better is
delivered. You are the orchestrator. You never compete, sit at the table, scribe or judge.

What the user typed after `/round-table`: `$ARGUMENTS`

If that is blank, or still reads like a placeholder, nothing was passed: take the task from the
conversation.

## The tool

Every piece of bookkeeping goes through `bracket.py` in this skill's folder:

```bash
python3 "${CLAUDE_SKILL_DIR}/bracket.py" <command>
```

Below, `ROUNDTABLE` means exactly that command. If the path looks unexpanded, use the "Base
directory for this skill" that Claude Code printed at the top of this skill. The state lives in
`.round-table/<run>/round-table.json` in the current directory, and every command after `init`
finds it through `.round-table/LATEST`.

## Step 1: size it, and get a yes if nobody asked for it

Read the flags out of the request. Everything that is not a flag is the task.

| flag | meaning |
| --- | --- |
| `--n N` (alias `--agents N`) | N competitors in the tournament. Default 100. Must be more than K. |
| `--k K` | K survivors sit at the table. Default 10, at least 2. |
| `--table-rounds R` | Table rounds. Default 3, at most 5. The table stops early when no objection is standing. |
| `--quick` | 16 competitors, K unchanged. The everyday setting. |
| `--auto` | A sizer sub-agent reads the task and picks N, K and R. No confirmation step. |
| `--max-n M` | The cap on N for `--auto`. Default 100, at most 2160 (the distinct cards). |
| `--seed S` | Fixes the cards and the pairings. Default: random, and recorded. |
| `--wave W` | Sub-agents per wave. Default 10. Only raise it if the user raised Claude Code's limit. |
| `--model-<role> M` | haiku, sonnet or opus per role. Defaults: competitor haiku, defender haiku, attacker sonnet, judge sonnet, seat opus, scribe opus, redteam sonnet, final opus, sizer sonnet. |

No task text at all means: the task is the user's most recent request in this conversation, and your
last answer to it is the baseline to beat.

Run `ROUNDTABLE plan` with the same flags. It prints every step, the sub-agent calls and the waves.

- **`--auto`**: do not ask, do not wait. The user asked for an automatic run. Go to step 2.
- **The user asked for the round table** (typed `/round-table`, said "round table", asked for a
  panel): tell them in one line how big it is, for example "100 competitors, 10 at the table, up to
  637 sub-agent calls", and start.
- **This skill fired because the user is unhappy** and never mentioned the round table: ask once
  before spending anything. Offer the full run, `--quick`, or an ordinary retry. Wait for the answer.

Sub-agents write their work into `.round-table/` in the current directory. In the default permission
mode that is one approval per file, which is hundreds on a big run. Before the first wave, suggest
accept-edits mode (Shift+Tab) for the run. Do not change the user's settings yourself.

## Step 2: write the task file

This is the step that decides the result. **Sub-agents cannot see this conversation.** Every
competitor, seat, scribe and judge knows only what is in the task file, so write
`.round-table/task.md` to stand on its own:

- The request, in the user's own words where you can.
- Every requirement and constraint the user stated anywhere in the conversation: audience, length,
  format, tone, stack, deadline, what must not change.
- The context a stranger would need: absolute paths of the files that matter, pasted data, what the
  product is, the conventions in the codebase.
- What "done" looks like, if the user said.
- If there is an answer to beat: what the user disliked about it, in their words.

Do not add requirements the user never gave. Do not write your own view of the right answer into it:
that pushes every agent the same way, which is the opposite of the point.

If there is an earlier answer the user was not satisfied with, write it word for word to
`.round-table/baseline.md`.

## Step 3: init

```bash
ROUNDTABLE init --n N --k K --table-rounds R --seed S --task-file .round-table/task.md --baseline-file .round-table/baseline.md
```

Pass only the flags the user gave, plus `--task-file`. Leave out `--baseline-file` when there is
nothing to beat. Pass every `--model-<role>` the user named. `init` copies the task into the run
folder, deals every competitor a different strategy card with no repeats, pairs round 1, and writes
`round-table.json`.

With `--auto`, `init` deals nothing yet. The first step `next` shows is **size**: one sizer job.
After it, `ROUNDTABLE collect` checks the sizer's JSON, clamps it to the guardrails (N 8 to
`--max-n`, K 2 to min(10, N / 2), R 1 to 5), lets every flag the user passed win, deals the cards
and prints the chosen settings and the reason in one line. Show that line to the user and carry on.

## Step 4: the loop

Always drive it with `ROUNDTABLE next`. It reads the state on disk and tells you the next step, the
model, and the exact command.

Every phase that runs sub-agents works the same way:

1. `ROUNDTABLE prompts <phase>` writes one brief per job and lists the jobs still to run, in waves,
   each with its model.
2. Launch **one wave at a time**: a single message with one Agent tool call per job in that wave (the
   Agent tool is called Task in older Claude Code versions). Each call is:
   - `subagent_type`: `general-purpose`
   - `model`: the model `prompts` lists for that job (haiku, sonnet or opus). Always pass it.
   - `description`: `round-table <job id>`
   - `prompt`: `Read <prompt path> and follow it exactly. It is your whole brief.`
   - `run_in_background`: false, where the tool has that option, so the wave comes back together.

   Wait until every agent in the wave has replied before you launch the next wave.
3. After the last wave, run `ROUNDTABLE next`. If an output is missing it sends you back to the same
   phase, and `prompts` then lists only the missing jobs. Re-run those once. If a job fails twice,
   write the single line `NO OUTPUT` into each of its output files (`ROUNDTABLE check <phase>` lists
   them) and move on. A missing attack counts as no attacks. A missing solution loses its match. A
   missing vote is an abstention. A judge, scribe or sizer that fails twice gets a third, fresh run:
   never decide a match, merge a draft or pick the sizes yourself.

The order `next` takes you through:

- **size**, once, only with `--auto`, then `ROUNDTABLE collect`.
- **spawn**, once: every competitor writes its own solution to the task.
- **tournament**, every round: **attack** (two per match) → **defend** (two per match) → **judge**
  (one per match) → `ROUNDTABLE collect` → `ROUNDTABLE advance`. The bracket stops at K: the last
  round plays only as many matches as it takes to get to exactly K, and the rest get a bye.
- **pitch**, once: each of the K survivors writes a pitch, max 300 words.
- **table**, every table round: **contribute** (one per seat) → **scribe** (one) → **vote** (one per
  seat) → `ROUNDTABLE advance`. It stops early when no objection is standing and at least half
  the seats cast a real vote. Objections still standing at the end go to the red team and the scribe.
- **redteam** (two fresh attackers) → **rebut** (one per seat) → **finalize** (the scribe).
- **final**: a blind judge compares the table's final draft with the bracket champion. Then
  `ROUNDTABLE collect`, which copies the better one to `<run>/solution.md`.
- **baseline**, only when there is an answer to beat: a blind judge compares the delivered answer
  with it. Then `ROUNDTABLE collect`.
- `next` prints DONE: go to step 5.

After each `advance`, give the user one line, such as "Round 2 done: 25 of 100 left." or "Table
round 1 done: 2 objections standing." Nothing more. Never paste pairings, attacks, contributions,
votes, verdicts or solutions into the chat.

Why waves: Claude Code runs at most 10 tool calls at once by default (the
`CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY` setting). `plan` prints the wave count for the whole run.

## Step 5: the result

Run `ROUNDTABLE winner`, then read `<run>/solution.md`. That is the only solution file you read in
the whole run. Give the user:

1. **The delivered solution**, in full.
2. **Where it came from**: the table or the bracket champion, and the final check's scores from
   `winner`, honestly. If the champion beat the table, say so.
3. **What it survived**: the champion's attacks from `winner`, and the table rounds (and whether it
   stopped early). The full log of accepted and dropped ideas is `<run>/table/log.md`.
4. **Against the answer you rejected**, if there was one: the baseline check's scores. If the old
   answer scored higher, say so plainly and show both.
5. Where the full record lives: the run folder.

If the solution changes files in the user's project, do not apply it. Ask: apply it, or change it?

## Rules for the orchestrator

- You run the tournament and the table. You do not compete, attack, sit, scribe or judge, and you
  never pick a winner. `collect` records what the judges decided. `record` is only for fixing
  bookkeeping when the user asks you to.
- Every sub-agent gets the task through its brief, which `prompts` builds from the one task file,
  byte for byte the same for everyone. Never paraphrase the task for one agent or add a hint to one
  agent's call.
- Do not read solutions, attacks, pitches, contributions, votes or verdicts during the run. The state
  is on disk, and `next`, `status` and `pairings` are all you need.
- If your context gets compacted mid-run, nothing is lost. Run `ROUNDTABLE status`, then
  `ROUNDTABLE next`, and carry on.
- Run every `ROUNDTABLE` command from the directory you ran `init` in. That is where
  `.round-table/LATEST` lives.
- Sub-agents only write inside `.round-table/`. If one wrote anywhere else, tell the user.
- If the user says stop, stop. `ROUNDTABLE status` shows where it got to, and `ROUNDTABLE next`
  resumes it later.

## The prompt templates

`bracket.py` fills these in (the `{{placeholders}}`) and writes one brief per job, so what you see
here is exactly what every sub-agent is told. Never edit a brief for a single agent.

### Competitor, in the spawn phase

<!-- template:competitor -->
```text
You are competitor {{agent}} in a tournament of {{n}}. All {{n}} competitors got the exact same task, word for word. The only thing that makes you different is the strategy card below: it decides how you attack the task. Your solution will be attacked by other competitors and scored by a judge, round after round, until only {{k}} are left. Those {{k}} then sit at a round table and merge their best ideas into one answer.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

{{baseline_note}}

=== YOUR STRATEGY CARD ===
Reasoning mode: {{reasoning_name}}. {{reasoning_how}}
Workflow: {{workflow_name}}. {{workflow_how}}
Strategy: {{strategy_name}}. {{strategy_how}}
=== END OF THE CARD ===

How to work:
1. Use the card for real. Think in the reasoning mode, go through the workflow's steps in order, and let the strategy settle every trade-off. A generic answer with the card's name on top will lose.
2. Meet every requirement the task states. The judge scores you against the task, not against your card.
3. You cannot ask the user anything. Where the task is ambiguous, take the most reasonable reading and state it in a short Assumptions section.
4. Expect attacks: concrete flaws, counterexamples, missed requirements. Close those holes before you submit.
5. Do not create, edit or delete anything outside {{run_dir}}. Read whatever the task points to. If the task is about code, put the exact changes in your solution (full files or a unified diff) instead of applying them. If your workflow needs scratch space, use {{run_dir}}/scratch/{{agent}}/.

Write your solution to {{out}}: the solution itself, written for the person who asked. Leave out your drafts and your working. Keep a checklist, tests or trade-off notes only where they help that person use the answer. Say nothing about the tournament, your card or your competitor number: the judges score the work blind.

When the file is written, reply with this one line and nothing else:
DONE {{agent}} <number of words in your solution>
```
<!-- /template:competitor -->

### Attacker, in every round

<!-- template:attacker -->
```text
You are competitor {{agent}} in round {{round}} of a tournament, match {{match}}. Your opponent is {{target}}. Only one of you gets out of this match. Right now your job is to attack your opponent's solution.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

Your strategy card is the lens you look for flaws through:
Reasoning mode: {{reasoning_name}}. {{reasoning_how}}
Workflow: {{workflow_name}}. {{workflow_how}}
Strategy: {{strategy_name}}. {{strategy_how}}

Read your opponent's solution: {{target_solution}}
You may read your own for comparison: {{own_solution}}. Attack theirs on its merits against the task, not for being different from yours.

Find the real problems:
- WRONG: factual errors, logic errors, bugs, false claims.
- MISSING: a requirement the task states that it skips or only half meets. Quote the requirement.
- BREAKS: a concrete input, scenario or edge case where it fails. Give the exact counterexample.
- VAGUE: a place where the user could not act on it without guessing.

Rules:
- Every attack must be specific and checkable: point at the exact part, say what is wrong and why.
- No praise, no summary, and no style nitpicks unless they stop the user from using it.
- Do not invent requirements the task does not state. Do not attack the approach, only what it gets wrong.
- At most 7 attacks, strongest first. If you only find 2 real ones, write 2.
- Label each FATAL (wrong or unusable for the task), MAJOR (a real gap) or MINOR.
- Do not create, edit or delete any file except the one below.

Write the attacks to {{out}} in this format:
ATTACK 1 [FATAL|MAJOR|MINOR] <one-line title>
Where: <quote or location>
Problem: <what is wrong, with the counterexample or the missed requirement>
(and the same for each attack after that)

When the file is written, reply with this one line and nothing else:
ATTACKED {{target}} <number of attacks> (<number that are FATAL> fatal)
```
<!-- /template:attacker -->

### Defender, in every round

<!-- template:defender -->
```text
You are competitor {{agent}} in round {{round}} of a tournament, match {{match}}. Your opponent {{attacker}} has attacked your solution. Now you defend it and revise it. A judge will score your revised solution against your opponent's, including how well each of you dealt with the attacks you took.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

Your strategy card. Keep your approach: it is why you are still here.
Reasoning mode: {{reasoning_name}}. {{reasoning_how}}
Workflow: {{workflow_name}}. {{workflow_how}}
Strategy: {{strategy_name}}. {{strategy_how}}

Your current solution: {{own_solution}}
The attacks against it: {{attacks}}

Do this:
1. Take every attack in turn and decide honestly. CONCEDE if it is right, and fix it. REBUT if it is wrong, and show why with evidence from the task, your solution or a concrete check. A rebuttal that only insists you are right counts as a concession. Conceding a real flaw and fixing it scores better than defending it.
2. Write your revised solution: the complete solution, standalone, with every conceded point fixed. The judge reads only this file, so never write "see the previous version". Say nothing about the tournament or your card.
3. Fix what was attacked and anything the attacks made you notice. Do not start again from scratch and do not copy your opponent.
4. If the attacks file is empty or says NO OUTPUT, you were not attacked: write NO ATTACKS RECEIVED as your defense, and resubmit your solution with only the fixes you know it needs.
5. Do not create, edit or delete anything outside {{run_dir}}.

Write your point-by-point defense to {{defense_out}} in this format:
ATTACK 1: CONCEDE|REBUT. <one to three lines>
(one entry per attack)

Write your revised solution to {{solution_out}}.

When both files are written, reply with this one line and nothing else:
DEFENDED {{agent}} conceded <n> rebutted <n>
```
<!-- /template:defender -->

### Judge, one per match

<!-- template:judge -->
```text
You are the judge of match {{match}}, round {{round}}, in a tournament. Two solutions to the same task have fought: each attacked the other, then defended and revised its own. Score both against the rubric. The one with the higher score goes through and the other is eliminated.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

Read the rubric first: {{rubric}}

Solution {{first}}
- revised solution: {{first_solution}}
- attacks it received: {{first_attacks}}
- its defense: {{first_defense}}

Solution {{second}}
- revised solution: {{second_solution}}
- attacks it received: {{second_attacks}}
- its defense: {{second_defense}}

How to judge:
1. Read both revised solutions in full before you score either one.
2. For every attack, check the revised solution yourself and call it FIXED, REBUTTED (only if the rebuttal is actually right) or STANDING. A defense that says "fixed" is not proof. Look.
3. Look for flaws the attackers missed, too.
4. Score each criterion from 0 to 10 using the rubric's anchors. Set fatal to true only for a flaw you have verified that makes the solution wrong or unusable for the task.
5. The winner is the higher weighted total (the weights are in the rubric). A fatal solution cannot beat one that is not fatal. On an exact tie, fewer standing attacks wins, then higher correctness.
6. Judge the work, not the writing about the work. Length is not quality. You do not know either competitor's strategy and should not guess it.
7. Do not create, edit or delete any file except the verdict. If the task is code and running something settles an attack, do it only inside {{run_dir}}/scratch/judge-{{match}}/, never in the user's project.

Write this JSON, and nothing else, to {{out}}:
{
  "match": "{{match}}",
  "scores": {
    "{{first}}": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false},
    "{{second}}": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false}
  },
  "winner": "{{first}} or {{second}}",
  "reason": "one sentence: the decisive difference",
  "survived": ["each attack the winner took and beat, in a few words"],
  "standing": {"{{first}}": ["attacks still standing"], "{{second}}": ["attacks still standing"]}
}

When the file is written, reply with this one line and nothing else:
WINNER <winner id> <winner total>-<loser total>
```
<!-- /template:judge -->

### Sizer, only with --auto

<!-- template:sizer -->
```text
You size a round table before it starts. A round table runs N competitors on one task through a single-elimination bracket until K survivors are left; those K then merge their work at a table for R rounds. Bigger N finds more distinct approaches but costs more: roughly 5 sub-agent calls per eliminated competitor, plus 2K+1 calls per table round. You do not solve the task.

=== THE TASK ===
{{task}}
=== END OF THE TASK ===

Pick the smallest settings that fit the task:
- n: {{min_n}} to {{max_n}}. Small, well-defined tasks need few competitors; open-ended, high-stakes or many-sided tasks need more.
- k: 2 to 10, and at most n / 2. More seats when good answers will differ in many independent parts that can be combined.
- table_rounds: 1 to {{max_rounds}}. More rounds when the parts interact and need negotiation.
If you cannot tell, use n {{default_n}}, k {{default_k}}, table_rounds {{default_rounds}}.

Do not create, edit or delete any file except the one below.

Write this JSON, and nothing else, to {{out}}:
{"n": 0, "k": 0, "table_rounds": 0, "reason": "one line: why these sizes fit this task"}

When the file is written, reply with this one line and nothing else:
SIZED <n> <k> <table_rounds>
```
<!-- /template:sizer -->

### Pitch, once per survivor

<!-- template:pitch -->
```text
You are {{agent}}, one of the {{k}} survivors of a tournament of {{n}} on the task below. You now take a seat at a round table, where the {{k}} of you build one answer together from the best of each. First you pitch: tell the others what you bring.

=== THE TASK (identical for every competitor) ===
{{task}}
=== END OF THE TASK ===

Your solution, as it stands after the tournament: {{own_solution}}
Your matches, with the attacks you took and how you defended:
{{history}}

Write a pitch of at most 300 words with exactly these three sections:
STRONGEST IDEAS: your three strongest ideas, each one concrete enough that another seat could adopt it as written. Name the section of your solution it lives in.
CONCEDED AND FIXED: the attacks you conceded and exactly how you fixed each one.
LIKELY MISSED: what the other survivors most likely missed, and why it matters for the task.

Rules: no praise of yourself, no summary of the task, nothing about your strategy card. Do not create, edit or delete any file except the one below.

Write the pitch to {{out}}.

When the file is written, reply with this one line and nothing else:
PITCHED {{agent}} <number of words>
```
<!-- /template:pitch -->

### Seat, every table round

<!-- template:seat -->
```text
You are seat {{agent}} at a round table of {{k}}, table round {{round}} of at most {{table_rounds}}. The table is building one answer to the task below. A scribe merges what every seat contributes into the next draft.

=== THE TASK (identical for every seat) ===
{{task}}
=== END OF THE TASK ===

Read, in this order:
- the current draft: {{draft}}
- the open objections from the last vote: {{objections}}
- every seat's pitch, yours included, in: {{pitches}} (yours is {{own_pitch}})
- the log of what the scribe accepted and dropped so far: {{log}}

Then write at most 5 items, strongest first. Each item is exactly one of:
ADOPT <seat id>: <the idea from another seat's pitch>. Reason: <why the draft is better with it, against the task>
AMEND "<section name in the draft>": <the full replacement text for that section>. Reason: <what it fixes>
OBJECT "<section name in the draft>": <a concrete counterexample: the input, scenario or requirement where the draft is wrong>. A vague objection ("could be clearer", "might not scale") will be rejected.

Rules:
- Keep your strongest idea. If the draft dropped it and the log gives no proven objection against it, AMEND it back in. Give it up only if an objection against it is proven.
- Do not repeat an item the log already rejected unless you answer the logged reason.
- Judge against the task, not against your own solution. Adopting another seat's better idea is a strength.
- Do not create, edit or delete any file except the one below.

Write your items to {{out}} in this format:
ITEM 1 ADOPT|AMEND|OBJECT ...
(one block per item)

When the file is written, reply with this one line and nothing else:
CONTRIBUTED {{agent}} <number of items>
```
<!-- /template:seat -->

### Scribe, every table round

<!-- template:scribe -->
```text
You are the scribe of a round table of {{k}} seats, table round {{round}}. You do not have opinions of your own about the answer: you merge what the seats contributed into the next draft, and you account for every item.

=== THE TASK (identical for every seat) ===
{{task}}
=== END OF THE TASK ===

Read:
- the current draft: {{draft}}
- the open objections from the last vote: {{objections}}
- every seat's pitch in: {{pitches}}
- the log so far: {{log}}
- this round's contributions:
{{contributions}}

How to merge:
1. Take every item in every contribution and decide ACCEPTED or REJECTED, with a reason that refers to the task. An OBJECT is accepted only if its counterexample actually holds against the draft; then fix the draft. A vague objection is rejected as vague.
2. Where two items conflict, keep the one that serves the task better and log why the other lost.
3. No idea may disappear without a logged reason. If a sentence, step or idea that was in the draft or in a pitch is not in the new draft, it must be in the log as DROPPED with the reason, and the seat whose idea it was.
4. A seat's strongest idea stays unless an objection against it is proven. "Another seat preferred something else" is not proof.
5. The new draft is the complete answer to the task, standalone, written for the person who asked. Say nothing about the table, the seats or the tournament in it.
6. Do not create, edit or delete any file except the two below.

Write the new draft to {{draft_out}}.

Write the log for this round to {{log_out}} in this format:
## Table round {{round}}
ACCEPTED <seat id> <ADOPT|AMEND|OBJECT> <short title>: <reason>
REJECTED <seat id> <ADOPT|AMEND|OBJECT> <short title>: <reason>
DROPPED <idea, in a few words> (from <seat id>): <reason, and the proven objection behind it>
(one line per item, every item from every contribution)

When both files are written, reply with this one line and nothing else:
MERGED round {{round}} accepted <n> rejected <n> dropped <n>
```
<!-- /template:scribe -->

### Vote, every table round

<!-- template:vote -->
```text
You are seat {{agent}} at a round table of {{k}}. The scribe has merged table round {{round}} into a new draft. Now you vote on it.

=== THE TASK (identical for every seat) ===
{{task}}
=== END OF THE TASK ===

Read:
- the new draft: {{new_draft}}
- the scribe's log for this round: {{round_log}}
- your pitch: {{own_pitch}}

Vote ACCEPT if the draft answers the task and nothing in it is wrong. Vote OBJECT with exactly one objection if something is: the most important one, as a concrete counterexample (the input, scenario or requirement where the draft fails), or a strong idea of yours the log dropped without a proven reason. Style preferences are not objections.

Do not create, edit or delete any file except the one below.

Write your vote to {{out}}. The file starts with the word ACCEPT or the word OBJECT. After OBJECT, the objection on the same line or the next ones:
ACCEPT
or
OBJECT "<section name>": <the concrete counterexample>

When the file is written, reply with this one line and nothing else:
VOTED {{agent}} ACCEPT|OBJECT
```
<!-- /template:vote -->

### Red-team attacker, twice after the table

<!-- template:redteam -->
```text
You are red-team attacker {{attacker}}. A round table of {{k}} built one answer to the task below. You never sat at that table and owe it nothing. Your job is to break the answer.

=== THE TASK ===
{{task}}
=== END OF THE TASK ===

Read the answer: {{draft}}
You may read the rubric it will be judged on: {{rubric}}

Your lens: {{lens}}

Objections still standing from the table: {{objections}}
Check every standing objection first. If it still holds against the answer, it is an attack: write it up in the format below, with its counterexample. Then look for new problems.

Find the real problems:
- WRONG: factual errors, logic errors, bugs, false claims.
- MISSING: a requirement the task states that it skips or only half meets. Quote the requirement.
- BREAKS: a concrete input, scenario or edge case where it fails. Give the exact counterexample.
- VAGUE: a place where the user could not act on it without guessing.

Rules:
- Every attack must be specific and checkable: point at the exact part, say what is wrong and why.
- No praise, no summary, and no style nitpicks unless they stop the user from using it.
- Do not invent requirements the task does not state.
- At most 7 attacks, strongest first. Label each FATAL, MAJOR or MINOR.
- Do not create, edit or delete any file except the one below.

Write the attacks to {{out}} in this format:
ATTACK 1 [FATAL|MAJOR|MINOR] <one-line title>
Where: <quote or location>
Problem: <what is wrong, with the counterexample or the missed requirement>

When the file is written, reply with this one line and nothing else:
ATTACKED {{attacker}} <number of attacks> (<number that are FATAL> fatal)
```
<!-- /template:redteam -->

### Rebuttal, once per seat after the red team

<!-- template:rebut -->
```text
You are seat {{agent}} at a round table of {{k}}. Two red-team attackers who never sat at the table have attacked the table's answer. You defend it, in one round. The scribe then writes the final version from every seat's defense.

=== THE TASK (identical for every seat) ===
{{task}}
=== END OF THE TASK ===

The table's answer: {{draft}}
The attacks:
{{attacks}}
Your pitch, for what you know best: {{own_pitch}}

For every attack, decide honestly:
CONCEDE: it is right. Give the exact fix: the section and its full replacement text.
REBUT: it is wrong. Show why with evidence from the task, the answer or a concrete check. A rebuttal that only insists counts as a concession.
Conceding a real flaw and fixing it is worth more than defending it.

Do not create, edit or delete any file except the one below.

Write your defense to {{out}} in this format:
rt<n> ATTACK <m>: CONCEDE|REBUT. <the fix, or the evidence>
(one entry per attack, from both attackers)

When the file is written, reply with this one line and nothing else:
DEFENDED {{agent}} conceded <n> rebutted <n>
```
<!-- /template:rebut -->

### Scribe, the final version

<!-- template:finalize -->
```text
You are the scribe of a round table of {{k}} seats. The table's answer was attacked by two red-team attackers, and every seat has defended it. Write the final version.

=== THE TASK ===
{{task}}
=== END OF THE TASK ===

Read:
- the table's answer: {{draft}}
- the attacks:
{{attacks}}
- the seats' defenses:
{{defenses}}
- the table log so far: {{log}}
- Objections still standing from the table: {{objections}}

How to finish:
1. For every attack, check it against the answer yourself and decide FIXED (apply the best fix the seats proposed, or your own if none works), REBUTTED (only if a seat's rebuttal actually holds) or STANDING (you could not fix it; say why).
2. Treat every standing objection like an attack: if it still holds, fix it, and log it as FIXED, REBUTTED or STANDING with "objection <seat id>" in place of "rt<n> ATTACK <m>".
3. Change nothing the attacks did not touch, unless a fix forces it. No idea disappears without a logged reason.
4. The final version is the complete answer, standalone, written for the person who asked. Say nothing about the table, the red team or the tournament in it.
5. Do not create, edit or delete any file except the two below.

Write the final version to {{final_out}}.

Write the log to {{log_out}} in this format:
## Red team
FIXED rt<n> ATTACK <m> <short title>: <what changed>
REBUTTED rt<n> ATTACK <m> <short title>: <which seat, and why it holds>
STANDING rt<n> ATTACK <m> <short title>: <why it could not be fixed>
DROPPED <idea> (from <seat id or the draft>): <reason>

When both files are written, reply with this one line and nothing else:
FINAL fixed <n> rebutted <n> standing <n>
```
<!-- /template:finalize -->

### Final check, table against champion

<!-- template:final -->
```text
You are the final check of a round table. {{n}} competitors fought over one task in a {{rounds}}-round bracket. One of the two solutions below is the best single competitor's answer; the other was built by {{k}} finalists at a table over {{table_rounds_played}} round(s) and then attacked by a red team. You are not told which is which. Score what is in front of you. Either one can win.

=== THE TASK ===
{{task}}
=== END OF THE TASK ===

Read the rubric first: {{rubric}}

Solution X: {{x_solution}}
Solution Y: {{y_solution}}

How to judge:
1. Read both in full before you score either.
2. Attack both yourself: find the strongest concrete flaws in each, the way a hostile expert would. For the robustness score, judge how well each one holds up against those attacks.
3. Score each criterion from 0 to 10 using the rubric's anchors. Set fatal to true only for a flaw you have verified that makes a solution wrong or unusable for the task.
4. The winner is the higher weighted total. A fatal solution cannot beat one that is not fatal.
5. Judge the work, not the writing about the work. Length is not quality, and neither is having more ideas in it.
6. Do not create, edit or delete any file except the verdict.

Write this JSON, and nothing else, to {{out}}:
{
  "scores": {
    "X": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false},
    "Y": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false}
  },
  "winner": "X or Y",
  "reason": "one sentence: the decisive difference",
  "fixed": ["each thing the winner gets right that the other gets wrong, in a few words"]
}

When the file is written, reply with this one line and nothing else:
FINAL <X or Y> <X total>-<Y total>
```
<!-- /template:final -->

### Baseline check, only when there is an answer to beat

<!-- template:baseline -->
```text
You are the last check of a round table. {{n}} competitors fought over one task and the best answer came out of a tournament and a table. Before it goes back to the user, it is compared with the answer the user already rejected. You are not told which of the two is which. Score what is in front of you. Either one can win.

=== THE TASK ===
{{task}}
=== END OF THE TASK ===

Read the rubric first: {{rubric}}

Solution X: {{x_solution}}
Solution Y: {{y_solution}}

How to judge:
1. Read both in full before you score either.
2. Attack both yourself: find the strongest concrete flaws in each, the way a hostile expert would. For the robustness score, judge how well each one holds up against those attacks.
3. Score each criterion from 0 to 10 using the rubric's anchors. Set fatal to true only for a flaw you have verified that makes a solution wrong or unusable for the task.
4. The winner is the higher weighted total. A fatal solution cannot beat one that is not fatal.
5. Judge the work, not the writing about the work. Length is not quality.
6. Do not create, edit or delete any file except the verdict.

Write this JSON, and nothing else, to {{out}}:
{
  "scores": {
    "X": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false},
    "Y": {"correctness": 0, "completeness": 0, "specificity": 0, "robustness": 0, "clarity": 0, "fatal": false}
  },
  "winner": "X or Y",
  "reason": "one sentence: the decisive difference",
  "fixed": ["each thing the winner gets right that the other gets wrong, in a few words"]
}

When the file is written, reply with this one line and nothing else:
FINAL <X or Y> <X total>-<Y total>
```
<!-- /template:baseline -->
