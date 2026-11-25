"""The handover package's own test suite (vectors TV1-TV17, regression, 10,000-case property tests, config parity), unchanged
except for two lines: the import (the reference scoring is vendored as app.services.fallguard_scoring) and the folder the
questionnaire JSON is read from. They are unittest classes; pytest collects them, and ``python -m unittest`` works too.
"""
import json
import os
import random
import unittest

from app.services import fallguard_scoring as fg
from app.services.fallguard_scoring import RANK, UNKNOWN, ValidationError, score_assessment

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "services", "fallguard_data")
ON = "2026-11-10"
DOB_62 = "1964-01-01"   # age 62 on ON  -> B1 = 1
DOB_96 = "1930-01-01"   # age 96        -> B1 = 3
DOB_55 = "1971-01-01"   # age 55        -> ineligible

A_ZERO = {"A1": 0, "A2": 0, "A3": 0, "A4": 0, "A5": 0, "A6": 0, "A7": 0}
NO_FLAGS = {}


def b_zero():
    b = {k: 0 for k in fg.SINGLE_MAX}
    b.update({"B8": ["NONE"], "B14": ["NONE"], "B17": ["NONE"]})
    return b


def run(dob=DOB_62, a=None, b=None, c=None, red=None):
    pa = dict(A_ZERO)
    pa.update(a or {})
    pb = b_zero()
    pb.update(b or {})
    return score_assessment({"date_of_birth": dob, "assessment_date": ON,
                             "part_a": pa, "part_b": pb, "part_c": c or {},
                             "red_flags": red or {}})


class TestVectors(unittest.TestCase):
    def test_tv1_low(self):
        r = run()
        self.assertEqual((r["score"], r["final_tier"]), (1, "LOW"))

    def test_tv2_two_falls_high(self):
        self.assertEqual(run(a={"A1": 2})["final_tier"], "HIGH")

    def test_tv3_one_fall_intermediate(self):
        r = run(a={"A1": 1})
        self.assertEqual((r["gate_tier"], r["score_tier"], r["final_tier"]),
                         ("INTERMEDIATE", "LOW", "INTERMEDIATE"))

    def test_tv4_fall_and_often_unsteady_high(self):
        self.assertEqual(run(a={"A1": 1, "A5": 2})["final_tier"], "HIGH")

    def test_tv5_fainting_high(self):
        self.assertEqual(run(a={"A4": 1})["final_tier"], "HIGH")

    def test_tv5b_injury_and_floor_high(self):
        self.assertEqual(run(a={"A1": 1, "A2": 1})["final_tier"], "HIGH")
        self.assertEqual(run(a={"A1": 1, "A3": 1})["final_tier"], "HIGH")

    def test_tv6_total5_low(self):
        r = run(b={"B3": 2, "B4": 2})
        self.assertEqual((r["score"], r["final_tier"]), (5, "LOW"))

    def test_tv7_total6_intermediate(self):
        r = run(b={"B3": 2, "B4": 2, "B13": 1})
        self.assertEqual((r["score"], r["final_tier"]), (6, "INTERMEDIATE"))

    def test_tv8_total11_intermediate(self):
        r = run(b={"B3": 2, "B4": 2, "B5": 2, "B6": 2, "B9": 1, "B13": 1})
        self.assertEqual((r["score"], r["final_tier"]), (11, "INTERMEDIATE"))

    def test_tv9_total12_high(self):
        r = run(b={"B3": 2, "B4": 2, "B5": 2, "B6": 2, "B9": 1, "B13": 1, "B19": 1})
        self.assertEqual((r["score"], r["final_tier"]), (12, "HIGH"))

    def test_tv10_max_score_36(self):
        b = {k: v for k, v in fg.SINGLE_MAX.items()}
        b["B8"] = ["STROKE", "DIABETES", "HEART"]
        b["B14"] = ["SEDATIVES", "OPIOIDS"]
        b["B17"] = ["LOOSE_RUGS", "CLUTTER", "NO_HANDRAIL"]
        r = run(dob=DOB_96, b=b)
        self.assertEqual((r["score"], r["final_tier"]), (36, "HIGH"))

    def test_tv11_incomplete_is_provisional_and_not_lowered(self):
        unk = {k: UNKNOWN for k in ("B3", "B4", "B5", "B6")}   # 4 unknown = 4 points
        r = run(b=dict(unk, B9=2, B10=2, B11=2, B12=1))        # 1 + 4 + 7 = 12
        self.assertTrue(r["incomplete"] and r["provisional"])
        self.assertEqual((r["score"], r["final_tier"]), (12, "HIGH"))

    def test_tv11b_three_unknown_not_incomplete(self):
        r = run(b={k: UNKNOWN for k in ("B3", "B4", "B5")})
        self.assertFalse(r["incomplete"])
        self.assertEqual(r["score"], 4)

    def test_tv12_head_blow_on_blood_thinner(self):
        r = run(c={"C5": 1}, red={"R1": True})
        self.assertTrue(r["urgent"] and r["consequence_flag"])

    def test_tv13_each_red_flag(self):
        for k in ("R2", "R3", "R4", "R5", "R6", "R7"):
            self.assertTrue(run(red={k: True})["urgent"], k)
        r = run(c={"C5": 0}, red={"R1": True})
        self.assertFalse(r["urgent"])
        self.assertTrue(r["prompt_attention"])
        self.assertTrue(run(red={"R1": True})["urgent"])            # C5 unanswered: cautious
        self.assertTrue(run(c={"C5": 9}, red={"R1": True})["urgent"])

    def test_tv14_under_60(self):
        r = run(dob=DOB_55, red={"R6": True})
        self.assertFalse(r["eligible"])
        self.assertIsNone(r["final_tier"])
        self.assertTrue(r["urgent"])

    def test_tv15_override(self):
        r = run(a={"A1": 2})
        clin = {"credentialed": True, "id": "n1"}
        o = fg.apply_override(r, "INTERMEDIATE", "OBJECTIVE_TEST_FINDINGS", "Normal TUG", clin)
        self.assertTrue(o["override"]["lowered_below_gate"])
        self.assertEqual(o["original"]["final_tier"], "HIGH")
        with self.assertRaises(ValidationError):
            fg.apply_override(r, "LOW", "OTHER", "", clin)
        with self.assertRaises(ValidationError):
            fg.apply_override(r, "LOW", "OTHER", "x", {"credentialed": False})
        with self.assertRaises(ValidationError):
            fg.apply_override(r, "LOW", "BAD", "x", clin)

    def test_tv16_b14_not_sure(self):
        r = run(b={"B14": ["NOT_SURE"]})
        self.assertEqual(r["part_b_points"]["B14"], 1)
        self.assertTrue(r["pharmacist_review_flag"])
        self.assertNotIn("B14", r["unknown_items"])

    def test_tv17_consequence_flag(self):
        self.assertTrue(run(c={"C1": 1})["consequence_flag"])
        self.assertFalse(run(c={"C1": 1, "C2": "DAILY"})["consequence_flag"])
        self.assertTrue(run(c={"C6": 9})["consequence_flag"])
        self.assertFalse(run()["consequence_flag"])

    def test_v1_regression_recurrent_faller_not_low(self):
        # 61-year-old, 3 falls, otherwise healthy: v1 scored 2 and said Low
        r = run(dob="1965-06-01", a={"A1": 2})
        self.assertEqual(r["final_tier"], "HIGH")

    def test_conditional_part_a(self):
        r = run(a={"A1": 0, "A2": 1, "A3": 1, "A7": 1})   # ignored when A1 = 0
        self.assertEqual(r["final_tier"], "LOW")
        with self.assertRaises(ValidationError):
            score_assessment({"date_of_birth": DOB_62, "assessment_date": ON,
                              "part_a": {"A1": 1, "A4": 0, "A5": 0, "A6": 0},
                              "part_b": b_zero()})

    def test_validation_errors(self):
        for bad in ({"B3": 3}, {"B8": ["NONE", "STROKE"]}, {"B8": []}, {"B14": ["X"]},
                    {"B2": True}, {"B17": "LOOSE_RUGS"}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                run(b=bad)
        with self.assertRaises(ValidationError):
            run(red={"R9": True})

    def test_top_factors_exclude_age_and_order(self):
        r = run(dob=DOB_96, b={"B3": 2, "B4": 2, "B5": 1, "B6": 1})
        self.assertEqual(r["top_factors"], ["B3", "B4", "B5"])


def random_payload(rng):
    a1 = rng.randint(0, 2)
    pa = {"A1": a1, "A2": rng.randint(0, 1), "A3": rng.randint(0, 1), "A4": rng.randint(0, 1),
          "A5": rng.randint(0, 2), "A6": rng.randint(0, 2), "A7": rng.randint(0, 1)}
    pb = {}
    for k, mx in fg.SINGLE_MAX.items():
        pb[k] = UNKNOWN if rng.random() < 0.1 else rng.randint(0, mx)
    for k, cfg in fg.MULTI.items():
        roll = rng.random()
        if roll < 0.1:
            pb[k] = UNKNOWN
        elif roll < 0.25:
            pb[k] = ["NONE"]
        elif k == "B14" and roll < 0.3:
            pb[k] = ["NOT_SURE"]
        else:
            opts = sorted(cfg["options"])
            pb[k] = rng.sample(opts, rng.randint(1, len(opts)))
    red = {k: rng.random() < 0.05 for k in fg.RED_FLAG_IDS}
    c = {"C1": rng.randint(0, 1), "C2": rng.choice(["DAILY", "SOMETIMES", "RARELY"]),
         "C5": rng.choice([0, 1, 9]), "C6": rng.choice([0, 1, 9])}
    return {"date_of_birth": "%d-03-15" % rng.randint(1925, 1966), "assessment_date": ON,
            "part_a": pa, "part_b": pb, "part_c": c, "red_flags": red}


class TestProperties(unittest.TestCase):
    N = 10000

    def test_floor_urgent_and_determinism(self):
        rng = random.Random(20261110)
        for _ in range(self.N):
            p = random_payload(rng)
            r = score_assessment(p)
            self.assertEqual(r, score_assessment(p))                       # deterministic
            exp = fg.evaluate_red_flags(p["red_flags"], p["part_c"]["C5"])["urgent"]
            self.assertEqual(r["urgent"], exp)
            if r["eligible"]:
                self.assertGreaterEqual(RANK[r["final_tier"]], RANK[r["gate_tier"]])  # SR-4
                self.assertGreaterEqual(RANK[r["final_tier"]], RANK[r["score_tier"]])
                self.assertTrue(0 <= r["score"] <= 36)

    def test_monotonic_in_part_b(self):
        rng = random.Random(7)
        for _ in range(self.N):
            p = random_payload(rng)
            known = [k for k, mx in fg.SINGLE_MAX.items()
                     if p["part_b"][k] != UNKNOWN and p["part_b"][k] < mx]
            if not known:
                continue
            k = rng.choice(known)
            before = score_assessment(p)
            if not before["eligible"]:
                continue
            p["part_b"][k] += 1
            after = score_assessment(p)
            self.assertGreaterEqual(after["score"], before["score"])
            self.assertGreaterEqual(RANK[after["final_tier"]], RANK[before["final_tier"]])
            self.assertEqual(after["urgent"], before["urgent"])             # unaffected


class TestConfigParity(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "questionnaire_v2_0.json"), encoding="utf-8") as f:
            self.cfg = json.load(f)
        self.items = {i["id"]: i for i in self.cfg["items"]}

    def test_versions_and_cutoffs(self):
        self.assertEqual(self.cfg["questionnaire_version"], fg.QUESTIONNAIRE_VERSION)
        self.assertEqual(self.cfg["algorithm_version"], fg.ALGORITHM_VERSION)
        sc = self.cfg["score_config"]
        self.assertEqual(sc["intermediate_from"], fg.CONFIG["intermediate_from"])
        self.assertEqual(sc["high_from"], fg.CONFIG["high_from"])
        self.assertEqual(sc["max_unknown_before_incomplete"], fg.CONFIG["max_unknown_before_incomplete"])
        self.assertEqual(sc["min_age"], fg.CONFIG["min_age"])

    def test_item_ids(self):
        expected = ["A%d" % i for i in range(1, 8)] + fg.PART_B_ITEMS + ["C%d" % i for i in range(1, 9)]
        self.assertEqual(sorted(self.items), sorted(expected))

    def test_single_items_match_engine(self):
        for k, mx in fg.SINGLE_MAX.items():
            it = self.items[k]
            codes = [o["code"] for o in it["options"]]
            self.assertEqual(codes, list(range(mx + 1)), k)
            self.assertEqual([o["points"] for o in it["options"]], codes, k)
            self.assertTrue(it["allow_unknown"], k)
            self.assertIn("explain", it, k)

    def test_multi_items_match_engine(self):
        for k, cfg in fg.MULTI.items():
            it = self.items[k]
            codes = {o["code"] for o in it["options"]}
            extra = {"NONE"} | ({"NOT_SURE"} if k == "B14" else set())
            self.assertEqual(codes, set(cfg["options"]) | extra, k)
            bands = sorted((b["min_count"], b["points"]) for b in it["scoring"]["bands"])
            eng = sorted([(0, 0)] + [tuple(b) for b in cfg["bands"]])
            self.assertEqual(bands, eng, k)

    def test_max_points(self):
        total = 3 + sum(fg.SINGLE_MAX.values()) + 2 * len(fg.MULTI)
        self.assertEqual(total, 36)

    def test_red_flags(self):
        self.assertEqual([r["id"] for r in self.cfg["red_flags"]], list(fg.RED_FLAG_IDS))

    def test_part_a_conditions(self):
        for k in ("A2", "A3", "A7"):
            self.assertEqual(self.items[k]["show_if"], {"item": "A1", "op": ">=", "value": 1})


if __name__ == "__main__":
    unittest.main()
