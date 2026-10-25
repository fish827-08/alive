import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selfdrive import config, llm_bridge, predictions, rules_compiler, skills, state


VALID = {
    "proposals": ["候选解释"],
    "assumptions": ["待核假设"],
    "unknowns": ["未知项"],
    "suggested_checks": ["核对建议"],
}


class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps({"message": {"content": self.value}}).encode("utf-8")


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(state, "LOGS", self.root / "logs"),
            mock.patch.object(state, "NOTES", self.root / "notes"),
            mock.patch.object(skills, "NOTES", self.root / "notes"),
            mock.patch.object(config, "LLM_ENABLED", True),
            mock.patch.object(config, "LLM_COOLDOWN", 0),
            mock.patch.object(config, "LLM_DAILY_MAX", 40),
            mock.patch.object(config, "LLM_FAILURE_SILENCE", 50),
        ]
        for patch in self.patches:
            patch.start()
        self.st = state.default_state()
        self.st["rounds"] = 100

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()


class LLMTests(SandboxTest):
    def consult(self, value):
        with mock.patch.object(llm_bridge.urllib.request, "urlopen", return_value=Response(value)):
            return llm_bridge.consult(self.st, "test", "prompt")

    def test_strict_schema_accepts_four_arrays(self):
        result = self.consult(json.dumps(VALID, ensure_ascii=False))
        self.assertEqual(result["proposals"], ["候选解释"])
        self.assertEqual(self.st["llm_budget"]["calls"], 1)
        self.assertEqual(self.st["llm_budget"]["fails"], 0)

    def test_extra_permission_field_is_rejected(self):
        value = dict(VALID, execute=True)
        self.assertIsNone(self.consult(json.dumps(value, ensure_ascii=False)))
        self.assertEqual(self.st["llm_budget"]["calls"], 1)
        self.assertEqual(self.st["llm_budget"]["fails"], 1)

    def test_prose_is_rejected(self):
        self.assertIsNone(self.consult("这是普通散文，不是 JSON"))
        self.assertEqual(self.st["llm_budget"]["fails"], 1)

    def test_timeout_counts_budget_and_failure(self):
        with mock.patch.object(llm_bridge.urllib.request, "urlopen", side_effect=TimeoutError):
            self.assertIsNone(llm_bridge.consult(self.st, "test", "prompt"))
        self.assertEqual(self.st["llm_budget"]["calls"], 1)
        self.assertEqual(self.st["llm_budget"]["fails"], 1)

    def test_disabled_does_not_call_network(self):
        with mock.patch.object(config, "LLM_ENABLED", False), \
             mock.patch.object(llm_bridge.urllib.request, "urlopen") as network:
            self.assertIsNone(llm_bridge.consult(self.st, "test", "prompt"))
        network.assert_not_called()

    def test_daily_budget_blocks_attempts(self):
        self.st["llm_budget"] = {"day": __import__("time").strftime("%Y%m%d"),
                                 "calls": config.LLM_DAILY_MAX, "fails": 0}
        self.assertFalse(llm_bridge.available(self.st))

    def test_failure_silence_and_success_reset(self):
        with mock.patch.object(llm_bridge.urllib.request, "urlopen", side_effect=TimeoutError):
            for _ in range(3):
                llm_bridge.consult(self.st, "test", "prompt")
        self.assertFalse(llm_bridge.available(self.st))
        self.st["rounds"] += config.LLM_FAILURE_SILENCE
        self.assertTrue(llm_bridge.available(self.st))
        result = self.consult(json.dumps(VALID, ensure_ascii=False))
        self.assertIsNotNone(result)
        self.assertEqual(self.st["llm_budget"]["fails"], 0)

    def test_consult_saves_only_unverified_proposals(self):
        result = dict(VALID)
        result.update({"model": "test", "task": "test", "raw": json.dumps(VALID),
                       "prompt_version": "v1", "input_sha256": "abc"})
        with mock.patch.object(llm_bridge, "consult", return_value=result), \
             mock.patch.object(skills, "learn_from_text") as learning:
            ok, _ = skills.do_llm_consult(self.st)
        self.assertTrue(ok)
        learning.assert_not_called()
        self.assertEqual(self.st["concepts"], {})
        self.assertTrue(all(p["status"] == "unverified" for p in self.st["llm_proposals"]))


class RegressionTests(SandboxTest):
    def test_dream_prediction_uses_full_dream_horizon(self):
        self.st["edge_preds"] = [{"a": "甲", "b": "乙", "round": 1,
                                  "deadline": 121, "status": "pending", "origin": "dream"}]
        self.st["rounds"] = 61
        self.assertEqual(predictions.expire_edge_predictions(self.st), 0)
        self.st["rounds"] = 122
        self.assertEqual(predictions.expire_edge_predictions(self.st), 1)

    def test_normal_prediction_still_uses_normal_horizon(self):
        self.st["edge_preds"] = [{"a": "甲", "b": "乙", "round": 1,
                                  "deadline": 121, "status": "pending"}]
        self.st["rounds"] = 62
        self.assertEqual(predictions.expire_edge_predictions(self.st), 1)

    def test_undo_rebuilds_multiplier_from_remaining_rules(self):
        rules_compiler.parse_and_compile(self.st, "[RULE] 任何 -> 权重:rest×0.5 [/RULE]", "test")
        rules_compiler.parse_and_compile(self.st, "[RULE] 任何 -> 权重:rest×0.75 [/RULE]", "test")
        self.assertEqual(self.st["skill_multipliers"]["rest"], 0.375)
        rules_compiler.parse_and_compile(self.st, "[RULE] 任何 -> 撤销:r001 [/RULE]", "test")
        self.assertEqual(self.st["skill_multipliers"]["rest"], 0.75)

    def test_third_student_does_not_impersonate_moon(self):
        with mock.patch.object(config, "SIBLING_NAME", "peer"), \
             mock.patch.object(config, "SHARED_LETTERS", self.root / "letters"):
            me, sibling, outbox, inbox = skills._sibling_name(self.st, "alive_star")
        self.assertEqual(me, "alive_star")
        self.assertEqual(sibling, "peer")
        self.assertEqual(outbox.name, "from_alive_star")
        self.assertEqual(inbox.name, "from_peer")

    def test_unconfigured_third_student_has_no_default_sibling(self):
        with mock.patch.object(config, "SIBLING_NAME", ""), \
             mock.patch.object(config, "SHARED_LETTERS", self.root / "letters"):
            _, sibling, _, inbox = skills._sibling_name(self.st, "alive_star")
        self.assertEqual(sibling, "")
        self.assertIsNone(inbox)

    def test_letters_beyond_batch_remain_unread(self):
        letters = self.root / "letters"
        inbox = letters / "from_peer"
        inbox.mkdir(parents=True)
        for index in range(5):
            (inbox / f"L-{index}.md").write_text(f"第{index}封来信，包含足够长的普通阅读文本内容。", encoding="utf-8")
        with mock.patch.object(config, "SIBLING_NAME", "peer"), \
             mock.patch.object(config, "SHARED_LETTERS", letters):
            skills.do_sibling_letter(self.st, "alive_star")
            self.assertEqual(len(self.st["letters_read"]), 3)
            skills.do_sibling_letter(self.st, "alive_star")
        self.assertEqual(len(self.st["letters_read"]), 5)


if __name__ == "__main__":
    unittest.main()
