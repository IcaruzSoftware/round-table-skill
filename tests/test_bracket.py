"""Tests for skills/round-table/bracket.py. Standard library only.

    python3 -m unittest discover -s tests -v

The headline test drives a full 100-agent tournament through the real command
line with random winners and checks it stops at exactly K survivors. The pipeline
tests drive every phase, tournament to delivered solution, with fake sub-agents.
"""
import collections
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SKILL = os.path.join(REPO, "skills", "round-table")
BRACKET = os.path.join(SKILL, "bracket.py")
sys.path.insert(0, SKILL)

import bracket as B  # noqa: E402

EM_DASH, EN_DASH = chr(0x2014), chr(0x2013)   # spelled as code points so this file stays clean


def cli(*args):
    return subprocess.run([sys.executable, BRACKET] + [str(a) for a in args],
                          capture_output=True, text=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load(d):
    with open(os.path.join(d, B.STATE_FILE), encoding="utf-8") as fh:
        return json.load(fh)


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="round-table-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ok(self, *args):
        r = cli(*args)
        self.assertEqual(r.returncode, 0, "bracket.py %s failed:\n%s%s" % (" ".join(map(str, args)), r.stdout, r.stderr))
        return r.stdout


class SimulatedTournament(TempDir):
    """Random winners, driven only through the CLI: init, pairings, record, advance, status, winner."""

    def run_tournament(self, n, k, seed):
        d = os.path.join(self.tmp, "run-%d-%d" % (n, k))
        self.ok("init", "--agents", n, "--k", k, "--seed", seed, "--task", "Simulated task.", "--dir", d)
        rng = random.Random(seed)
        alive_at_start = []
        while True:
            state = load(d)
            if state["champion"]:
                break
            rd = state["rounds"][-1]
            alive_at_start.append(len(rd["alive"]))
            out = self.ok("pairings", "--dir", d)
            self.assertIn("round %d:" % rd["n"], out)
            in_round = [x for m in rd["matches"] for x in (m["a"], m["b"])] + rd["byes"]
            self.assertEqual(sorted(in_round), sorted(rd["alive"]), "everyone alive plays once or gets a bye")
            self.assertEqual(len(in_round), len(set(in_round)))
            for m in rd["matches"]:
                self.ok("record", m["id"], rng.choice([m["a"], m["b"]]), "--reason", "simulated", "--dir", d)
            self.ok("advance", "--dir", d)
        alive_at_start.append(len(state["survivors"]))
        return d, load(d), alive_at_start

    def test_100_agents_stop_at_exactly_10(self):
        d, state, sizes = self.run_tournament(100, 10, 7)
        self.assertEqual(sizes, [100, 50, 25, 13, 10])
        self.assertEqual(len(state["rounds"]), 4)
        alive = sorted(a for a, v in state["agents"].items() if v["alive"])
        self.assertEqual(alive, state["survivors"])
        self.assertIn(state["champion"], alive)
        dead = [v for v in state["agents"].values() if not v["alive"]]
        self.assertEqual(len(dead), 90)
        self.assertTrue(all(v["eliminated_in"] and v["eliminated_by"] for v in dead))
        self.assertEqual(sum(len(rd["matches"]) for rd in state["rounds"]), 90)
        self.assertEqual([len(rd["byes"]) for rd in state["rounds"]], [0, 0, 1, 7])
        self.assertTrue(all(v["byes"] <= 1 for v in state["agents"].values()), "no agent gets two byes")
        status = self.ok("status", "--dir", d)
        self.assertIn("now: 10 alive, 90 eliminated, champion %s" % state["champion"], status)
        won = self.ok("winner", "--dir", d)
        self.assertIn("CHAMPION  %s" % state["champion"], won)
        self.assertIn("4 played, 100 agents in, 10 left", won)
        self.assertIn("tournament is over", self.ok("pairings", "--dir", d))

    def test_21_agents_k_10(self):
        _, state, sizes = self.run_tournament(21, 10, 5)
        self.assertEqual(sizes, [21, 11, 10])
        self.assertEqual([len(rd["matches"]) for rd in state["rounds"]], [10, 1])
        self.assertEqual([len(rd["byes"]) for rd in state["rounds"]], [1, 9])

    def test_20_agents_k_10_is_one_round_without_byes(self):
        _, state, sizes = self.run_tournament(20, 10, 2)
        self.assertEqual(sizes, [20, 10])
        self.assertEqual(len(state["rounds"][0]["matches"]), 10)
        self.assertEqual(state["rounds"][0]["byes"], [])

    def test_16_agents_k_4(self):
        _, state, sizes = self.run_tournament(16, 4, 3)
        self.assertEqual(sizes, [16, 8, 4])
        self.assertTrue(all(rd["byes"] == [] for rd in state["rounds"]))
        self.assertEqual(sum(v["alive"] for v in state["agents"].values()), 4)

    def test_7_agents_with_a_bye(self):
        _, state, sizes = self.run_tournament(7, 2, 11)
        self.assertEqual(sizes, [7, 4, 2])
        self.assertEqual(len(state["rounds"][0]["byes"]), 1)

    def test_byes_in_the_stop_round_go_to_the_fewest_byes(self):
        data = B.load_strategies()
        for seed in range(20):
            st = B.new_state(25, seed, data, os.path.join(self.tmp, "api"), k=10)
            rng = random.Random(seed)
            first_bye = st["rounds"][0]["byes"]
            self.assertEqual(len(first_bye), 1)
            for m in st["rounds"][0]["matches"]:
                B.record(st, m["id"], rng.choice([m["a"], m["b"]]))
            B.advance(st)
            rd = B.current_round(st)
            self.assertEqual((len(rd["alive"]), len(rd["matches"]), len(rd["byes"])), (13, 3, 7))
            self.assertNotIn(first_bye[0], rd["byes"], "a second bye only when nobody else is waiting")
            for m in rd["matches"]:
                B.record(st, m["id"], m["a"])
            B.advance(st)
            self.assertEqual(st["survivors"], sorted(rd["byes"] + [m["a"] for m in rd["matches"]]))

    def test_many_sizes_and_seeds_always_end_with_k(self):
        data = B.load_strategies()
        for n, k in ((3, 2), (5, 2), (9, 4), (11, 10), (16, 10), (31, 7), (64, 10), (100, 10), (101, 3), (250, 10)):
            for seed in range(3):
                st = B.new_state(n, seed, data, os.path.join(self.tmp, "api"), k=k)
                rng = random.Random(seed)
                sizes = []
                while not st["champion"]:
                    rd = B.current_round(st)
                    sizes.append(len(rd["alive"]))
                    for m in rd["matches"]:
                        B.record(st, m["id"], rng.choice([m["a"], m["b"]]))
                    B.advance(st)
                self.assertEqual(sizes + [k], B.bracket_sizes(n, k))
                self.assertEqual(len(B.alive_ids(st)), k)

    def test_champion_is_the_best_last_score(self):
        data = B.load_strategies()
        st = B.new_state(4, 1, data, os.path.join(self.tmp, "api"), k=2)
        m1, m2 = st["rounds"][0]["matches"]
        B.record(st, m1["id"], m1["a"], scores={m1["a"]: 60, m1["b"]: 50})
        B.record(st, m2["id"], m2["b"], scores={m2["a"]: 70, m2["b"]: 80})
        B.advance(st)
        self.assertEqual(st["champion"], m2["b"])


class Dealer(unittest.TestCase):
    def setUp(self):
        self.data = B.load_strategies()

    def test_card_count(self):
        self.assertEqual(len(self.data["reasoning"]), 15)
        self.assertEqual(len(self.data["workflows"]), 12)
        self.assertEqual(len(self.data["strategies"]), 12)
        self.assertEqual(B.combo_count(self.data), 2160)

    def check(self, n, seed):
        cards = B.deal(n, seed, self.data)
        self.assertEqual(len(cards), n)
        self.assertEqual(len(set(cards)), n, "no card is dealt twice")
        for dim, size in enumerate((15, 12, 12)):
            count = collections.Counter(c[dim] for c in cards)
            per = [count.get(i, 0) for i in range(size)]
            self.assertLessEqual(max(per) - min(per), 1, "balanced: n=%d seed=%d dim=%d %s" % (n, seed, dim, per))
        if n <= 144:
            for a, b in ((0, 1), (0, 2), (1, 2)):
                pairs = collections.Counter((c[a], c[b]) for c in cards)
                self.assertEqual(max(pairs.values()), 1, "two agents share two parts: n=%d seed=%d" % (n, seed))
        return cards

    def test_100_agents_unique_balanced_and_every_part_used(self):
        cards = self.check(100, 7)
        self.assertEqual(len({c[0] for c in cards}), 15)
        self.assertEqual(len({c[1] for c in cards}), 12)
        self.assertEqual(len({c[2] for c in cards}), 12)

    def test_guarantees_hold_across_sizes_and_seeds(self):
        for n in (1, 2, 7, 16, 32, 50, 99, 100, 101, 144):
            for seed in range(25):
                self.check(n, seed)
        for n in (145, 180, 500, 2160):
            self.check(n, 1)

    def test_deterministic_for_a_seed(self):
        self.assertEqual(B.deal(100, 42, self.data), B.deal(100, 42, self.data))
        self.assertNotEqual(B.deal(100, 42, self.data), B.deal(100, 43, self.data))

    def test_out_of_range(self):
        with self.assertRaises(B.RoundTableError):
            B.deal(0, 1, self.data)
        with self.assertRaises(B.RoundTableError):
            B.deal(2161, 1, self.data)

    def test_pairing_prefers_different_reasoning_modes(self):
        for seed in range(50):
            st = B.new_state(100, seed, self.data, "/tmp/unused")
            same = 0
            for m in st["rounds"][0]["matches"]:
                a, b = st["agents"][m["a"]], st["agents"][m["b"]]
                same += a["card"]["reasoning"]["id"] == b["card"]["reasoning"]["id"]
            self.assertLessEqual(same, 1, "at most one forced same-mode match in a 50-match round")


class Plan(unittest.TestCase):
    def test_plan_accepts_the_init_flags(self):
        r = cli("plan", "--n", 20, "--k", 4, "--seed", 3, "--task", "t", "--task-file", "x.md", "--baseline-file", "b.md")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("20 competitors, 4 at the table", r.stdout)

    def test_numbers(self):
        self.assertEqual(B.bracket_sizes(100, 10), [100, 50, 25, 13, 10])
        self.assertEqual(B.bracket_sizes(21, 10), [21, 11, 10])
        self.assertEqual(B.bracket_sizes(100, 1), [100, 50, 25, 13, 7, 4, 2, 1], "k 1 is the old arena")
        t = B.plan_totals(100, 10, 3)
        # 100 spawn + 90 matches x 5 + 10 pitches + 3 x (10 + 1 + 10) + (2 + 10 + 1) red team + 1 final
        self.assertEqual((t["rounds"], t["calls"], t["calls_min"], t["waves"]), (4, 637, 595, 73))
        t = B.plan_totals(16, 4, 1)
        self.assertEqual((t["rounds"], t["calls"]), (2, 16 + 60 + 4 + 9 + 7 + 1))
        t = B.plan_totals(100, 10, 3, auto=True, baseline=True)
        self.assertEqual(t["calls"], 639)

    def test_role_counts_add_up(self):
        for n, k, r in ((100, 10, 3), (16, 4, 1), (21, 10, 5)):
            t = B.plan_totals(n, k, r, auto=True, baseline=True)
            self.assertEqual(sum(t["roles"].values()), t["calls"])
            self.assertEqual(sum(t["by_model"].values()), t["calls"])

    def test_plan_cli(self):
        r = cli("plan", "--n", 100, "--k", 10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("     total                                     637     73", r.stdout)
        self.assertIn("100 -> 50 -> 25 -> 13 -> 10", r.stdout)
        self.assertEqual(cli("plan", "--n", 10, "--k", 10).returncode, 2)


class Verdicts(unittest.TestCase):
    def scores(self, c, m, s, r, cl, fatal=False):
        return {"correctness": c, "completeness": m, "specificity": s, "robustness": r, "clarity": cl, "fatal": fatal}

    def test_weights_match_the_rubric(self):
        with open(os.path.join(SKILL, "rubric.md"), encoding="utf-8") as fh:
            rows = re.findall(r"^\| (\w+) \| (\d+) \|", fh.read(), re.M)
        self.assertEqual([(k.lower(), int(w)) for k, w in rows], list(B.WEIGHTS))
        self.assertEqual(sum(w for _, w in B.WEIGHTS), 100)

    def test_total(self):
        self.assertEqual(B.weighted_total(self.scores(10, 10, 10, 10, 10)), 100.0)
        self.assertEqual(B.weighted_total(self.scores(8, 6, 7, 5, 9)), 68.5)   # (240 + 150 + 105 + 100 + 90) / 10
        self.assertIsNone(B.weighted_total({"correctness": 8}))

    def test_scores_beat_the_judges_pick(self):
        v = {"scores": {"a001": self.scores(5, 5, 5, 5, 5), "a002": self.scores(8, 8, 8, 8, 8)}, "winner": "a001"}
        w, totals, note = B.decide(v, "a001", "a002")
        self.assertEqual(w, "a002")
        self.assertIn("scores win", note)

    def test_fatal_rule(self):
        v = {"scores": {"a001": self.scores(9, 9, 9, 9, 9, fatal=True), "a002": self.scores(4, 4, 4, 4, 4)}}
        self.assertEqual(B.decide(v, "a001", "a002")[0], "a002")

    def test_tie_goes_to_fewer_standing_attacks(self):
        v = {"scores": {"a001": self.scores(7, 7, 7, 7, 7), "a002": self.scores(7, 7, 7, 7, 7)},
             "standing": {"a001": ["x", "y"], "a002": ["z"]}}
        self.assertEqual(B.decide(v, "a001", "a002")[0], "a002")

    def test_incomplete_scores_fall_back_to_the_pick(self):
        v = {"scores": {"a001": {"correctness": 3}}, "winner": "a001"}
        self.assertEqual(B.decide(v, "a001", "a002")[0], "a001")
        with self.assertRaises(B.RoundTableError):
            B.decide({"scores": {}}, "a001", "a002")

    def test_json_inside_a_code_fence(self):
        self.assertEqual(B.extract_json('```json\n{"winner": "a001"}\n```'), {"winner": "a001"})
        self.assertIsNone(B.extract_json("no json here"))

    def test_votes(self):
        self.assertEqual(B.parse_vote("ACCEPT\n"), ("ACCEPT", ""))
        self.assertEqual(B.parse_vote("accept. looks right"), ("ACCEPT", ""))
        self.assertEqual(B.parse_vote('OBJECT "Step 2": fails for an empty list'),
                         ("OBJECT", '"Step 2": fails for an empty list'))
        self.assertEqual(B.parse_vote("NO OUTPUT"), ("ABSTAIN", ""))
        self.assertIsNone(B.parse_vote("OBJECT"), "an objection with no content is not concrete")
        self.assertIsNone(B.parse_vote("I think it is fine"))


class AutoSizing(unittest.TestCase):
    def test_guardrails_clamp(self):
        self.assertEqual(B.apply_sizing({"n": 500, "k": 50, "table_rounds": 9}, {})[:3], (100, 10, 5))
        self.assertEqual(B.apply_sizing({"n": 3, "k": 1, "table_rounds": 0}, {})[:3], (8, 2, 1))
        self.assertEqual(B.apply_sizing({"n": 12, "k": 10, "table_rounds": 2}, {})[:3], (12, 6, 2), "k at most n // 2")
        self.assertEqual(B.apply_sizing({"n": 90, "k": 8, "table_rounds": 2}, {}, max_n=40)[:3], (40, 8, 2))
        self.assertEqual(B.apply_sizing({"n": "32", "k": 4, "table_rounds": 2, "reason": "r"}, {}), (32, 4, 2, "r"))

    def test_invalid_json_falls_back_to_the_defaults(self):
        for raw in (None, [], {"n": 20}, {"n": "many", "k": 3, "table_rounds": 1}, {"n": True, "k": 3, "table_rounds": 1}):
            n, k, r, note = B.apply_sizing(raw, {})
            self.assertEqual((n, k, r), (100, 10, 3), raw)
            self.assertIn("defaults", note)

    def test_explicit_flags_win(self):
        raw = {"n": 40, "k": 8, "table_rounds": 4}
        self.assertEqual(B.apply_sizing(raw, {"k": 3})[:3], (40, 3, 4))
        self.assertEqual(B.apply_sizing(raw, {"n": 16, "table_rounds": 1})[:3], (16, 8, 1))
        self.assertEqual(B.apply_sizing({"n": 8, "k": 2, "table_rounds": 1}, {"k": 9})[:3], (10, 9, 1),
                         "an explicit k above the auto n lifts n to k + 1")

    def test_explicit_n_clamps_the_auto_k(self):
        raw = {"n": 100, "k": 10, "table_rounds": 3}
        self.assertEqual(B.apply_sizing(raw, {"n": 8})[:3], (8, 4, 3), "k at most the explicit n // 2")
        self.assertEqual(B.apply_sizing(raw, {"n": B.QUICK_N})[:3], (16, 8, 3), "--auto --quick")

    def test_max_n_is_capped_at_the_distinct_cards(self):
        cards = B.combo_count(B.load_strategies())
        self.assertEqual(B.apply_sizing({"n": 5000, "k": 10, "table_rounds": 3}, {}, max_n=cards)[0], cards)
        tmp = tempfile.mkdtemp(prefix="round-table-maxn-")
        try:
            r = cli("init", "--auto", "--max-n", cards + 1, "--task", "t", "--dir", os.path.join(tmp, "run"))
            self.assertEqual(r.returncode, 2)
            self.assertIn("--max-n must be at most %d" % cards, r.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_auto_through_the_cli(self):
        tmp = tempfile.mkdtemp(prefix="round-table-auto-")
        try:
            d = os.path.join(tmp, "run")
            r = cli("init", "--auto", "--k", 4, "--model-sizer", "opus", "--seed", 3, "--task", "t", "--dir", d)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("NEXT: size, model opus", cli("next", "--dir", d).stdout)
            listing = cli("prompts", "size", "--dir", d).stdout
            self.assertRegex(listing, r"size\s+opus\s+\S+size\.md")
            write(B.sizing_out(d), '{"n": 50, "k": 8, "table_rounds": 2, "reason": "a medium task"}')
            self.assertIn("NEXT: collect", cli("next", "--dir", d).stdout)
            r = cli("collect", "--dir", d)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("auto: n 50, k 4, table rounds 2, from your flags: k. a medium task", r.stdout)
            st = load(d)
            self.assertEqual((st["agents_n"], st["k"], st["table_rounds"]), (50, 4, 2))
            self.assertEqual(st["models"]["sizer"], "opus")
            self.assertIn("NEXT: spawn, model haiku", cli("next", "--dir", d).stdout)
            d2 = os.path.join(tmp, "run2")
            cli("init", "--auto", "--task", "t", "--dir", d2)
            write(B.sizing_out(d2), "the sizer wrote prose")
            self.assertIn("auto: n 100, k 10, table rounds 3. sizer JSON invalid or missing, using the defaults",
                          cli("collect", "--dir", d2).stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FullPipelineWithFakeAgents(TempDir):
    """Every phase, through the CLI, with fake sub-agents writing the files real ones would."""

    TASK = "Write a haiku about the sea.\nIt must mention salt.\nLiteral braces stay: {{name}} and {not_a_placeholder}."

    def fake_outputs(self, state, phase, jobs, rng, vote, final_winner):
        for j in jobs:
            o = j["outputs"]
            if phase == "spawn":
                write(o[0], "Salt on the wind, from %s." % j["agent"])
            elif phase in ("attack", "redteam"):
                write(o[0], "ATTACK 1 [MINOR] too short\nWhere: line 1\nProblem: it is short.\n")
            elif phase == "defend":
                write(o[0], "ATTACK 1: CONCEDE. Made it longer.\n")
                write(o[1], "Salt on the long wind, revised by %s." % j["agent"])
            elif phase == "judge":
                a, b = j["a"], j["b"]
                s = lambda: {k: rng.randint(3, 10) for k, _ in B.WEIGHTS}
                v = {"match": j["match"], "scores": {a: s(), b: s()}, "winner": a, "reason": "fake",
                     "survived": ["too short"], "standing": {a: [], b: []}}
                write(o[0], json.dumps(v))
            elif phase == "pitch":
                write(o[0], "STRONGEST IDEAS: salt.\nCONCEDED AND FIXED: length.\nLIKELY MISSED: foam.\n")
            elif phase == "contribute":
                write(o[0], "ITEM 1 ADOPT a001: salt. Reason: required.\n")
            elif phase == "scribe":
                write(o[0], "Salt and foam, table draft %d." % j["table_round"])
                write(o[1], "## Table round %d\nACCEPTED a001 ADOPT salt: required.\n" % j["table_round"])
            elif phase == "vote":
                write(o[0], vote(j))
            elif phase == "rebut":
                write(o[0], "rt1 ATTACK 1: CONCEDE. Longer.\n")
            elif phase == "finalize":
                write(o[0], "Salt and foam, final table answer.")
                write(o[1], "## Red team\nFIXED rt1 ATTACK 1 too short: longer.\n")
            elif phase in ("final", "baseline"):
                win = final_winner(state, phase)
                lose = "Y" if win == "X" else "X"
                v = {"scores": {win: {k: 8 for k, _ in B.WEIGHTS}, lose: {k: 4 for k, _ in B.WEIGHTS}},
                     "winner": win, "reason": "fake", "fixed": ["salt"]}
                write(o[0], json.dumps(v))

    def drive(self, d, vote, final_winner, seed=9):
        rng = random.Random(seed)
        seen = []
        for _ in range(300):
            out = self.ok("next", "--dir", d)
            phase = re.search(r"^(NEXT: (\w+)|DONE)", out, re.M)
            phase = "done" if phase.group(0) == "DONE" else phase.group(2)
            seen.append(phase)
            if phase == "done":
                return seen
            if phase in ("collect", "advance"):
                self.ok(phase, "--dir", d)
                continue
            state = load(d)
            model = state["models"][B.ROLE_OF[B.KIND_OF[phase]]]
            self.assertIn("NEXT: %s, model %s." % (phase, model), out)
            listing = self.ok("prompts", phase, "--dir", d)
            state = load(d)
            jobs = B.phase_jobs(state, phase)
            for j in jobs:
                self.assertIn("  %-26s %-6s %s" % (j["id"], model, j["prompt"]), listing, "every job lists its model")
                brief = read(j["prompt"])
                self.assertNotRegex(brief.replace("{{name}}", ""), r"\{\{\w+\}\}", "unfilled placeholder")
                if phase == "spawn":
                    task_block = brief.split("=== THE TASK (identical for every competitor) ===\n")[1]
                    self.assertEqual(task_block.split("\n=== END OF THE TASK ===")[0], self.TASK)
                if phase in ("judge", "final", "baseline", "redteam"):
                    self.assertNotIn("Reasoning mode", brief, "judges and the red team never see the cards")
            self.fake_outputs(state, phase, jobs, rng, vote, final_winner)
            self.ok("check", phase, "--dir", d)
        self.fail("never reached DONE: %s" % seen[-20:])

    def init(self, name, *flags):
        d = os.path.join(self.tmp, name)
        task_file = os.path.join(self.tmp, "task.md")
        write(task_file, self.TASK)
        self.ok("init", "--seed", 9, "--task-file", task_file, "--dir", d, *flags)
        return d

    def test_every_phase_to_done_and_the_table_wins(self):
        base_file = os.path.join(self.tmp, "baseline.md")
        write(base_file, "The sea is big.")
        d = self.init("run", "--n", 5, "--k", 2, "--table-rounds", 2, "--baseline-file", base_file,
                      "--model-judge", "opus")
        self.assertTrue(os.path.isfile(os.path.join(d, "rubric.md")), "the run has its own rubric copy")
        objected = []

        def vote(j):   # one objection in table round 1, all accept in round 2
            if j["table_round"] == 1 and not objected:
                objected.append(j["agent"])
                return 'OBJECT "line 1": no foam.'
            return "ACCEPT"

        pick = lambda st, phase: "X" if st["checks"][phase]["X"] in ("table", "winner") else "Y"
        seen = self.drive(d, vote, pick)
        self.assertEqual(seen[-1], "done")
        for phase in ("spawn", "judge", "pitch", "contribute", "scribe", "vote", "redteam", "rebut",
                      "finalize", "final", "baseline"):
            self.assertIn(phase, seen)
        self.assertEqual(seen.count("contribute"), 2, "the objection forced a second table round")
        self.assertEqual(seen.count("advance"), 2 + 2, "2 bracket rounds (5 -> 3 -> 2), 2 table rounds")
        st = load(d)
        self.assertEqual(len(st["survivors"]), 2)
        self.assertEqual(st["delivered"]["from"], "table")
        self.assertEqual(read(os.path.join(d, "solution.md")), "Salt and foam, final table answer.")
        log = read(os.path.join(d, "table", "log.md"))
        self.assertIn("## Table round 1", log)
        self.assertIn("## Table round 2", log)
        self.assertIn("## Red team", log)
        self.assertEqual(read(os.path.join(d, "table", "draft.md")), "Salt and foam, table draft 2.")
        pair = {read(os.path.join(d, "baseline", "X.md")).strip(), read(os.path.join(d, "baseline", "Y.md")).strip()}
        self.assertEqual(pair, {"The sea is big.", "Salt and foam, final table answer."})
        rep = json.loads(self.ok("winner", "--json", "--dir", d))
        self.assertEqual(rep["final"]["better"], "table")
        self.assertEqual(rep["baseline"]["better"], "winner")
        self.assertIn("DELIVERED the table's answer", self.ok("winner", "--dir", d))

    def test_table_stops_early_and_the_champion_can_win(self):
        d = self.init("early", "--n", 6, "--k", 3, "--table-rounds", 3)
        pick = lambda st, phase: "X" if st["checks"][phase]["X"] == "champion" else "Y"
        seen = self.drive(d, lambda j: "ACCEPT", pick)
        self.assertEqual(seen.count("contribute"), 1, "everyone accepted in table round 1")
        st = load(d)
        self.assertTrue(st["table"]["stopped_early"])
        self.assertNotIn("baseline", seen)
        self.assertEqual(st["delivered"]["from"], "champion", "never deliver what the champion beat")
        champ = st["agents"][st["champion"]]["solution"]
        self.assertEqual(read(os.path.join(d, "solution.md")), read(champ))

    def test_objections_run_all_table_rounds_and_abstentions_do_not_block(self):
        pick = lambda st, phase: "X"
        d = self.init("all", "--n", 6, "--k", 3, "--table-rounds", 2)
        seen = self.drive(d, lambda j: 'OBJECT "all": salt is missing in line 2.', pick)
        self.assertEqual(seen.count("contribute"), 2)
        self.assertFalse(load(d)["table"]["stopped_early"])
        self.assertIn("salt is missing", read(os.path.join(d, "table", "objections.md")))

    def test_standing_objections_reach_the_red_team_and_the_scribe(self):
        pick = lambda st, phase: "X"
        d = self.init("obj", "--n", 6, "--k", 3, "--table-rounds", 1)
        self.drive(d, lambda j: 'OBJECT "all": salt is missing in line 2.', pick)
        seat = load(d)["survivors"][0]
        for job in ("rt1.attack", "finalize"):
            brief = read(B.prompt_path(d, "redteam", job))
            self.assertIn('- %s: "all": salt is missing in line 2.' % seat, brief, job)
        d = self.init("noobj", "--n", 6, "--k", 3, "--table-rounds", 1)
        self.drive(d, lambda j: "ACCEPT", pick)
        for job in ("rt1.attack", "finalize"):
            self.assertIn("Objections still standing from the table: none", read(B.prompt_path(d, "redteam", job)))

    def test_a_table_of_abstentions_does_not_converge(self):
        pick = lambda st, phase: "X"
        d = self.init("abstain", "--n", 6, "--k", 3, "--table-rounds", 3)
        seen = self.drive(d, lambda j: "NO OUTPUT", pick)
        self.assertEqual(seen.count("contribute"), 3, "abstentions are not consensus")
        st = load(d)
        self.assertFalse(st["table"]["stopped_early"])
        self.assertIn("table did not converge", read(os.path.join(d, "table", "log.md")))
        self.assertEqual(seen[-1], "done", "the red team still runs")

    def test_unreadable_vote_is_set_aside(self):
        d = self.init("bad-vote", "--n", 3, "--k", 2, "--table-rounds", 1)
        st = load(d)
        for m in st["rounds"][0]["matches"]:
            self.ok("record", m["id"], m["a"], "--dir", d)
        self.ok("advance", "--dir", d)
        st = load(d)
        for aid in st["agents"]:
            write(B.spawn_out(d, aid), "x")
        self.ok("prompts", "pitch", "--dir", d)
        write(B.scribe_draft_out(d, 1), "draft")
        write(B.scribe_log_out(d, 1), "log")
        for aid in st["survivors"]:
            write(B.vote_out(d, 1, aid), "maybe")
        r = cli("advance", "--dir", d)
        self.assertEqual(r.returncode, 2)
        self.assertIn("unreadable", r.stderr)
        self.assertTrue(os.path.exists(B.vote_out(d, 1, st["survivors"][0]) + ".unreadable"))

    def test_spawn_briefs_share_one_task_and_differ_in_card(self):
        d = os.path.join(self.tmp, "run16")
        self.ok("init", "--quick", "--seed", 4, "--task", self.TASK, "--dir", d)
        self.ok("prompts", "spawn", "--dir", d)
        blocks, cards = set(), set()
        for name in sorted(os.listdir(os.path.join(d, "prompts", "r0"))):
            brief = read(os.path.join(d, "prompts", "r0", name))
            blocks.add(brief.split("=== THE TASK (identical for every competitor) ===\n")[1].split("\n=== END")[0])
            cards.add(brief.split("=== YOUR STRATEGY CARD ===")[1].split("=== END OF THE CARD ===")[0])
        self.assertEqual(len(os.listdir(os.path.join(d, "prompts", "r0"))), 16)
        self.assertEqual(blocks, {self.TASK}, "every competitor gets the exact same task text")
        self.assertEqual(len(cards), 16, "every competitor gets a different card")

    def test_unreadable_verdict_is_set_aside_and_rerun(self):
        d = os.path.join(self.tmp, "bad")
        self.ok("init", "--agents", 3, "--k", 2, "--seed", 1, "--task", "t", "--dir", d)
        state = load(d)
        m = state["rounds"][0]["matches"][0]
        path = B.verdict_out(d, 1, m["id"])
        write(path, "the judge rambled and wrote no json")
        r = cli("collect", "--dir", d)
        self.assertEqual(r.returncode, 1)
        self.assertIn("unreadable", r.stdout)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(path + ".unreadable"))


class Guards(TempDir):
    def test_init_refuses_to_overwrite_and_bad_sizes(self):
        d = os.path.join(self.tmp, "run")
        self.ok("init", "--agents", 4, "--k", 2, "--task", "t", "--dir", d)
        bad = (("--agents", 4, "--k", 2),                 # the run already exists
               ("--n", 10, "--k", 10), ("--n", 9, "--k", 10), ("--n", 5, "--k", 1),
               ("--n", 2161, "--k", 2), ("--quick", "--agents", 8),
               ("--n", 20, "--k", 4, "--table-rounds", 6), ("--n", 20, "--k", 4, "--table-rounds", 0),
               ("--n", 20, "--k", 4, "--wave", 0), ("--n", 20, "--model-seat", "gpt"))
        for i, flags in enumerate(bad):
            r = cli("init", *flags, "--task", "t", "--dir", d if i == 0 else d + str(i))
            self.assertEqual(r.returncode, 2, "should refuse %s" % (flags,))
        self.assertEqual(cli("init", "--n", 4, "--k", 2, "--task", "   ", "--dir", d + "x").returncode, 2)

    def test_quick_is_16_and_keeps_k(self):
        d = os.path.join(self.tmp, "quick")
        self.ok("init", "--quick", "--task", "t", "--dir", d)
        st = load(d)
        self.assertEqual((st["agents_n"], st["k"], st["table_rounds"]), (16, 10, 3))
        self.assertEqual(st["models"], B.DEFAULT_MODELS)

    def test_record_rejects_an_outsider(self):
        d = os.path.join(self.tmp, "rec")
        self.ok("init", "--agents", 4, "--k", 2, "--seed", 2, "--task", "t", "--dir", d)
        m = load(d)["rounds"][0]["matches"][0]
        outsider = next(a for a in load(d)["agents"] if a not in (m["a"], m["b"]))
        self.assertEqual(cli("record", m["id"], outsider, "--dir", d).returncode, 2)
        self.assertEqual(cli("advance", "--dir", d).returncode, 2, "cannot advance with open matches")

    def test_templates_are_all_in_skill_md(self):
        self.assertEqual(sorted(B.load_templates()), sorted(B.TEMPLATES))

    def test_every_role_has_a_flag_and_a_valid_default(self):
        self.assertEqual(set(B.ROLE_OF.values()), set(B.DEFAULT_MODELS))
        self.assertTrue(all(m in B.MODEL_CHOICES for m in B.DEFAULT_MODELS.values()))

    def test_no_em_dashes_anywhere(self):
        for root, dirs, files in os.walk(REPO):
            dirs[:] = [x for x in dirs if x not in (".git", "__pycache__")]
            for name in files:
                path = os.path.join(root, name)
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                self.assertNotIn(EM_DASH, text, "em dash in %s" % os.path.relpath(path, REPO))
                self.assertNotIn(EN_DASH, text, "en dash in %s" % os.path.relpath(path, REPO))


if __name__ == "__main__":
    unittest.main(verbosity=2)
