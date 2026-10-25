import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selfdrive import config, decision, main, outcomes, state, working_memory


class SandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(state, "STATE_FILE", self.root / "state.json"),
            mock.patch.object(state, "LOGS", self.root / "logs"),
            mock.patch.object(state, "NOTES", self.root / "notes"),
            mock.patch.object(config, "OUTBOX", self.root / "outbox"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()


class StateTests(SandboxTest):
    def test_round_trip_preserves_unknown_fields(self):
        st = state.default_state()
        st["future_extension"] = {"keep": True}
        state.save_state(st)
        loaded = state.load_state()
        self.assertEqual(loaded["future_extension"], {"keep": True})
        self.assertEqual(loaded["state_version"], state.STATE_VERSION)

    def test_corrupt_state_is_not_silently_reset(self):
        state.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state.STATE_FILE.write_text("{broken", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            state.load_state()

    def test_atomic_save_leaves_no_temp_file(self):
        state.save_state(state.default_state())
        self.assertTrue(state.STATE_FILE.exists())
        self.assertEqual(list(self.root.glob("*.tmp")), [])


class WorkingMemoryDecisionTests(SandboxTest):
    def test_working_memory_does_not_modify_long_term_memory(self):
        st = state.default_state()
        st["recent_active"] = [{"c": "加法", "round": 1}]
        before = copy.deepcopy({k: v for k, v in st.items() if k != "working_memory"})
        wm = working_memory.build(st)
        after = {k: v for k, v in st.items() if k != "working_memory"}
        self.assertEqual(after, before)
        self.assertEqual(wm["active_concepts"], ["加法"])

    def test_low_energy_prefers_rest_in_candidate_utility(self):
        st = state.default_state()
        st["energy"] = 0.1
        candidates = decision.evaluate_candidates(st, {"rest": 1.0, "explore_file": 1.0})
        by_action = {c["action"]: c for c in candidates}
        self.assertGreater(by_action["rest"]["utility"], by_action["explore_file"]["utility"])

    def test_zero_weight_candidates_cannot_be_selected(self):
        st = state.default_state()
        candidates = decision.evaluate_candidates(
            st, {"rest": 1.0, "llm_consult": 0.0, "web_search": 0.0})
        self.assertEqual([c["action"] for c in candidates], ["rest"])

    def test_working_memory_surfaces_claims_about_active_concepts(self):
        st = state.default_state()
        st["recent_active"] = [{"c": "加法", "round": 1}]
        st["claims"]["C1"] = {"id": "C1", "expression": "加法需要独立证据",
                              "last_tested": 1, "status": "hypothesis"}
        wm = working_memory.build(st)
        self.assertEqual(wm["active_claims"], ["C1"])

    def test_unread_response_raises_read_weight(self):
        config.OUTBOX.mkdir(parents=True)
        (config.OUTBOX / "Q-1.md").write_text("回答", encoding="utf-8")
        st = state.default_state()
        with_unread = decision.legacy_weights(st, st["emotions"], False)["read_brain"]
        st["brain_read"].append("Q-1")
        without_unread = decision.legacy_weights(st, st["emotions"], False)["read_brain"]
        self.assertGreater(with_unread, without_unread)

    def test_all_zero_weights_fall_back_to_rest(self):
        st = state.default_state()
        st["skill_multipliers"] = {name: 0.0 for name in config.SKILLS}
        weights = decision.legacy_weights(st, st["emotions"], False)
        self.assertEqual(weights["rest"], 1.0)


class OutcomeTests(SandboxTest):
    def test_pending_precedes_actual_and_brier_is_recorded(self):
        st = state.default_state()
        candidates = [{"action": "rest", "success_probability": 0.8,
                       "expected_energy_delta": 0.02}]
        outcomes.begin(st, "rest", candidates)
        rows = [json.loads(line) for line in (state.LOGS / "action_outcomes.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["status"], "pending")
        st["energy"] += 0.02
        outcome = outcomes.settle(st, True, "休息完成")
        self.assertIsNone(st.get("current_action"))
        self.assertEqual(outcome["status"], "hit")
        self.assertAlmostEqual(outcome["brier"], 0.04)
        self.assertEqual(outcome["actual"]["state_changes"]["energy_delta"], 0.02)

    def test_interrupted_action_is_recovered_as_inconclusive(self):
        st = state.default_state()
        outcomes.begin(st, "rest", [{"action": "rest"}])
        outcomes.begin(st, "rest", [{"action": "rest"}])
        rows = [json.loads(line) for line in (state.LOGS / "action_outcomes.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[1]["status"], "inconclusive")
        self.assertIsNone(rows[1]["brier"])
        self.assertEqual(len(st["action_outcomes"]), 0)

    def test_inconclusive_outcome_is_excluded_from_brier(self):
        st = state.default_state()
        outcomes.begin(st, "rest", [{"action": "rest", "success_probability": 0.8}])
        outcomes.settle(st, False, "异常中断", forced_status="inconclusive")
        self.assertIsNone(outcomes.metrics(st)["brier"])
        self.assertEqual(st["outcome_stats"], {})

    def test_main_executes_handler_once_and_settles_after_energy(self):
        st = state.default_state()
        calls = []
        candidate = {"action": "rest", "success_probability": 0.5,
                     "expected_energy_delta": -0.008}

        def handler(current):
            calls.append(current["current_action"]["status"])
            self.assertEqual(current["current_action"]["action"], "rest")
            return True, "测试休息"

        saves = []

        def record_save(current):
            saves.append(current.get("current_action", {}).get("status", "final"))

        with mock.patch.object(main.skills, "scan_workspace", return_value=([], [])), \
             mock.patch.object(main.skills, "novelty_score", return_value=0.0), \
             mock.patch.object(main, "choose_action", return_value=("rest", [candidate])), \
             mock.patch.object(main.skills, "do_rest", side_effect=handler), \
             mock.patch.object(main, "log_event"), \
             mock.patch.object(main, "save_state", side_effect=record_save), \
             mock.patch.object(main.SEM, "save"), \
             mock.patch("builtins.print"):
            main.run_round(st)
        self.assertEqual(calls, ["pending"])
        self.assertEqual(saves, ["pending", "final"])
        self.assertEqual(len(st["action_outcomes"]), 1)
        self.assertEqual(st["action_outcomes"][0]["actual"]["observation"], "action_success")


if __name__ == "__main__":
    unittest.main()
