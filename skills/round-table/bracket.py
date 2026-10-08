#!/usr/bin/env python3
"""bracket.py: the state machine behind /round-table.

A round table runs in five phases, all of it in one JSON file (round-table.json), so the
orchestrator never loses track of who is alive, who sits at the table and what happens next,
even if its own context gets compacted halfway through a long run.

  1. tournament: N competitors fight a single-elimination bracket that stops at K survivors
  2. pitch: each of the K survivors pitches its strongest ideas
  3. table: R rounds of contribute (ADOPT / AMEND / OBJECT), scribe merge, vote
  4. red team: two fresh attackers hit the table's draft, the seats defend, the scribe finalizes
  5. final check: a blind judge compares the table's draft with the bracket champion

    python3 bracket.py plan --n 100 --k 10           # steps, sub-agent calls and waves. Writes nothing.
    python3 bracket.py init --n 100 --k 10 --seed 7 --task-file task.md [--baseline-file old.md]
    python3 bracket.py init --quick --task "..."     # 16 competitors, k unchanged
    python3 bracket.py init --auto --task-file task.md   # a sizer sub-agent picks n, k and table rounds
    python3 bracket.py next                          # what to do now, and the exact command for it
    python3 bracket.py prompts <phase>               # write the sub-agent briefs, list the jobs left, in waves
    python3 bracket.py check <phase>                 # which outputs are still missing
    python3 bracket.py pairings                      # this round's matches, including the byes
    python3 bracket.py collect                       # apply the sizing, or read the judges' verdicts
    python3 bracket.py record <match_id> <winner_id> [--reason "..."]
    python3 bracket.py advance                       # close the bracket round or the table round
    python3 bracket.py status                        # alive and eliminated, per round
    python3 bracket.py winner                        # the champion, the table, what was delivered
    python3 bracket.py card <agent_id>               # one competitor's strategy card

Phases, in order: size (only with --auto), spawn, then per bracket round attack, defend,
judge; then pitch, per table round contribute, scribe, vote; then redteam, rebut, finalize;
then final, and baseline (only when there is a rejected answer to beat). Every command takes
--dir; without it, the run named in .round-table/LATEST is used.

Python 3.8+, standard library only.
"""
import argparse
import json
import math
import os
import random
import re
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.abspath(__file__)
STRATEGIES_PATH = os.path.join(HERE, "strategies.json")
SKILL_PATH = os.path.join(HERE, "SKILL.md")
RUBRIC_PATH = os.path.join(HERE, "rubric.md")

# ---------------------------------------------------------------- settings
# Every default lives here. The CLI flag of the same name overrides it.

DEFAULT_N = 100            # --n (alias --agents): competitors in the tournament
QUICK_N = 16               # --quick: n becomes 16, k stays
DEFAULT_K = 10             # --k: survivors who sit at the table
DEFAULT_TABLE_ROUNDS = 3   # --table-rounds
MAX_TABLE_ROUNDS = 5
DEFAULT_WAVE = 10          # --wave. Claude Code runs at most 10 tool calls at once by default
MODEL_CHOICES = ("haiku", "sonnet", "opus")
DEFAULT_MODELS = {         # --model-<role>
    "competitor": "haiku",
    "defender": "haiku",
    "attacker": "sonnet",
    "judge": "sonnet",
    "seat": "opus",
    "scribe": "opus",
    "redteam": "sonnet",
    "final": "opus",
    "sizer": "sonnet",
}
# --auto guardrails: the sizer's suggestion is clamped to these.
AUTO_MIN_N = 8
DEFAULT_MAX_N = 100        # --max-n
AUTO_MAX_K = 10            # and never more than n // 2
REDTEAM_LENSES = (
    "correctness: wrong claims, logic errors, bugs, and the concrete input or scenario that breaks it",
    "the user: requirements the task states that it misses or half meets, and places where the user could not act without guessing",
)

ROOT = ".round-table"
LATEST = "LATEST"
STATE_FILE = "round-table.json"
PHASES = ("size", "spawn", "attack", "defend", "judge", "pitch", "contribute", "scribe", "vote",
          "redteam", "rebut", "finalize", "final", "baseline")
KIND_OF = {"size": "sizer", "spawn": "competitor", "attack": "attacker", "defend": "defender",
           "judge": "judge", "pitch": "pitch", "contribute": "seat", "scribe": "scribe", "vote": "vote",
           "redteam": "redteam", "rebut": "rebut", "finalize": "finalize", "final": "final",
           "baseline": "baseline"}
# Which --model-<role> runs each kind of job. The seats pitch, contribute, vote and rebut.
ROLE_OF = {"sizer": "sizer", "competitor": "competitor", "attacker": "attacker", "defender": "defender",
           "judge": "judge", "pitch": "seat", "seat": "seat", "vote": "seat", "rebut": "seat",
           "scribe": "scribe", "finalize": "scribe", "redteam": "redteam", "final": "final",
           "baseline": "final"}
TEMPLATES = tuple(KIND_OF.values())
CALLS_PER_MATCH = 5        # 2 attacks, 2 defenses, 1 judge

# Mirrors the table in rubric.md. tests/test_bracket.py checks they agree.
WEIGHTS = (
    ("correctness", 30),
    ("completeness", 25),
    ("specificity", 15),
    ("robustness", 20),
    ("clarity", 10),
)


class RoundTableError(Exception):
    pass


# ---------------------------------------------------------------- the cards

def load_strategies(path=STRATEGIES_PATH):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for key in ("reasoning", "workflows", "strategies"):
        items = data.get(key) or []
        if not items:
            raise RoundTableError("strategies.json has no '%s' entries" % key)
        ids = [it["id"] for it in items]
        if len(set(ids)) != len(ids):
            raise RoundTableError("strategies.json has a duplicate id in '%s'" % key)
        for it in items:
            if not it.get("name") or not it.get("how"):
                raise RoundTableError("strategies.json entry '%s' needs a name and a how" % it["id"])
    return data


def combo_count(data):
    return len(data["reasoning"]) * len(data["workflows"]) * len(data["strategies"])


def _edge_colour(edges, k):
    """Properly edge-colour a bipartite (multi)graph with k colours, then balance it.

    edges are (row, col) pairs and k must be at least the largest degree. Proper
    means no two edges at the same row, or at the same col, share a colour. Every
    bipartite graph has one (Konig), found here with alternating-path swaps. Then
    de Werra's swaps even the colour classes out until each is used floor or ceil
    of len(edges) / k times.
    """
    colour = [None] * len(edges)
    at = ({}, {})

    def slot(side, node):
        return at[side].setdefault(node, {})

    def put(e, c):
        u, v = edges[e]
        colour[e] = c
        slot(0, u)[c] = e
        slot(1, v)[c] = e

    def take(e):
        u, v = edges[e]
        del slot(0, u)[colour[e]]
        del slot(1, v)[colour[e]]

    def walk(side, node, first, other):
        path, c = [], first
        while True:
            e = slot(side, node).get(c)
            if e is None or e in path:
                return path
            path.append(e)
            node = edges[e][1 - side]
            side = 1 - side
            c = other if c == first else first

    def swap(path, a, b):
        new = {e: (b if colour[e] == a else a) for e in path}
        for e in path:
            take(e)
        for e in path:
            put(e, new[e])

    for e, (u, v) in enumerate(edges):
        free_u = [c for c in range(k) if c not in slot(0, u)]
        free_v = set(c for c in range(k) if c not in slot(1, v))
        c = next((c for c in free_u if c in free_v), None)
        if c is None:
            a, b = free_u[0], min(free_v)
            swap(walk(1, v, a, b), a, b)
            c = a
        put(e, c)

    while True:
        count = [0] * k
        for c in colour:
            count[c] += 1
        hi = max(range(k), key=lambda c: (count[c], -c))
        lo = min(range(k), key=lambda c: (count[c], c))
        if count[hi] - count[lo] <= 1:
            return colour
        swapped = False
        for side in (0, 1):
            for node in sorted(at[side]):
                s = at[side][node]
                if hi in s and lo not in s:
                    path = walk(side, node, hi, lo)
                    if sum(colour[e] == hi for e in path) > sum(colour[e] == lo for e in path):
                        swap(path, hi, lo)
                        swapped = True
                        break
            if swapped:
                break
        if not swapped:
            raise RoundTableError("could not balance the strategy cards")


def deal(n, seed, data):
    """Deal n distinct (reasoning, workflow, strategy) index triples, one per agent.

    Deterministic for a given seed and strategies.json. Guarantees:
      - no card is dealt twice
      - every reasoning mode, workflow and strategy is dealt as evenly as possible
        (any two counts differ by at most 1), so 100 agents use all of them
      - while every reasoning mode and every workflow has no more agents than there
        are strategies (up to 144 agents with the shipped cards): no two agents share
        a reasoning mode and a workflow, a reasoning mode and a strategy, or a
        workflow and a strategy. Any two agents differ in at least two of the three.

    How: agent slot i gets reasoning i mod R and workflow (i + i // lcm(R, W)) mod W,
    which never repeats a workflow inside a reasoning mode and stays balanced. The
    strategies are then a balanced proper edge colouring of that reasoning x
    workflow grid. The seed shuffles every list's labels and who gets which card.
    """
    R, W, S = len(data["reasoning"]), len(data["workflows"]), len(data["strategies"])
    total = R * W * S
    if n < 1 or n > total:
        raise RoundTableError("can deal between 1 and %d cards, not %d" % (total, n))
    rng = random.Random("arena-deal:%s" % seed)
    pr, pw, ps = list(range(R)), list(range(W)), list(range(S))
    rng.shuffle(pr)
    rng.shuffle(pw)
    rng.shuffle(ps)
    lcm = R * W // math.gcd(R, W)
    cells = [(i % R, (i + i // lcm) % W) for i in range(n)]
    deg_r = max(sum(1 for r, _ in cells if r == x) for x in range(R))
    deg_w = max(sum(1 for _, w in cells if w == x) for x in range(W))
    if deg_r <= S and deg_w <= S:
        order = list(range(n))
        rng.shuffle(order)
        colours = _edge_colour([cells[i] for i in order], S)
        strategy_of = {order[j]: colours[j] for j in range(n)}
        triples = [(r, w, strategy_of[i]) for i, (r, w) in enumerate(cells)]
    else:
        # Past that size a repeat pair is unavoidable. Stay balanced and never repeat a card.
        count, pairs_rs, pairs_ws, used, triples = [0] * S, {}, {}, set(), []
        for r, w in cells:
            low = min(count)
            s = min((c for c in range(S) if (r, w, c) not in used),
                    key=lambda c: (count[c] - low, pairs_rs.get((r, c), 0), pairs_ws.get((w, c), 0), c))
            used.add((r, w, s))
            count[s] += 1
            pairs_rs[(r, s)] = pairs_rs.get((r, s), 0) + 1
            pairs_ws[(w, s)] = pairs_ws.get((w, s), 0) + 1
            triples.append((r, w, s))
    dealt = [(pr[r], pw[w], ps[s]) for r, w, s in triples]
    rng.shuffle(dealt)
    return dealt


def agent_ids(n):
    width = max(3, len(str(n)))
    return ["a%0*d" % (width, i) for i in range(1, n + 1)]



def round_matches(alive, k):
    """Matches in a round of `alive`: half of them, but never so many that fewer than k are left.
    13 alive with k 10 is 3 matches and 7 byes."""
    return min(alive // 2, alive - k)


def bracket_sizes(n, k=DEFAULT_K):
    """Alive count at the start of each round, ending at k. 100 -> 50 -> 25 -> 13 -> 10 for k 10."""
    sizes = [n]
    while sizes[-1] > k:
        sizes.append(sizes[-1] - round_matches(sizes[-1], k))
    return sizes


# ---------------------------------------------------------------- paths

def spawn_out(d, aid):
    return os.path.join(d, "r0", aid + ".md")


def attack_out(d, rnd, mid, attacker):
    return os.path.join(d, "r%d" % rnd, "%s.%s.attack.md" % (mid, attacker))


def defense_out(d, rnd, mid, aid):
    return os.path.join(d, "r%d" % rnd, "%s.%s.defense.md" % (mid, aid))


def revised_out(d, rnd, mid, aid):
    return os.path.join(d, "r%d" % rnd, "%s.%s.solution.md" % (mid, aid))


def verdict_out(d, rnd, mid):
    return os.path.join(d, "r%d" % rnd, "%s.verdict.json" % mid)


def sizing_out(d):
    return os.path.join(d, "sizing.json")


def table_path(d, *parts):
    return os.path.join(d, "table", *parts)


def pitch_out(d, aid):
    return table_path(d, "pitches", aid + ".md")


def contrib_out(d, n, aid):
    return table_path(d, "t%d" % n, aid + ".contribution.md")


def scribe_draft_out(d, n):
    return table_path(d, "t%d" % n, "draft.md")


def scribe_log_out(d, n):
    return table_path(d, "t%d" % n, "log.md")


def vote_out(d, n, aid):
    return table_path(d, "t%d" % n, aid + ".vote.md")


def redteam_out(d, i):
    return table_path(d, "redteam", "rt%d.attack.md" % i)


def rebut_out(d, aid):
    return table_path(d, "redteam", aid + ".defense.md")


def table_final_out(d):
    return table_path(d, "final.md")


def finalize_log_out(d):
    return table_path(d, "redteam", "log.md")


def check_out(d, name):
    return os.path.join(d, "%s.verdict.json" % name)


def prompt_path(d, rnd, job_id):
    sub = rnd if isinstance(rnd, str) else "r%d" % rnd
    return os.path.join(d, "prompts", sub, job_id + ".md")


def _has_output(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---------------------------------------------------------------- the bracket

def new_state(n, seed, data, run_dir, k=DEFAULT_K, table_rounds=DEFAULT_TABLE_ROUNDS,
              wave=DEFAULT_WAVE, has_baseline=False, models=None):
    if not 1 <= k < n:
        raise RoundTableError("n (%d) must be greater than k (%d), and k at least 1" % (n, k))
    run_dir = os.path.abspath(run_dir)
    ids = agent_ids(n)
    agents = {}
    for aid, (r, w, s) in zip(ids, deal(n, seed, data)):
        agents[aid] = {
            "card": {
                "reasoning": dict(data["reasoning"][r]),
                "workflow": dict(data["workflows"][w]),
                "strategy": dict(data["strategies"][s]),
            },
            "solution": spawn_out(run_dir, aid),
            "alive": True,
            "eliminated_in": None,
            "eliminated_by": None,
            "byes": 0,
        }
    state = {
        "version": 1,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "seed": seed,
        "agents_n": n,
        "k": k,
        "table_rounds": table_rounds,
        "cards_available": combo_count(data),
        "wave": wave,
        "models": dict(models or DEFAULT_MODELS),
        "dir": run_dir,
        "has_baseline": bool(has_baseline),
        "agents": agents,
        "rounds": [],
        "survivors": None,
        "champion": None,
        "table": None,
        "checks": {},
        "delivered": None,
    }
    open_round(state, ids)
    return state


def pair_round(state, rnd, alive):
    """Seeded pairing. A round plays round_matches() matches and everyone else gets a bye: one
    bye for an odd pool, more in the round that stops at k. Byes go to the agents with the
    fewest byes so far. Where it can, it pairs agents with different reasoning modes, so every
    attack comes from a genuinely different angle. The first-listed side is random, which is
    the order the judge reads them in."""
    agents = state["agents"]
    rng = random.Random("arena-pair:%s:%d" % (state["seed"], rnd))
    pool = sorted(alive)
    rng.shuffle(pool)
    n_byes = len(pool) - 2 * round_matches(len(pool), state["k"])
    # sorted() is stable, so among equal bye counts the shuffled order decides.
    byes = sorted(pool, key=lambda a: agents[a]["byes"])[:n_byes]
    for b in byes:
        pool.remove(b)
    width = max(2, len(str(len(pool) // 2)))
    matches = []
    while pool:
        a = pool.pop(0)
        mode = agents[a]["card"]["reasoning"]["id"]
        j = next((k for k, b in enumerate(pool) if agents[b]["card"]["reasoning"]["id"] != mode), 0)
        b = pool.pop(j)
        if rng.random() < 0.5:
            a, b = b, a
        matches.append({
            "id": "r%d-m%0*d" % (rnd, width, len(matches) + 1),
            "a": a, "b": b,
            "winner": None, "loser": None, "reason": None,
            "survived": [], "scores": None, "source": None,
        })
    return matches, sorted(byes)


def _blind(rng, one, other):
    x = one if rng.random() < 0.5 else other
    return {"X": x, "Y": other if x == one else one, "result": None}


def open_round(state, alive):
    """Open the next round, or end the tournament when only k agents are left: they take their
    seats at the table, and the best of them by judge score is the bracket champion."""
    alive = sorted(alive)
    if len(alive) <= state["k"]:
        state["survivors"] = alive
        state["champion"] = bracket_champion(state, alive)
        state["table"] = {"rounds": [_table_round(1)], "done": False, "stopped_early": False}
        rng = random.Random("arena-final:%s" % state["seed"])
        state["checks"] = {"final": _blind(rng, "table", "champion")}
        if state.get("has_baseline"):
            state["checks"]["baseline"] = _blind(rng, "winner", "baseline")
        return None
    rnd = len(state["rounds"]) + 1
    matches, byes = pair_round(state, rnd, alive)
    rd = {"n": rnd, "alive": alive, "matches": matches, "byes": byes, "closed": False}
    state["rounds"].append(rd)
    return rd


def bracket_champion(state, survivors):
    """The survivor with the highest judge total in the last match it won. Totals from different
    judges are only roughly comparable, so this is a proxy for "the best single competitor".
    No score (a manual record) counts as lowest; ties go to the lowest id."""
    last = {}
    for rd in state["rounds"]:
        for m in rd["matches"]:
            if m["winner"] in survivors:
                last[m["winner"]] = (m.get("scores") or {}).get(m["winner"])
    return max(survivors, key=lambda a: -1 if last.get(a) is None else last[a])


def current_round(state):
    if state["champion"] or not state["rounds"]:
        return None
    rd = state["rounds"][-1]
    return None if rd["closed"] else rd


def norm_agent(state, raw):
    raw = str(raw).strip().lower()
    if raw in state["agents"]:
        return raw
    digits = raw.lstrip("a")
    if digits.isdigit():
        for aid in state["agents"]:
            if int(aid[1:]) == int(digits):
                return aid
    raise RoundTableError("no agent called '%s'" % raw)


def find_match(state, mid):
    rd = current_round(state)
    if rd is None:
        raise RoundTableError("no round is open" + (" (the tournament is over)" if state["champion"] else ""))
    raw = str(mid).strip().lower()
    for m in rd["matches"]:
        if m["id"] == raw:
            return rd, m
    tail = raw.split("-m")[-1].lstrip("m")
    if tail.isdigit():
        for m in rd["matches"]:
            if int(m["id"].split("-m")[-1]) == int(tail):
                return rd, m
    raise RoundTableError("no match '%s' in round %d (run pairings)" % (mid, rd["n"]))


def record(state, mid, winner, reason=None, survived=None, scores=None, source="manual"):
    rd, m = find_match(state, mid)
    winner = norm_agent(state, winner)
    if winner not in (m["a"], m["b"]):
        raise RoundTableError("%s is not in %s (%s vs %s)" % (winner, m["id"], m["a"], m["b"]))
    previous = m["winner"]
    m["winner"] = winner
    m["loser"] = m["b"] if winner == m["a"] else m["a"]
    m["reason"] = reason or None
    m["survived"] = list(survived or [])
    m["scores"] = scores
    m["source"] = source
    return m, previous


def advance(state):
    """Close the current round: eliminate every loser, carry the winners' revised
    solutions forward, give the byes their free pass, and open the next round."""
    rd = current_round(state)
    if rd is None:
        raise RoundTableError("no round is open" + (" (the tournament is over)" if state["champion"] else ""))
    unrecorded = [m["id"] for m in rd["matches"] if not m["winner"]]
    if unrecorded:
        raise RoundTableError("round %d has %d unrecorded match(es): %s"
                         % (rd["n"], len(unrecorded), ", ".join(unrecorded)))
    survivors = []
    for m in rd["matches"]:
        loser = state["agents"][m["loser"]]
        loser["alive"] = False
        loser["eliminated_in"] = rd["n"]
        loser["eliminated_by"] = m["winner"]
        for aid in (m["a"], m["b"]):
            rev = revised_out(state["dir"], rd["n"], m["id"], aid)
            if _has_output(rev):
                state["agents"][aid]["solution"] = rev
        survivors.append(m["winner"])
    for b in rd["byes"]:
        state["agents"][b]["byes"] += 1
        survivors.append(b)
    rd["closed"] = True
    open_round(state, survivors)
    return survivors


def rounds_played(state):
    return sum(1 for rd in state["rounds"] if rd["closed"])


def alive_ids(state):
    return sorted(a for a, v in state["agents"].items() if v["alive"])


# ---------------------------------------------------------------- the table

def _table_round(n):
    return {"n": n, "votes": None, "closed": False}


def current_table_round(state):
    t = state.get("table")
    if not t or t["done"]:
        return None
    return t["rounds"][-1]


def table_rounds_played(state):
    t = state.get("table") or {"rounds": []}
    return sum(1 for tr in t["rounds"] if tr["closed"])


def parse_vote(text):
    """('ACCEPT', ''), ('OBJECT', objection), ('ABSTAIN', '') for a NO OUTPUT file, or None when
    the vote cannot be read. An OBJECT with nothing after it is not concrete, so it is unreadable."""
    text = text.strip()
    if text.upper().startswith("NO OUTPUT"):
        return "ABSTAIN", ""
    m = re.match(r"(ACCEPT|OBJECT)\b[\s:.-]*(.*)", text, re.S | re.I)
    if not m:
        return None
    vote, rest = m.group(1).upper(), m.group(2).strip()
    if vote == "OBJECT" and not rest:
        return None
    return vote, (rest[:1000] if vote == "OBJECT" else "")


def ensure_table_files(state):
    """Set up the shared table files. Draft v0 is a copy of the bracket champion's solution, so
    the table starts from the best single answer and can only argue its way up from there."""
    d = state["dir"]
    os.makedirs(table_path(d, "pitches"), exist_ok=True)
    if not os.path.isfile(table_path(d, "draft.md")):
        shutil.copyfile(state["agents"][state["champion"]]["solution"], table_path(d, "draft.md"))
    if not os.path.isfile(table_path(d, "objections.md")):
        _write(table_path(d, "objections.md"), "No open objections yet.\n")
    rebuild_log(state)


def rebuild_log(state):
    """log.md is every scribe log so far, in order, rebuilt from the per-step files."""
    d = state["dir"]
    parts = [scribe_log_out(d, tr["n"]) for tr in state["table"]["rounds"]] + [finalize_log_out(d)]
    body = ["# Table log", ""]
    for p in parts:
        if _has_output(p):
            body += [_read(p).rstrip(), ""]
        if p == scribe_log_out(d, state["table"]["rounds"][-1]["n"]) and state["table"].get("converged") is False:
            body += ["The table did not converge: fewer than half of the seats cast a real vote.", ""]
    _write(table_path(d, "log.md"), "\n".join(body))


def advance_table(state):
    """Close the open table round: read the votes, make the scribe's draft the current one,
    publish the standing objections, and end the table when none is left or R rounds are done.
    A vote that cannot be read is set aside as <name>.unreadable, so it gets re-run."""
    tr = current_table_round(state)
    if tr is None:
        raise RoundTableError("no table round is open")
    d, n = state["dir"], tr["n"]
    seats = state["survivors"]
    gone = [p for p in [scribe_draft_out(d, n)] + [vote_out(d, n, a) for a in seats] if not _has_output(p)]
    if gone:
        raise RoundTableError("table round %d has %d output(s) missing. %s" % (n, len(gone), _cmd("next")))
    votes, bad = {}, []
    for aid in seats:
        path = vote_out(d, n, aid)
        v = parse_vote(_read(path))
        if v is None:
            os.replace(path, path + ".unreadable")
            bad.append(aid)
        else:
            votes[aid] = {"vote": v[0], "objection": v[1]}
    if bad:
        raise RoundTableError("unreadable vote(s) from %s, set aside. Re-run them: %s" % (", ".join(bad), _cmd("next")))
    tr["votes"] = votes
    tr["closed"] = True
    shutil.copyfile(scribe_draft_out(d, n), table_path(d, "draft.md"))
    objections = [(a, v["objection"]) for a, v in sorted(votes.items()) if v["vote"] == "OBJECT"]
    real = sum(1 for v in votes.values() if v["vote"] != "ABSTAIN")
    stalled = not objections and real * 2 < len(seats)   # fewer than half voted: no consensus
    lines = ["# Open objections after table round %d" % n, ""]
    lines += ["- %s: %s" % (a, o.replace("\n", " ")) for a, o in objections] or ["No open objections."]
    _write(table_path(d, "objections.md"), "\n".join(lines) + "\n")
    t = state["table"]
    if (not objections and not stalled) or n >= state["table_rounds"]:
        t["done"] = True
        t["stopped_early"] = not objections and not stalled and n < state["table_rounds"]
        t["converged"] = not stalled
    rebuild_log(state)
    if not t["done"]:
        t["rounds"].append(_table_round(n + 1))
    return objections


def open_check(state):
    """The blind check that is due now: final once the scribe has written the final draft,
    then baseline (only with a rejected answer). None when neither is due."""
    t = state.get("table")
    if not t or not t["done"] or missing_jobs(phase_jobs(state, "finalize")):
        return None
    for name in ("final", "baseline"):
        chk = state["checks"].get(name)
        if chk and not chk["result"]:
            return name
    return None


def check_sources(state, name):
    d = state["dir"]
    if name == "final":
        return {"table": table_final_out(d), "champion": state["agents"][state["champion"]]["solution"]}
    return {"winner": state["delivered"]["path"], "baseline": os.path.join(d, "baseline.md")}


def deliver(state):
    """The final check decided: copy the better of the table's draft and the champion's
    solution to <run>/solution.md. The table never delivers something its champion beat."""
    better = state["checks"]["final"]["result"]["better"]
    src = check_sources(state, "final")[better]
    path = os.path.join(state["dir"], "solution.md")
    shutil.copyfile(src, path)
    state["delivered"] = {"from": better, "source": src, "path": path}


# ---------------------------------------------------------------- auto sizing

def apply_sizing(raw, explicit, max_n=DEFAULT_MAX_N):
    """Turn the sizer's JSON into (n, k, table_rounds, note).

    The suggestion is clamped: n to AUTO_MIN_N..max_n, k to 2..min(AUTO_MAX_K, n // 2),
    table_rounds to 1..MAX_TABLE_ROUNDS. Invalid or missing JSON falls back to the defaults.
    Every value in `explicit` (the flags the user passed) wins over the suggestion.
    """
    note = ""
    try:
        v = raw if isinstance(raw, dict) else None
        n, k, r = (int(v[key]) for key in ("n", "k", "table_rounds")
                   if not isinstance(v[key], bool))
        reason = str(v.get("reason") or "").strip().replace("\n", " ")[:200]
    except (TypeError, KeyError, ValueError):
        n, k, r = DEFAULT_N, DEFAULT_K, DEFAULT_TABLE_ROUNDS
        reason, note = "", "sizer JSON invalid or missing, using the defaults"
    n = explicit.get("n", max(AUTO_MIN_N, min(max_n, n)))
    k = explicit.get("k", max(2, min(AUTO_MAX_K, n // 2, k)))
    r = explicit.get("table_rounds", max(1, min(MAX_TABLE_ROUNDS, r)))
    if n <= k:
        n = k + 1   # only reachable with an explicit --k above the auto n
    return n, k, r, reason or note


def collect_sizing(state, data=None):
    """Read sizing.json, apply the guardrails and the explicit flags, deal the cards and open
    round 1. Returns the one-line summary."""
    d = state["dir"]
    pending = state["pending"]
    path = sizing_out(d)
    raw = extract_json(_read(path)) if _has_output(path) else None
    n, k, r, why = apply_sizing(raw, pending["explicit"], pending["max_n"])
    data = data or load_strategies()
    if n > combo_count(data):
        raise RoundTableError("n %d is more than the %d distinct cards" % (n, combo_count(data)))
    fresh = new_state(n, state["seed"], data, d, k=k, table_rounds=r, wave=state["wave"],
                      has_baseline=state["has_baseline"], models=state["models"])
    fresh["sizing"] = {"n": n, "k": k, "table_rounds": r, "reason": why,
                       "explicit": sorted(pending["explicit"])}
    state.clear()
    state.update(fresh)
    over = ", from your flags: %s" % ", ".join(sorted(pending["explicit"])) if pending["explicit"] else ""
    return "auto: n %d, k %d, table rounds %d%s. %s" % (n, k, r, over, why)


def _require_sized(state):
    if state.get("pending"):
        raise RoundTableError("the run is waiting for its sizing. %s" % _cmd("next"))


# ---------------------------------------------------------------- verdicts

def weighted_total(scores):
    """0 to 100 from five 0 to 10 scores, weighted as in rubric.md. None if any is missing."""
    if not isinstance(scores, dict):
        return None
    total = 0.0
    for key, weight in WEIGHTS:
        v = scores.get(key)
        if isinstance(v, bool):
            return None
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        total += max(0.0, min(10.0, v)) * weight
    return round(total / 10.0, 2)


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def extract_json(text):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except ValueError:
            return None
    return None


def decide(verdict, a, b):
    """Apply the rubric's arithmetic to a judge's verdict.

    The winner is the higher weighted total. A fatal solution cannot beat a
    non-fatal one. On an exact tie: fewer standing attacks, then higher
    correctness, then the judge's own pick. If the judge's pick disagrees with its
    own scores, the scores win and the note says so.
    Returns (winner, {a: total, b: total}, note).
    """
    if not isinstance(verdict, dict):
        raise RoundTableError("verdict is not a JSON object")
    raw_scores = verdict.get("scores") or {}
    scores = {str(k).strip().lower(): v for k, v in raw_scores.items()} if isinstance(raw_scores, dict) else {}
    sa, sb = scores.get(a), scores.get(b)
    pick_raw = str(verdict.get("winner") or "").strip().lower()
    in_a = re.search(r"\b%s\b" % re.escape(a), pick_raw) is not None
    in_b = re.search(r"\b%s\b" % re.escape(b), pick_raw) is not None
    pick = a if (in_a and not in_b) else b if (in_b and not in_a) else None
    ta, tb = weighted_total(sa), weighted_total(sb)
    totals = {a: ta, b: tb}
    if ta is None or tb is None:
        if pick:
            return pick, totals, "scores incomplete, used the judge's pick"
        raise RoundTableError("verdict has neither complete scores nor a clear winner")
    fa, fb = _truthy(sa.get("fatal")), _truthy(sb.get("fatal"))
    if fa != fb:
        winner = b if fa else a
    elif ta != tb:
        winner = a if ta > tb else b
    else:
        standing = verdict.get("standing") if isinstance(verdict.get("standing"), dict) else {}
        standing = {str(k).strip().lower(): v for k, v in standing.items()}
        na = len(standing.get(a) or []) if isinstance(standing.get(a), list) else None
        nb = len(standing.get(b) or []) if isinstance(standing.get(b), list) else None
        ca, cb = float(sa.get("correctness", 0)), float(sb.get("correctness", 0))
        if na is not None and nb is not None and na != nb:
            winner = a if na < nb else b
        elif ca != cb:
            winner = a if ca > cb else b
        elif pick:
            winner = pick
        else:
            raise RoundTableError("exact tie and the judge did not pick a winner")
    note = ""
    if pick and pick != winner:
        note = "judge picked %s but its own scores favour %s, so the scores win" % (pick, winner)
    return winner, totals, note



def collect_check(state, name):
    """Record the blind verdict of the final or the baseline check. The final check also
    delivers the winner to <run>/solution.md."""
    chk = state["checks"][name]
    path = check_out(state["dir"], name)
    if not _has_output(path):
        return name, "missing", ""
    v = extract_json(_read(path))
    try:
        if isinstance(v, dict) and isinstance(v.get("scores"), dict):
            v = dict(v, scores={str(k).strip().lower(): s for k, s in v["scores"].items()})
        w, totals, note = decide(v, "x", "y")
    except RoundTableError as e:
        os.replace(path, path + ".unreadable")
        return name, "unreadable", str(e)
    better = chk[w.upper()]
    chk["result"] = {
        "better": better,
        "totals": {chk["X"]: totals["x"], chk["Y"]: totals["y"]},
        "reason": str(v.get("reason") or "")[:400],
        "fixed": [str(x) for x in (v.get("fixed") or [])][:10] if isinstance(v.get("fixed"), list) else [],
    }
    if name == "final":
        deliver(state)
    return name, "recorded", ("%s is better. %s" % (better, note)).strip()


def collect(state):
    """Apply the sizing (with --auto), or record every verdict the judges have written for the
    open round, or the blind check that is due. An unreadable verdict is set aside as
    <name>.unreadable so the judge job shows up as missing again and gets re-run."""
    d = state["dir"]
    results = []
    if state.get("pending"):
        if not _has_output(sizing_out(d)):
            return [("size", "missing", "")]
        return [("size", "applied", collect_sizing(state))]
    if state["champion"]:
        name = open_check(state)
        return [collect_check(state, name)] if name else results
    rd = current_round(state)
    if rd is None:
        return results
    for m in rd["matches"]:
        if m["winner"]:
            continue
        path = verdict_out(d, rd["n"], m["id"])
        if not _has_output(path):
            results.append((m["id"], "missing", ""))
            continue
        v = extract_json(_read(path))
        try:
            winner, totals, note = decide(v, m["a"], m["b"])
        except RoundTableError as e:
            os.replace(path, path + ".unreadable")
            results.append((m["id"], "unreadable", str(e)))
            continue
        survived = v.get("survived") if isinstance(v.get("survived"), list) else []
        record(state, m["id"], winner,
               reason=str(v.get("reason") or "")[:400],
               survived=[str(x) for x in survived][:10],
               scores=totals, source="judge")
        loser = m["loser"]
        line = "%s beat %s, %s to %s" % (winner, loser, _fmt(totals[winner]), _fmt(totals[loser]))
        results.append((m["id"], "recorded", line + (". " + note if note else "")))
    return results


def _fmt(x):
    return "?" if x is None else ("%g" % x)


# ---------------------------------------------------------------- jobs and prompts

def phase_jobs(state, phase):
    """Every sub-agent job for a phase, with its prompt file, its model and the outputs it must
    write. Matches that already have a recorded winner are skipped. A phase whose time has
    not come (or has passed, for the table rounds) has no jobs."""
    if phase not in PHASES:
        raise RoundTableError("unknown phase '%s' (one of: %s)" % (phase, ", ".join(PHASES)))
    d = state["dir"]
    jobs = []
    kind = KIND_OF[phase]
    if phase == "size":
        if state.get("pending"):
            jobs.append({"id": "size", "kind": kind, "round": "size", "outputs": [sizing_out(d)]})
    elif state.get("pending"):
        pass
    elif phase == "spawn":
        for aid in sorted(state["agents"]):
            jobs.append({"id": aid + ".spawn", "kind": kind, "round": 0, "agent": aid,
                         "outputs": [spawn_out(d, aid)]})
    elif phase in ("attack", "defend", "judge"):
        rd = current_round(state)
        n = rd["n"] if rd else None
        for m in (rd["matches"] if rd else []):
            if m["winner"]:
                continue
            a, b = m["a"], m["b"]
            if phase == "attack":
                for me, opp in ((a, b), (b, a)):
                    jobs.append({"id": "%s.%s.attack" % (m["id"], me), "kind": kind, "round": n,
                                 "agent": me, "opponent": opp, "match": m["id"],
                                 "outputs": [attack_out(d, n, m["id"], me)]})
            elif phase == "defend":
                for me, opp in ((a, b), (b, a)):
                    jobs.append({"id": "%s.%s.defend" % (m["id"], me), "kind": kind, "round": n,
                                 "agent": me, "opponent": opp, "match": m["id"],
                                 "outputs": [defense_out(d, n, m["id"], me), revised_out(d, n, m["id"], me)]})
            else:
                jobs.append({"id": "%s.judge" % m["id"], "kind": kind, "round": n,
                             "match": m["id"], "a": a, "b": b,
                             "outputs": [verdict_out(d, n, m["id"])]})
    elif not state["champion"]:
        pass
    elif phase == "pitch":
        for aid in state["survivors"]:
            jobs.append({"id": aid + ".pitch", "kind": kind, "round": "table", "agent": aid,
                         "outputs": [pitch_out(d, aid)]})
    elif phase in ("contribute", "scribe", "vote"):
        tr = current_table_round(state)
        if tr:
            n, where = tr["n"], "t%d" % tr["n"]
            if phase == "scribe":
                jobs.append({"id": "%s.scribe" % where, "kind": kind, "round": where, "table_round": n,
                             "outputs": [scribe_draft_out(d, n), scribe_log_out(d, n)]})
            for aid in (state["survivors"] if phase != "scribe" else []):
                out = contrib_out(d, n, aid) if phase == "contribute" else vote_out(d, n, aid)
                jobs.append({"id": "%s.%s.%s" % (where, aid, phase), "kind": kind, "round": where,
                             "table_round": n, "agent": aid, "outputs": [out]})
    elif phase in ("redteam", "rebut", "finalize"):
        if state["table"]["done"]:
            if phase == "redteam":
                for i, lens in enumerate(REDTEAM_LENSES, start=1):
                    jobs.append({"id": "rt%d.attack" % i, "kind": kind, "round": "redteam",
                                 "attacker": "rt%d" % i, "lens": lens, "outputs": [redteam_out(d, i)]})
            elif phase == "rebut":
                for aid in state["survivors"]:
                    jobs.append({"id": aid + ".rebut", "kind": kind, "round": "redteam", "agent": aid,
                                 "outputs": [rebut_out(d, aid)]})
            else:
                jobs.append({"id": "finalize", "kind": kind, "round": "redteam",
                             "outputs": [table_final_out(d), finalize_log_out(d)]})
    elif open_check(state) == phase:
        jobs.append({"id": phase + ".judge", "kind": kind, "round": phase, "outputs": [check_out(d, phase)]})
    for j in jobs:
        j["prompt"] = prompt_path(d, j["round"], j["id"])
        j["model"] = state["models"][ROLE_OF[j["kind"]]]
    return jobs


def missing_jobs(jobs):
    return [j for j in jobs if not all(_has_output(p) for p in j["outputs"])]


_TEMPLATE_RE = re.compile(r"<!-- template:([a-z]+) -->\s*```text\n(.*?)\n```\s*<!-- /template:\1 -->", re.S)
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def load_templates(path=SKILL_PATH):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    found = {m.group(1): m.group(2) for m in _TEMPLATE_RE.finditer(text)}
    missing = [t for t in TEMPLATES if t not in found]
    if missing:
        raise RoundTableError("SKILL.md is missing prompt templates: %s" % ", ".join(missing))
    return found


def fill(template, values):
    """One pass, so a task that itself contains {{braces}} is left exactly as written."""
    wanted = set(_PLACEHOLDER_RE.findall(template))
    missing = sorted(wanted - set(values))
    if missing:
        raise RoundTableError("no value for placeholder(s): %s" % ", ".join(missing))
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), template)


def read_task(state):
    """The task exactly as init stored it (init adds one trailing newline to the file)."""
    with open(os.path.join(state["dir"], "task.md"), encoding="utf-8") as fh:
        return fh.read().rstrip("\n")


def _card_values(card):
    return {
        "reasoning_name": card["reasoning"]["name"], "reasoning_how": card["reasoning"]["how"],
        "workflow_name": card["workflow"]["name"], "workflow_how": card["workflow"]["how"],
        "strategy_name": card["strategy"]["name"], "strategy_how": card["strategy"]["how"],
    }


def _bullets(paths):
    return "\n".join("- " + p for p in paths)


def _history(state, aid):
    """The attacks a survivor took and the defenses it wrote, round by round, for its pitch."""
    d, lines = state["dir"], []
    for rd in state["rounds"]:
        if aid in rd["byes"]:
            lines.append("- round %d: a bye, no match" % rd["n"])
        for m in rd["matches"]:
            if aid in (m["a"], m["b"]):
                opp = m["b"] if m["a"] == aid else m["a"]
                lines.append("- round %d: attacks you took %s, your defense %s"
                             % (rd["n"], attack_out(d, rd["n"], m["id"], opp), defense_out(d, rd["n"], m["id"], aid)))
    return "\n".join(lines)


def _standing(state):
    """The objections still standing when the table ended, one line per seat, or 'none'."""
    votes = state["table"]["rounds"][-1]["votes"] or {}
    lines = ["- %s: %s" % (a, v["objection"].replace("\n", " "))
             for a, v in sorted(votes.items()) if v["vote"] == "OBJECT"]
    return "\n" + "\n".join(lines) if lines else "none"


def render_job(state, job, templates, task):
    kind = job["kind"]
    d = state["dir"]
    if kind == "sizer":
        p = state["pending"]
        return fill(templates[kind], {
            "task": task, "out": job["outputs"][0], "max_n": p["max_n"], "min_n": AUTO_MIN_N,
            "max_k": AUTO_MAX_K, "max_rounds": MAX_TABLE_ROUNDS, "default_n": DEFAULT_N,
            "default_k": DEFAULT_K, "default_rounds": DEFAULT_TABLE_ROUNDS})
    agents = state["agents"]
    v = {"task": task, "run_dir": d, "rubric": os.path.join(d, "rubric.md"), "n": state["agents_n"],
         "k": state["k"], "table_rounds": state["table_rounds"],
         "draft": table_path(d, "draft.md"), "objections": table_path(d, "objections.md"),
         "log": table_path(d, "log.md"), "pitches": table_path(d, "pitches")}
    if "agent" in job:
        v["agent"] = job["agent"]
    if kind in ("seat", "vote", "rebut"):
        v["own_pitch"] = pitch_out(d, job["agent"])
    if kind == "competitor":
        aid = job["agent"]
        v.update(_card_values(agents[aid]["card"]))
        if state.get("has_baseline"):
            v["baseline_note"] = (
                "The user already got an answer to this task and was NOT satisfied with it. It is at "
                "%s. Read it first and work out exactly why it fell short. Then beat it. Do not just "
                "polish it: what this tournament delivers is compared with that answer at the end."
                % os.path.join(d, "baseline.md"))
        else:
            v["baseline_note"] = "There is no earlier answer to beat. Start from the task."
        v.update(out=job["outputs"][0])
    elif kind == "attacker":
        me, opp = job["agent"], job["opponent"]
        v.update(_card_values(agents[me]["card"]))
        v.update(target=opp, match=job["match"], round=job["round"],
                 own_solution=agents[me]["solution"], target_solution=agents[opp]["solution"],
                 out=job["outputs"][0])
    elif kind == "defender":
        me, opp = job["agent"], job["opponent"]
        v.update(_card_values(agents[me]["card"]))
        v.update(attacker=opp, match=job["match"], round=job["round"],
                 own_solution=agents[me]["solution"],
                 attacks=attack_out(d, job["round"], job["match"], opp),
                 defense_out=job["outputs"][0], solution_out=job["outputs"][1])
    elif kind == "judge":
        n, mid, a, b = job["round"], job["match"], job["a"], job["b"]
        v.update(match=mid, round=n, first=a, second=b,
                 first_solution=revised_out(d, n, mid, a), second_solution=revised_out(d, n, mid, b),
                 first_attacks=attack_out(d, n, mid, b), second_attacks=attack_out(d, n, mid, a),
                 first_defense=defense_out(d, n, mid, a), second_defense=defense_out(d, n, mid, b),
                 out=job["outputs"][0])
    elif kind == "pitch":
        v.update(own_solution=agents[job["agent"]]["solution"], history=_history(state, job["agent"]),
                 out=job["outputs"][0])
    elif kind == "seat":
        v.update(round=job["table_round"], out=job["outputs"][0])
    elif kind == "scribe":
        n = job["table_round"]
        v.update(round=n, contributions=_bullets(contrib_out(d, n, a) for a in state["survivors"]),
                 draft_out=job["outputs"][0], log_out=job["outputs"][1])
    elif kind == "vote":
        n = job["table_round"]
        v.update(round=n, new_draft=scribe_draft_out(d, n), round_log=scribe_log_out(d, n),
                 out=job["outputs"][0])
    elif kind == "redteam":
        v.update(attacker=job["attacker"], lens=job["lens"], objections=_standing(state), out=job["outputs"][0])
    elif kind == "rebut":
        v.update(attacks=_bullets(redteam_out(d, i) for i in range(1, len(REDTEAM_LENSES) + 1)),
                 out=job["outputs"][0])
    elif kind == "finalize":
        v.update(attacks=_bullets(redteam_out(d, i) for i in range(1, len(REDTEAM_LENSES) + 1)),
                 defenses=_bullets(rebut_out(d, a) for a in state["survivors"]),
                 objections=_standing(state), final_out=job["outputs"][0], log_out=job["outputs"][1])
    elif kind in ("final", "baseline"):
        v.update(rounds=rounds_played(state), table_rounds_played=table_rounds_played(state),
                 x_solution=os.path.join(d, kind, "X.md"), y_solution=os.path.join(d, kind, "Y.md"),
                 out=job["outputs"][0])
    return fill(templates[kind], v)


def write_prompts(state, phase):
    jobs = phase_jobs(state, phase)
    if not jobs:
        return jobs
    templates = load_templates()
    task = read_task(state)
    if PHASES.index(phase) >= PHASES.index("pitch"):
        ensure_table_files(state)
    if phase in ("final", "baseline"):
        # Blind copies: the judge sees X and Y, never which one is which.
        chk, sources = state["checks"][phase], check_sources(state, phase)
        for label in ("X", "Y"):
            _write(os.path.join(state["dir"], phase, label + ".md"), _read(sources[chk[label]]))
    for j in jobs:
        for p in j["outputs"] + [j["prompt"]]:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        body = render_job(state, j, templates, task)
        with open(j["prompt"], "w", encoding="utf-8") as fh:
            fh.write(body + "\n")
    if phase == "spawn":
        for aid in state["agents"]:
            os.makedirs(os.path.join(state["dir"], "scratch", aid), exist_ok=True)
    return jobs


def next_action(state):
    """(phase, jobs_left, jobs_total). phase is one of PHASES, collect, advance or done."""
    if state.get("pending"):
        jobs = phase_jobs(state, "size")
        left = missing_jobs(jobs)
        return ("size", left, jobs) if left else ("collect", [], jobs)
    spawn = phase_jobs(state, "spawn")
    left = missing_jobs(spawn)
    if left:
        return "spawn", left, spawn
    if not state["champion"]:
        for phase in ("attack", "defend", "judge"):
            jobs = phase_jobs(state, phase)
            left = missing_jobs(jobs)
            if left:
                return phase, left, jobs
        rd = current_round(state)
        if any(not m["winner"] for m in rd["matches"]):
            return "collect", [], []
        return "advance", [], []
    # The table phases have no jobs before their time, and the table-round ones none after it.
    for phase in ("pitch", "contribute", "scribe", "vote", "redteam", "rebut", "finalize"):
        jobs = phase_jobs(state, phase)
        left = missing_jobs(jobs)
        if left:
            return phase, left, jobs
    if current_table_round(state):
        return "advance", [], []
    name = open_check(state)
    if name:
        jobs = phase_jobs(state, name)
        left = missing_jobs(jobs)
        return (name, left, jobs) if left else ("collect", [], jobs)
    return "done", [], []


# ---------------------------------------------------------------- plan

def plan_rows(n, k=DEFAULT_K, wave=DEFAULT_WAVE):
    rows = []
    for rnd, alive in enumerate(bracket_sizes(n, k)[:-1], start=1):
        m = round_matches(alive, k)
        waves = 2 * math.ceil(2 * m / wave) + math.ceil(m / wave)
        rows.append({"round": rnd, "alive": alive, "matches": m, "byes": alive - 2 * m,
                     "calls": CALLS_PER_MATCH * m, "waves": waves})
    return rows


def plan_totals(n, k=DEFAULT_K, table_rounds=DEFAULT_TABLE_ROUNDS, wave=DEFAULT_WAVE,
                models=None, auto=False, baseline=False):
    """Sub-agent calls and waves for the whole run, at most: the table may stop early.
    calls_min is the count when every seat accepts after table round 1."""
    models = models or DEFAULT_MODELS
    rows = plan_rows(n, k, wave)
    kw = math.ceil(k / wave)
    steps = [{"step": "pitch", "calls": k, "waves": kw}]
    steps += [{"step": "table %d" % r, "calls": 2 * k + 1, "waves": 2 * kw + 1} for r in range(1, table_rounds + 1)]
    steps.append({"step": "red team", "calls": len(REDTEAM_LENSES) + k + 1,
                  "waves": math.ceil(len(REDTEAM_LENSES) / wave) + kw + 1})
    steps.append({"step": "final", "calls": 1, "waves": 1})
    if baseline:
        steps.append({"step": "baseline", "calls": 1, "waves": 1})
    if auto:
        steps.insert(0, {"step": "size", "calls": 1, "waves": 1})
    matches = sum(r["matches"] for r in rows)
    roles = {"sizer": int(auto), "competitor": n, "attacker": 2 * matches, "defender": 2 * matches,
             "judge": matches, "seat": k + 2 * k * table_rounds + k, "scribe": table_rounds + 1,
             "redteam": len(REDTEAM_LENSES), "final": 1 + int(baseline)}
    by_model = {}
    for role, c in roles.items():
        if c:
            by_model[models[role]] = by_model.get(models[role], 0) + c
    calls = n + sum(r["calls"] for r in rows) + sum(s["calls"] for s in steps)
    waves = math.ceil(n / wave) + sum(r["waves"] for r in rows) + sum(s["waves"] for s in steps)
    return {"agents": n, "k": k, "table_rounds": table_rounds, "rounds": len(rows), "calls": calls,
            "calls_min": calls - (table_rounds - 1) * (2 * k + 1), "waves": waves, "rows": rows,
            "steps": steps, "roles": {r: c for r, c in roles.items() if c}, "by_model": by_model}


# ---------------------------------------------------------------- state io

def run_dir_from(args):
    if getattr(args, "dir", None):
        return os.path.abspath(args.dir)
    latest = os.path.join(ROOT, LATEST)
    if os.path.isfile(latest):
        with open(latest, encoding="utf-8") as fh:
            return fh.read().strip()
    raise RoundTableError("no round table here. Run init first, or pass --dir")


def load_state(args):
    d = run_dir_from(args)
    path = os.path.join(d, STATE_FILE)
    if not os.path.isfile(path):
        raise RoundTableError("no %s in %s" % (STATE_FILE, d))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    path = os.path.join(state["dir"], STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def _cmd(*parts):
    return 'python3 "%s" %s' % (SELF, " ".join(parts))


def _card_line(card):
    return "%s + %s + %s" % (card["reasoning"]["name"], card["workflow"]["name"], card["strategy"]["name"])


# ---------------------------------------------------------------- commands

def _sizes(args):
    """(n, k, table_rounds, explicit) from the flags. explicit holds only what the user passed,
    which is what --auto must not override."""
    if args.quick and args.n is not None:
        raise RoundTableError("pass --quick or --n, not both")
    explicit = {}
    if args.quick:
        explicit["n"] = QUICK_N
    elif args.n is not None:
        explicit["n"] = args.n
    if args.k is not None:
        explicit["k"] = args.k
    if args.table_rounds is not None:
        explicit["table_rounds"] = args.table_rounds
    n = explicit.get("n", DEFAULT_N)
    k = explicit.get("k", DEFAULT_K)
    r = explicit.get("table_rounds", DEFAULT_TABLE_ROUNDS)
    if args.wave < 1:
        raise RoundTableError("--wave must be at least 1")
    if k < 2:
        raise RoundTableError("--k must be at least 2")
    if not 1 <= r <= MAX_TABLE_ROUNDS:
        raise RoundTableError("--table-rounds must be between 1 and %d" % MAX_TABLE_ROUNDS)
    if "n" in explicit or not getattr(args, "auto", False):
        if n <= k:
            raise RoundTableError("--n (%d) must be greater than --k (%d)" % (n, k))
    return n, k, r, explicit


def _models(args):
    return {role: getattr(args, "model_" + role) for role in DEFAULT_MODELS}


def cmd_plan(args):
    n, k, r, _ = _sizes(args)
    models = _models(args)
    t = plan_totals(n, k, r, args.wave, models, auto=args.auto)
    if args.json:
        print(json.dumps(t, indent=1))
        return 0
    print("%d competitors, %d at the table, %d bracket rounds, up to %d table round(s), waves of %d"
          % (n, k, t["rounds"], r, args.wave))
    if args.auto:
        print("--auto: the sizer picks n, k and table rounds. The numbers below are for the values shown.")
    print("")
    print("      step  alive  matches  byes  sub-agent calls  waves")
    steps = t["steps"]
    if args.auto:
        print("  %8s  %5s  %7s  %4s  %15d  %5d" % ("size", "-", "-", "-", 1, 1))
        steps = steps[1:]
    print("     spawn  %5d  %7s  %4s  %15d  %5d" % (n, "-", "-", n, math.ceil(n / args.wave)))
    for row in t["rows"]:
        print("   round %d  %5d  %7d  %4s  %15d  %5d"
              % (row["round"], row["alive"], row["matches"], row["byes"] or "-", row["calls"], row["waves"]))
    for s in steps:
        alive = "-" if s["step"] in ("final", "baseline") else k
        print("  %8s  %5s  %7s  %4s  %15d  %5d" % (s["step"], alive, "-", "-", s["calls"], s["waves"]))
    print("     total                         %15d  %5d" % (t["calls"], t["waves"]))
    print("")
    print("  alive per round: %s" % " -> ".join(str(s) for s in bracket_sizes(n, k)))
    print("  calls per role: %s" % ", ".join("%s %d (%s)" % (role, c, models[role]) for role, c in t["roles"].items()))
    print("  calls per model: %s" % ", ".join("%s %d" % (m, c) for m, c in sorted(t["by_model"].items())))
    print("  at least %d calls if every seat accepts after table round 1" % t["calls_min"])
    print("  plus 1 call (%s) for the baseline check when there is a rejected answer to beat" % models["final"])
    return 0


def cmd_init(args):
    n, k, r, explicit = _sizes(args)
    data = load_strategies()
    if args.auto and args.max_n < AUTO_MIN_N:
        raise RoundTableError("--max-n must be at least %d" % AUTO_MIN_N)
    if args.auto and args.max_n > combo_count(data):
        raise RoundTableError("--max-n must be at most %d (the number of distinct cards)" % combo_count(data))
    if n > combo_count(data):
        raise RoundTableError("--n must be at most %d (the number of distinct cards)" % combo_count(data))
    if args.task_file:
        with open(args.task_file, encoding="utf-8") as fh:
            task = fh.read()
    else:
        task = args.task or ""
    task = task.strip()
    if not task:
        raise RoundTableError("the task is empty. Pass --task-file or --task")
    baseline = None
    if args.baseline_file:
        with open(args.baseline_file, encoding="utf-8") as fh:
            baseline = fh.read().strip()
        if not baseline:
            raise RoundTableError("the baseline file is empty")
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(1, 1000000)
    default_dir = not args.dir
    d = os.path.abspath(args.dir or os.path.join(ROOT, "run-%s-s%s" % (time.strftime("%Y%m%d-%H%M%S"), seed)))
    if os.path.exists(os.path.join(d, STATE_FILE)):
        raise RoundTableError("%s already has a round table in it. Pick another --dir" % d)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "task.md"), "w", encoding="utf-8") as fh:
        fh.write(task + "\n")
    if baseline:
        with open(os.path.join(d, "baseline.md"), "w", encoding="utf-8") as fh:
            fh.write(baseline + "\n")
    # The run gets its own copy of the rubric: frozen for this run, and inside the
    # working directory, so judges can read it without a permission prompt each.
    with open(RUBRIC_PATH, encoding="utf-8") as src, \
            open(os.path.join(d, "rubric.md"), "w", encoding="utf-8") as dst:
        dst.write(src.read())
    if args.auto:
        # The cards are dealt once the sizer has picked n: collect_sizing() builds the real state.
        state = {"version": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "seed": seed,
                 "wave": args.wave, "models": _models(args), "dir": d, "has_baseline": bool(baseline),
                 "pending": {"explicit": explicit, "max_n": args.max_n}}
    else:
        state = new_state(n, seed, data, d, k=k, table_rounds=r, wave=args.wave,
                          has_baseline=bool(baseline), models=_models(args))
    save_state(state)
    if default_dir:
        os.makedirs(ROOT, exist_ok=True)
        with open(os.path.join(ROOT, LATEST), "w", encoding="utf-8") as fh:
            fh.write(d + "\n")
    print("round table ready: %s" % d)
    if args.auto:
        print("seed %s. --auto: a sizer sub-agent picks n, k and table rounds first%s."
              % (seed, ", your flags win: %s" % ", ".join(sorted(explicit)) if explicit else ""))
    else:
        t = plan_totals(n, k, r, args.wave, state["models"], baseline=bool(baseline))
        print("seed %s. %d competitors, %d distinct cards dealt from %d, no repeats. %d sit at the table."
              % (seed, n, n, combo_count(data), k))
        print("%d bracket rounds (%s), up to %d table round(s). Up to %d sub-agent calls in %d waves of %d."
              % (t["rounds"], " -> ".join(str(s) for s in bracket_sizes(n, k)), r, t["calls"], t["waves"], args.wave))
    print("next: %s" % _cmd("next"))
    return 0


def cmd_next(args):
    state = load_state(args)
    phase, left, jobs = next_action(state)
    if state.get("pending"):
        print("sizing: a sizer picks n, k and table rounds before the cards are dealt")
    elif phase == "spawn":
        print("spawn: %d competitors, then %d bracket round(s) down to %d"
              % (state["agents_n"], len(bracket_sizes(state["agents_n"], state["k"])) - 1, state["k"]))
    elif current_round(state):
        rd = current_round(state)
        print("round %d of %d, %d alive, stopping at %d"
              % (rd["n"], len(bracket_sizes(state["agents_n"], state["k"])) - 1, len(rd["alive"]), state["k"]))
    elif current_table_round(state):
        print("table round %d of at most %d, %d seats, bracket champion %s"
              % (current_table_round(state)["n"], state["table_rounds"], state["k"], state["champion"]))
    elif state["champion"]:
        print("table closed after %d round(s), bracket champion %s" % (table_rounds_played(state), state["champion"]))
    if phase in PHASES:
        model = state["models"][ROLE_OF[KIND_OF[phase]]]
        print("NEXT: %s, model %s. %d of %d job(s) to run." % (phase, model, len(left), len(jobs)))
        print("  1. %s" % _cmd("prompts", phase))
        print("  2. launch the jobs it lists, one wave of %d at a time, each with model %s" % (state["wave"], model))
        print("  3. %s" % _cmd("next"))
    elif phase == "collect":
        print("NEXT: collect. Every output for this step is on disk.")
        print("  %s" % _cmd("collect"))
    elif phase == "advance":
        what = ("every vote for table round %d is in" % current_table_round(state)["n"]
                if state["champion"] else "every match in round %d has a winner" % current_round(state)["n"])
        print("NEXT: advance. %s%s." % (what[0].upper(), what[1:]))
        print("  %s" % _cmd("advance"))
    else:
        print("DONE. %s" % _cmd("winner"))
    return 0


def cmd_prompts(args):
    state = load_state(args)
    jobs = write_prompts(state, args.phase)
    save_state(state)
    if not jobs:
        print("nothing to run for %s right now. %s" % (args.phase, _cmd("next")))
        return 0
    left = missing_jobs(jobs)
    w = state["wave"]
    where = jobs[0]["round"]
    where = "round %d" % where if isinstance(where, int) else where
    waves = [left[i:i + w] for i in range(0, len(left), w)]
    print("%s, %s: %d job(s), %d still to run, %d wave(s) of up to %d"
          % (where, args.phase, len(jobs), len(left), len(waves), w))
    for k, wave in enumerate(waves, start=1):
        print("wave %d" % k)
        for j in wave:
            print("  %-26s %-6s %s" % (j["id"], j["model"], j["prompt"]))
    if left:
        print("")
        print("Each job is one Agent call: subagent_type general-purpose, model <the model listed>,")
        print("description \"round-table <job id>\",")
        print("prompt \"Read <prompt path> and follow it exactly. It is your whole brief.\"")
        print("A wave is one message with all of its Agent calls. Let it finish before the next wave.")
    print("then: %s" % _cmd("next"))
    return 0


def cmd_check(args):
    state = load_state(args)
    jobs = phase_jobs(state, args.phase)
    left = missing_jobs(jobs)
    print("%s: %d of %d job(s) done" % (args.phase, len(jobs) - len(left), len(jobs)))
    for j in left:
        gone = [p for p in j["outputs"] if not _has_output(p)]
        print("  missing %-26s %s" % (j["id"], ", ".join(gone)))
    return 1 if left else 0


def cmd_pairings(args):
    state = load_state(args)
    _require_sized(state)
    rd = current_round(state)
    if rd is None:
        if state["champion"]:
            print("No matches left. The tournament is over: %d at the table (%s), bracket champion %s."
                  % (len(state["survivors"]), ", ".join(state["survivors"]), state["champion"]))
            return 0
        raise RoundTableError("no round is open")
    if args.json:
        print(json.dumps(rd, indent=1))
        return 0
    agents = state["agents"]
    print("round %d: %d alive, %d match(es), byes: %s"
          % (rd["n"], len(rd["alive"]), len(rd["matches"]), ", ".join(rd["byes"]) or "none"))
    for m in rd["matches"]:
        ra = agents[m["a"]]["card"]["reasoning"]["name"]
        rb = agents[m["b"]]["card"]["reasoning"]["name"]
        res = ("winner %s" % m["winner"]) if m["winner"] else "open"
        print("  %s  %s vs %s  (%s vs %s)  %s" % (m["id"], m["a"], m["b"], ra, rb, res))
    for b in rd["byes"]:
        print("  bye     %s goes through without a match" % b)
    return 0


def cmd_record(args):
    state = load_state(args)
    _require_sized(state)
    m, previous = record(state, args.match_id, args.winner_id, reason=args.reason,
                         survived=args.survived, source="manual")
    save_state(state)
    extra = " (was %s)" % previous if previous and previous != m["winner"] else ""
    print("%s: %s beat %s%s" % (m["id"], m["winner"], m["loser"], extra))
    return 0


def cmd_collect(args):
    state = load_state(args)
    results = collect(state)
    save_state(state)
    if not results:
        print("nothing to collect. %s" % _cmd("next"))
        return 0
    bad = 0
    for mid, what, note in results:
        if what not in ("recorded", "applied"):
            bad += 1
        print("  %-10s %-10s %s" % (mid, what, note))
    rd = current_round(state)
    if rd:
        done = sum(1 for m in rd["matches"] if m["winner"])
        print("round %d: %d of %d match(es) recorded" % (rd["n"], done, len(rd["matches"])))
    if state.get("delivered") and any(r[0] == "final" and r[1] == "recorded" for r in results):
        print("delivered: the %s's answer, %s" % (state["delivered"]["from"], state["delivered"]["path"]))
    if bad:
        print("%d output(s) missing or unreadable. Re-run those jobs: %s" % (bad, _cmd("next")))
        return 1
    print("then: %s" % _cmd("next"))
    return 0


def cmd_advance(args):
    state = load_state(args)
    _require_sized(state)
    if state["champion"]:
        n = (current_table_round(state) or {}).get("n")
        objections = advance_table(state)
        save_state(state)
        print("table round %s closed: %d objection(s) standing" % (n, len(objections)))
        t = state["table"]
        if t["done"]:
            why = ("the table did not converge, the last table round is done" if t.get("converged") is False
                   else "no objection standing" if not objections else "the last table round is done")
            print("TABLE DONE: %s. Red team next. %s" % (why, _cmd("next")))
        else:
            print("table round %d open. %s" % (t["rounds"][-1]["n"], _cmd("next")))
        return 0
    rd = current_round(state)
    closing = rd["n"] if rd else None
    survivors = advance(state)
    save_state(state)
    print("round %s closed: %d through, %d eliminated"
          % (closing, len(survivors), len(rd["matches"])))
    if state["champion"]:
        print("TOURNAMENT DONE: %d at the table, bracket champion %s. %s"
              % (len(state["survivors"]), state["champion"], _cmd("next")))
    else:
        nr = state["rounds"][-1]
        print("round %d open: %d match(es)%s. %s"
              % (nr["n"], len(nr["matches"]), ", byes %s" % ", ".join(nr["byes"]) if nr["byes"] else "",
                 _cmd("next")))
    return 0


def status_rows(state):
    rows = []
    for rd in state["rounds"]:
        recorded = sum(1 for m in rd["matches"] if m["winner"])
        rows.append({"round": rd["n"], "alive": len(rd["alive"]), "matches": len(rd["matches"]),
                     "byes": len(rd["byes"]), "eliminated": recorded,
                     "state": "closed" if rd["closed"] else "open, %d of %d recorded" % (recorded, len(rd["matches"]))})
    return rows


def cmd_status(args):
    state = load_state(args)
    if state.get("pending"):
        print("round table %s\nwaiting for the sizer. %s" % (state["dir"], _cmd("next")))
        return 0
    rows = status_rows(state)
    alive = alive_ids(state)
    if args.json:
        print(json.dumps({"dir": state["dir"], "seed": state["seed"], "agents": state["agents_n"],
                          "k": state["k"], "alive": len(alive), "eliminated": state["agents_n"] - len(alive),
                          "champion": state["champion"], "rounds": rows, "table": state["table"],
                          "delivered": state["delivered"]}, indent=1))
        return 0
    print("round table %s" % state["dir"])
    print("seed %s, %d competitors, %d at the table, waves of %d, %d distinct cards available"
          % (state["seed"], state["agents_n"], state["k"], state["wave"], state["cards_available"]))
    print("  round  alive  matches  byes  eliminated  state")
    for r in rows:
        print("  %5d  %5d  %7d  %4d  %10d  %s"
              % (r["round"], r["alive"], r["matches"], r["byes"], r["eliminated"], r["state"]))
    print("now: %d alive, %d eliminated%s"
          % (len(alive), state["agents_n"] - len(alive),
             ", champion %s" % state["champion"] if state["champion"] else ""))
    t = state["table"]
    if t:
        print("table: %d of at most %d round(s) closed%s%s"
              % (table_rounds_played(state), state["table_rounds"],
                 ", stopped early" if t["stopped_early"] else "", ", done" if t["done"] else ""))
    if state["delivered"]:
        print("delivered: the %s's answer, %s" % (state["delivered"]["from"], state["delivered"]["path"]))
    return 0


def winner_report(state):
    champ = state.get("champion")
    if not champ:
        return None
    agent = state["agents"][champ]
    path = []
    survived = []
    for rd in state["rounds"]:
        if champ in rd["byes"]:
            path.append({"round": rd["n"], "bye": True})
        for m in rd["matches"]:
            if champ in (m["a"], m["b"]):
                opp = m["b"] if m["a"] == champ else m["a"]
                sc = m.get("scores") or {}
                path.append({"round": rd["n"], "bye": False, "beat": opp, "match": m["id"],
                             "score": sc.get(champ), "opponent_score": sc.get(opp),
                             "reason": m.get("reason"), "survived": m.get("survived") or []})
                survived.extend(m.get("survived") or [])
    checks = state.get("checks") or {}
    return {"champion": champ, "card": agent["card"], "card_line": _card_line(agent["card"]),
            "solution": agent["solution"], "rounds": rounds_played(state), "agents": state["agents_n"],
            "survivors": state["survivors"], "matches_won": sum(1 for p in path if not p["bye"]), "path": path,
            "attacks_survived": survived, "table_rounds": table_rounds_played(state),
            "stopped_early": state["table"]["stopped_early"],
            "final": (checks.get("final") or {}).get("result"),
            "baseline": (checks.get("baseline") or {}).get("result"), "delivered": state.get("delivered")}


def cmd_winner(args):
    state = load_state(args)
    _require_sized(state)
    rep = winner_report(state)
    if rep is None:
        rd = current_round(state)
        print("no winner yet: round %s, %d alive. %s"
              % (rd["n"] if rd else "?", len(alive_ids(state)), _cmd("next")))
        return 1
    if args.json:
        print(json.dumps(rep, indent=1))
        return 0
    print("CHAMPION  %s (best of the %d at the table by its last judge score)" % (rep["champion"], len(rep["survivors"])))
    print("card      %s" % rep["card_line"])
    print("solution  %s" % rep["solution"])
    print("rounds    %d played, %d agents in, %d left, %d match(es) won"
          % (rep["rounds"], rep["agents"], len(rep["survivors"]), rep["matches_won"]))
    for p in rep["path"]:
        if p["bye"]:
            print("  round %d  bye" % p["round"])
            continue
        print("  round %d  beat %s, %s to %s. %s"
              % (p["round"], p["beat"], _fmt(p["score"]), _fmt(p["opponent_score"]), p["reason"] or ""))
        for s in p["survived"]:
            print("           survived: %s" % s)
    print("table     %d round(s)%s, seats %s"
          % (rep["table_rounds"], ", stopped early" if rep["stopped_early"] else "", ", ".join(rep["survivors"])))
    fin = rep["final"]
    if fin:
        print("final     table %s, champion %s. %s is better. %s"
              % (_fmt(fin["totals"].get("table")), _fmt(fin["totals"].get("champion")), fin["better"], fin["reason"]))
    if rep["delivered"]:
        print("DELIVERED the %s's answer: %s" % (rep["delivered"]["from"], rep["delivered"]["path"]))
    else:
        print("delivered nothing yet. %s" % _cmd("next"))
    base = rep["baseline"]
    if base:
        verdict = ("the delivered answer beats it" if base["better"] == "winner"
                   else "the answer you rejected scored higher. Say so")
        print("vs the answer you rejected: delivered %s, rejected %s, %s. %s"
              % (_fmt(base["totals"].get("winner")), _fmt(base["totals"].get("baseline")), verdict, base["reason"]))
    return 0


def cmd_card(args):
    state = load_state(args)
    _require_sized(state)
    aid = norm_agent(state, args.agent_id)
    a = state["agents"][aid]
    c = a["card"]
    print("%s  %s" % (aid, "alive" if a["alive"] else "eliminated in round %s by %s"
                      % (a["eliminated_in"], a["eliminated_by"])))
    for part in ("reasoning", "workflow", "strategy"):
        print("  %-9s %s. %s" % (part, c[part]["name"], c[part]["how"]))
    print("  solution  %s" % a["solution"])
    return 0


def _size_flags(s):
    s.add_argument("--n", "--agents", dest="n", type=int, help="competitors (default %d)" % DEFAULT_N)
    s.add_argument("--k", type=int, help="survivors who sit at the table (default %d)" % DEFAULT_K)
    s.add_argument("--table-rounds", type=int,
                   help="table rounds (default %d, max %d)" % (DEFAULT_TABLE_ROUNDS, MAX_TABLE_ROUNDS))
    s.add_argument("--quick", action="store_true", help="%d competitors, k unchanged" % QUICK_N)
    s.add_argument("--auto", action="store_true", help="a sizer sub-agent picks n, k and table rounds")
    s.add_argument("--max-n", type=int, default=DEFAULT_MAX_N, help="the cap on n for --auto (default %d)" % DEFAULT_MAX_N)
    s.add_argument("--wave", type=int, default=DEFAULT_WAVE, help="parallel sub-agents per wave (default %d)" % DEFAULT_WAVE)
    for role, model in DEFAULT_MODELS.items():
        s.add_argument("--model-" + role, choices=MODEL_CHOICES, default=model,
                       help="model for the %s jobs (default %s)" % (role, model))
    s.add_argument("--seed", type=int, help="fixes the cards and the pairings (default: random, recorded)")
    s.add_argument("--task-file", help="the task, word for word, as every competitor will get it")
    s.add_argument("--task", help="the task as a string, instead of --task-file")
    s.add_argument("--baseline-file", help="the answer the user was not satisfied with")


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", help="the run directory (default: the one in .round-table/LATEST)")
    p = argparse.ArgumentParser(prog="bracket.py", description="Tournament and table state for /round-table.")
    sub = p.add_subparsers(dest="cmd")
    sub.required = True

    s = sub.add_parser("plan", parents=[common], help="steps, sub-agent calls and waves for n, k and table rounds "
                                     "(takes init's flags; --seed and the files do not change the plan)")
    _size_flags(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("init", parents=[common], help="deal the cards and write round-table.json")
    _size_flags(s)
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("next", parents=[common], help="what to do now")
    s.set_defaults(func=cmd_next)

    for name, fn, hlp in (("prompts", cmd_prompts, "write the briefs for a phase and list the jobs left"),
                          ("check", cmd_check, "list the jobs whose outputs are missing")):
        s = sub.add_parser(name, parents=[common], help=hlp)
        s.add_argument("phase", choices=PHASES)
        s.set_defaults(func=fn)

    s = sub.add_parser("pairings", parents=[common], help="this round's matches, including the byes")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_pairings)

    s = sub.add_parser("record", parents=[common], help="record a match result by hand")
    s.add_argument("match_id")
    s.add_argument("winner_id")
    s.add_argument("--reason")
    s.add_argument("--survived", action="append", help="an attack the winner survived (repeatable)")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("collect", parents=[common], help="apply the sizing, or record the judges' verdicts")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("advance", parents=[common], help="close the bracket round or the table round")
    s.set_defaults(func=cmd_advance)

    s = sub.add_parser("status", parents=[common], help="alive and eliminated, per round")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("winner", parents=[common], help="the champion, the table and what was delivered")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_winner)

    s = sub.add_parser("card", parents=[common], help="one competitor's strategy card")
    s.add_argument("agent_id")
    s.set_defaults(func=cmd_card)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RoundTableError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
