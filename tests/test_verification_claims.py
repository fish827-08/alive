import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selfdrive import claims, conceptcard, learning, state, teacher_verdict


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(state, "LOGS", root / "logs"),
            mock.patch.object(state, "NOTES", root / "notes"),
        ]
        for patch in self.patches:
            patch.start()
        self.st = state.default_state()
        self.st["rounds"] = 10

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def verdict(self, expr, result, teacher):
        il = {"qid": "Q-test", "type": "transform", "premises": [expr], "result": result}
        teacher_verdict.apply_verdict(self.st, il, teacher, "Q-test")
        return il

    def test_false_arithmetic_never_enters_transforms(self):
        learning._bump_transforms(self.st, "一加一等于三", "wrong-source")
        self.assertNotIn("一加一→三", self.st["transforms"])
        self.assertEqual(self.st["rejected_transforms"][-1]["reason"], "probe_refuted")

    def test_liang_is_handled_consistently_by_probe_and_learning(self):
        learning._bump_transforms(self.st, "两加三等于五", "source-liang")
        tf = self.st["transforms"]["两加三→五"]
        self.assertEqual(tf["operands"], ["两", "三"])

    def test_same_source_repetition_does_not_add_independent_support(self):
        for _ in range(100):
            learning._bump_transforms(self.st, "一加一等于二", "same-source")
        tf = self.st["transforms"]["一加一→二"]
        self.assertEqual(tf["support"], 1)
        self.assertEqual(tf["evidence_groups"], ["same-source"])
        claim = claims.ensure_claim(self.st, "一加一→二", claims.stable_id("transform", "一加一→二"))
        self.assertEqual(claim["independent_support"], 1)

    def test_teacher_cannot_override_environment_false(self):
        il = self.verdict("一加一", "三", "真")
        self.assertEqual(il["environment_verdict"], "假")
        self.assertNotIn("一加一→三", self.st["transforms"])
        self.assertEqual(self.st["source_reliability"]["teacher"]["addition"]["disagree"], 1)

    def test_teacher_cannot_override_environment_true(self):
        il = self.verdict("一加一", "二", "假")
        self.assertEqual(il["environment_verdict"], "真")
        self.assertIn("一加一→二", self.st["transforms"])
        self.assertEqual(self.st["source_reliability"]["teacher"]["addition"]["disagree"], 1)

    def test_inconclusive_teacher_input_remains_hypothesis(self):
        il = self.verdict("甲加乙", "丙", "存疑")
        self.assertEqual(il["verification_status"], "teacher_only")
        claim = self.st["claims"][claims.stable_id("transform", "甲加乙→丙")]
        self.assertEqual(claim["status"], "hypothesis")
        self.assertNotIn("甲加乙→丙", self.st["transforms"])

    def test_teacher_inference_does_not_directly_change_beliefs(self):
        il = {"qid": "Q-infer", "premises": ["猫", "树"], "result": "天空"}
        teacher_verdict.apply_verdict(self.st, il, "真", "Q-infer")
        self.assertNotIn("天空", self.st["concepts"])
        self.assertEqual(self.st["relations"], {})
        self.assertEqual(il["verification_status"], "teacher_only")

    def test_wrong_candidate_does_not_falsify_general_law(self):
        conceptcard.ensure_cards(self.st)
        conceptcard.on_transform_verdict(self.st, False, "一加一", "三", "Q-wrong")
        self.assertEqual(self.st["laws"]["L001"]["falsifiers"], [])

    def test_law_graduates_only_with_five_independent_examples(self):
        conceptcard.ensure_cards(self.st)
        examples = [("一加一", "二"), ("一加二", "三"), ("一加三", "四"),
                    ("一加四", "五"), ("一加五", "六")]
        for index, (expr, result) in enumerate(examples):
            conceptcard.on_transform_verdict(self.st, True, expr, result, f"Q-{index}")
        self.assertEqual(self.st["laws"]["L001"]["status"], "belief")

    def test_repeated_example_does_not_graduate_law(self):
        conceptcard.ensure_cards(self.st)
        for _ in range(100):
            conceptcard.on_transform_verdict(self.st, True, "一加一", "二", "repeat")
        self.assertEqual(self.st["laws"]["L001"]["status"], "hypothesis")
        self.assertEqual(self.st["laws"]["L001"]["independent_support"], 1)

    def test_consolidation_audit_removes_legacy_false_transform(self):
        self.st["transforms"]["一加一→三"] = {"support": 9, "source": "legacy"}
        removed = learning.audit_transforms(self.st)
        self.assertEqual(removed, 1)
        self.assertNotIn("一加一→三", self.st["transforms"])


if __name__ == "__main__":
    unittest.main()
