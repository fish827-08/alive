# -*- coding: utf-8 -*-
"""Teacherless 阶段 1 回归：独立初始化、种子账本、硬禁用与标准模式不受影响。"""
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selfdrive import (conceptcard, config, decision, kb, llm_bridge, skills,
                       state, teacherless, templates, working_memory)

REPO = Path(__file__).resolve().parent.parent


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(config, "TEACHERLESS", True),
            mock.patch.object(config, "OUTBOX", self.base / "brain" / "outbox"),
            mock.patch.object(config, "INBOX", self.base / "brain" / "inbox.md"),
            mock.patch.object(config, "BRAIN", self.base / "brain"),
            mock.patch.object(state, "STATE_FILE", self.base / "state.json"),
            mock.patch.object(state, "LOGS", self.base / "logs"),
            mock.patch.object(state, "NOTES", self.base / "workspace" / "notes"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def init(self, experiment_id="TL-test", seed_entries=None):
        return teacherless.init_experiment(
            base=self.base, experiment_id=experiment_id, seed_entries=seed_entries)

    def write_state(self, st):
        (self.base / "state.json").write_text(
            json.dumps(st, ensure_ascii=False), encoding="utf-8")

    def write_seed(self, name="seed.txt", text="种子语料"):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


class InitTests(Sandbox):
    def test_clean_directory_initializes_from_zero(self):
        manifest = self.init()
        self.assertEqual(manifest["seed_policy"], "from_zero")
        self.assertTrue(manifest["clean_from_zero"])
        self.assertFalse(manifest["llm_enabled"])
        self.assertTrue((self.base / teacherless.MANIFEST_NAME).exists())
        for rel in ("logs", "brain/templates", "workspace/archive",
                    "workspace/notes", "workspace/web", "semantics"):
            self.assertTrue((self.base / rel).is_dir(), rel)
        self.assertIn("ask_brain", manifest["channels_disabled"])
        self.assertIn("shared_kb", manifest["channels_disabled"])

    def test_refuses_existing_state(self):
        (self.base / "state.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.init()

    def test_refuses_teacher_inbox(self):
        (self.base / "brain").mkdir()
        (self.base / "brain" / "inbox.md").write_text("Q-1 问题\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.init()

    def test_refuses_workspace_notes(self):
        notes = self.base / "workspace" / "notes"
        notes.mkdir(parents=True)
        (notes / "journey.md").write_text("旧笔记", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.init()

    def test_refuses_old_logs(self):
        logs = self.base / "logs"
        logs.mkdir()
        (logs / "events.jsonl").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.init()

    def test_refuses_old_semantics(self):
        sem = self.base / "semantics"
        sem.mkdir()
        (sem / "ae.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.init()

    def test_refuses_double_init(self):
        self.init()
        with self.assertRaises(RuntimeError):
            self.init()

    def test_declared_external_seed_is_not_from_zero(self):
        seed = self.write_seed("corpus.txt", "公开语料")
        manifest = self.init(seed_entries=[{
            "source_type": "training_material", "kind": "docs",
            "path": str(seed), "description": "测试用公开语料",
        }])
        self.assertEqual(manifest["seed_policy"], "declared")
        self.assertFalse(manifest["clean_from_zero"])
        self.assertIn("不是完全从零", manifest["seed_policy_note"])

    def test_teacher_history_seed_is_declared_explicitly(self):
        seed = self.write_seed("old_answers.md", "历史回答")
        manifest = self.init(seed_entries=[{
            "source_type": "teacher_history", "kind": "old_answers",
            "path": str(seed),
            "description": "历史教师回答（明确声明，不算从零）",
        }])
        self.assertEqual(manifest["seed_policy"], "teacher_history_seeded")
        self.assertFalse(manifest["clean_from_zero"])
        self.assertIn("不能宣称完全从零", manifest["seed_policy_note"])

    def test_from_zero_declares_code_priors(self):
        manifest = self.init()
        self.assertIn("代码级先验", manifest["seed_policy_note"])
        builtin = [x for x in manifest["seed_items"]
                   if x.get("kind") == "builtin_priors"]
        self.assertEqual(len(builtin), 1)
        self.assertIn("L001", builtin[0]["description"])

    def test_unknown_seed_source_rejected_before_manifest(self):
        with self.assertRaises(ValueError):
            self.init(seed_entries=[{"source_type": "magic"}])
        self.assertFalse((self.base / teacherless.MANIFEST_NAME).exists())

    def test_agent_name_recorded(self):
        teacherless.init_experiment(
            base=self.base, experiment_id="TL-name", agent_name="小无师")
        self.assertEqual((self.base / "agent_name.txt").read_text(encoding="utf-8"), "小无师")


class SeedLedgerTests(Sandbox):
    def test_external_seed_requires_real_path(self):
        with self.assertRaises(ValueError):
            self.init(seed_entries=[{
                "source_type": "training_material", "kind": "docs",
                "description": "只有描述，没有可核验文件",
            }])
        self.assertFalse((self.base / teacherless.MANIFEST_NAME).exists())

    def test_external_seed_path_must_exist(self):
        with self.assertRaises(ValueError):
            self.init(seed_entries=[{
                "source_type": "training_material",
                "path": str(self.base / "missing.txt"),
            }])

    def test_external_seed_cannot_claim_base_code(self):
        seed = self.write_seed()
        with self.assertRaises(ValueError):
            self.init(seed_entries=[{
                "source_type": "base_code", "path": str(seed),
            }])

    def test_external_seed_records_hash_size_and_time(self):
        seed = self.write_seed("corpus.txt", "abc")
        manifest = self.init(seed_entries=[{
            "source_type": "training_material", "path": str(seed),
        }])
        item = manifest["seed_items"][-1]
        self.assertEqual(item["sha256"],
                         hashlib.sha256(seed.read_bytes()).hexdigest())
        self.assertEqual(item["bytes"], 3)
        self.assertEqual(item["file_count"], 1)
        self.assertTrue(item["imported_at"])
        self.assertFalse(item["imported"])
        self.assertNotIn("stored_at", item)
        self.assertEqual(teacherless.seed_problems(manifest, self.base), [])

    def test_directory_seed_hashes_all_files(self):
        corpus = self.base / "corpus"
        (corpus / "sub").mkdir(parents=True)
        (corpus / "a.txt").write_text("A", encoding="utf-8")
        (corpus / "sub" / "b.txt").write_text("BB", encoding="utf-8")
        manifest = self.init(seed_entries=[{
            "source_type": "training_material", "path": str(corpus),
        }])
        item = manifest["seed_items"][-1]
        self.assertEqual(item["file_count"], 2)
        self.assertEqual(item["bytes"], 3)
        self.assertEqual(teacherless.seed_problems(manifest, self.base), [])

    def test_seed_import_copies_into_archive_and_verifies(self):
        seed = self.write_seed("corpus.txt", "abc")
        manifest = self.init(seed_entries=[{
            "source_type": "training_material", "path": str(seed), "import": True,
        }])
        item = manifest["seed_items"][-1]
        self.assertEqual(item["stored_at"],
                         "workspace/archive/seed-01-corpus.txt")
        stored = self.base / item["stored_at"]
        self.assertEqual(stored.read_text(encoding="utf-8"), "abc")

        # 导入后实验吃的是副本；源文件变化不影响已导入的实验
        seed.write_text("changed", encoding="utf-8")
        self.assertTrue(teacherless.status(self.base)["clean"])

        stored.write_text("tampered", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            teacherless.verify_clean_start(self.base)
        report = teacherless.status(self.base)
        self.assertFalse(report["clean"])
        self.assertTrue(report["seed_problems"])

    def test_seed_change_detected_for_non_imported_seed(self):
        seed = self.write_seed("corpus.txt", "abc")
        manifest = self.init(seed_entries=[{
            "source_type": "training_material", "path": str(seed),
        }])
        seed.write_text("changed", encoding="utf-8")
        report = teacherless.status(self.base)
        self.assertFalse(report["clean"])
        self.assertTrue(any("内容哈希已变化" in x for x in report["seed_problems"]))
        with self.assertRaises(RuntimeError):
            teacherless.verify_clean_start(self.base)

    def test_manifest_seed_items_tamper_rejected(self):
        manifest = self.init()
        manifest["seed_items"][1]["description"] = "被篡改"
        (self.base / teacherless.MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            teacherless.load_manifest(self.base)
        with self.assertRaises(RuntimeError):
            teacherless.status(self.base)

    def test_missing_seed_file_detected(self):
        seed = self.write_seed("corpus.txt", "abc")
        manifest = self.init(seed_entries=[{
            "source_type": "training_material", "path": str(seed),
        }])
        seed.unlink()
        problems = teacherless.seed_problems(manifest, self.base)
        self.assertTrue(any("种子文件缺失" in x for x in problems))


class VerifyTests(Sandbox):
    def test_requires_manifest(self):
        with self.assertRaises(RuntimeError):
            teacherless.verify_clean_start(self.base)

    def test_clean_start_without_state(self):
        manifest = self.init()
        self.assertEqual(teacherless.verify_clean_start(self.base)["experiment_id"],
                         manifest["experiment_id"])

    def test_stamped_state_passes(self):
        manifest = self.init()
        st = state.default_state()
        teacherless.stamp_state(st, manifest)
        self.write_state(st)
        teacherless.verify_clean_start(self.base)

    def test_marker_experiment_mismatch_rejected(self):
        manifest = self.init()
        st = state.default_state()
        teacherless.stamp_state(st, manifest)
        st["teacherless"]["experiment_id"] = "TL-other"
        self.write_state(st)
        with self.assertRaises(RuntimeError):
            teacherless.verify_clean_start(self.base)

    def test_artifact_growth_after_baseline_rejected(self):
        manifest = self.init()
        st = state.default_state()
        teacherless.stamp_state(st, manifest)
        st["brain_questions"].append("Q-1")
        with self.assertRaises(RuntimeError):
            teacherless.assert_state_clean(st, manifest, base=self.base)

    def test_teacher_note_marker_rejected_on_start(self):
        self.init()
        notes = self.base / "workspace" / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / "journey.md").write_text("## 大脑回答 [Q-1]\n内容", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            teacherless.verify_clean_start(self.base)

    def test_blocked_channel_log_rejected_on_start(self):
        self.init()
        logs = self.base / "logs"
        logs.mkdir(exist_ok=True)
        (logs / "events.jsonl").write_text(
            json.dumps({"action": "ask_brain"}) + "\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            teacherless.verify_clean_start(self.base)

    def test_teacher_history_seeded_baseline_is_allowed_but_recorded(self):
        seed = self.write_seed("old_answers.md", "Q-1\nA\n")
        manifest = self.init(
            experiment_id="TL-seeded",
            seed_entries=[{"source_type": "teacher_history", "kind": "old_answers",
                           "path": str(seed)}])
        st = state.default_state()
        st["brain_questions"] = ["Q-1", "Q-2"]
        st["brain_read"] = ["Q-1"]
        marker = teacherless.stamp_state(st, manifest)
        self.assertEqual(marker["baseline_counts"]["teacher_questions"], 2)
        self.assertEqual(marker["baseline_counts"]["teacher_answers_read"], 1)
        self.assertFalse(manifest["clean_from_zero"])

    def test_stamp_refuses_state_from_another_experiment(self):
        manifest_a = self.init(experiment_id="TL-a")
        st = state.default_state()
        teacherless.stamp_state(st, manifest_a)
        with tempfile.TemporaryDirectory() as other:
            manifest_b = teacherless.init_experiment(
                base=other, experiment_id="TL-b")
        with self.assertRaises(RuntimeError):
            teacherless.stamp_state(st, manifest_b)

    def test_standard_mode_refuses_teacherless_state(self):
        manifest = self.init()
        st = state.default_state()
        teacherless.stamp_state(st, manifest)
        with mock.patch.object(config, "TEACHERLESS", False):
            with self.assertRaises(RuntimeError):
                teacherless.assert_mode_matches_state(st)

    def test_status_reports_clean_and_violations(self):
        manifest = self.init()
        st = state.default_state()
        teacherless.stamp_state(st, manifest)
        self.write_state(st)
        clean = teacherless.status(self.base)
        self.assertTrue(clean["clean"])
        self.assertEqual(clean["violations"], {})

        st["brain_questions"].append("Q-1")
        self.write_state(st)
        dirty = teacherless.status(self.base)
        self.assertFalse(dirty["clean"])
        self.assertEqual(dirty["violations"]["teacher_questions"], 1)

    def test_status_reports_code_drift(self):
        manifest = self.init()
        st = state.default_state()
        teacherless.stamp_state(st, manifest)
        self.write_state(st)
        manifest["code_sha256"] = "0" * 64
        (self.base / teacherless.MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        report = teacherless.status(self.base)
        self.assertTrue(report["code_drift"])
        self.assertFalse(report["clean"])

    def test_audit_appends_jsonl(self):
        self.init()
        teacherless.audit(self.base, "probe", "runtime", "测试审计")
        path = self.base / "logs" / teacherless.AUDIT_NAME
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(rows[-1]["event"], "probe")
        self.assertEqual(rows[-1]["detail"], "测试审计")


class CodeVersionTests(Sandbox):
    def force_drift(self):
        manifest = teacherless.load_manifest(self.base)
        manifest["code_sha256"] = "0" * 64
        (self.base / teacherless.MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return manifest

    def test_start_refuses_code_drift(self):
        self.init()
        self.force_drift()
        with self.assertRaises(RuntimeError) as ctx:
            teacherless.verify_clean_start(self.base)
        self.assertIn("代码指纹已漂移", str(ctx.exception))

    def test_assert_state_clean_refuses_midrun_code_drift(self):
        manifest = self.init()
        st = state.default_state()
        teacherless.stamp_state(st, manifest)
        self.write_state(st)
        self.force_drift()
        with self.assertRaises(RuntimeError):
            teacherless.assert_state_clean(st, base=self.base)

    def test_accept_code_records_migration_and_clears_drift(self):
        self.init()
        self.force_drift()
        with mock.patch.object(teacherless, "code_fingerprint",
                               return_value="a" * 64):
            migration = teacherless.accept_code_change("升级研究算法", self.base)
            self.assertEqual(migration["from_sha256"], "0" * 64)
            self.assertEqual(migration["to_sha256"], "a" * 64)
            self.assertEqual(migration["reason"], "升级研究算法")
            loaded = teacherless.load_manifest(self.base)
            self.assertEqual(loaded["code_sha256"], "a" * 64)
            self.assertEqual(loaded["code_migrations"], [migration])
            report = teacherless.status(self.base)
            self.assertFalse(report["code_drift"])
            self.assertTrue(report["clean"])

    def test_accept_code_requires_reason(self):
        self.init()
        self.force_drift()
        with self.assertRaises(ValueError):
            teacherless.accept_code_change("   ", self.base)

    def test_accept_code_requires_actual_drift(self):
        self.init()
        with self.assertRaises(RuntimeError):
            teacherless.accept_code_change("没有变化", self.base)

    def test_code_migration_is_audited(self):
        self.init()
        self.force_drift()
        with mock.patch.object(teacherless, "code_fingerprint",
                               return_value="b" * 64):
            teacherless.accept_code_change("测试迁移", self.base)
        path = self.base / "logs" / teacherless.AUDIT_NAME
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(r["event"] == "code_migration" for r in rows))


class BasePriorTests(unittest.TestCase):
    def test_l001_is_declared_uncalibrated_prior(self):
        st = state.default_state()
        conceptcard.ensure_cards(st)
        law = st["laws"]["L001"]
        self.assertEqual(law["support"], 0)
        self.assertEqual(law["source"], "base_code_prior")
        self.assertEqual(law["confidence"], 0.4)
        self.assertEqual(law["confidence_basis"], "declared_prior_uncalibrated")
        self.assertEqual(law["status"], "hypothesis")

    def test_from_zero_card_has_no_inherited_exemplars(self):
        st = state.default_state()
        conceptcard.ensure_cards(st)
        self.assertEqual(st["concept_cards"]["加法"]["ground"]["exemplars"], [])

    def test_existing_l001_is_not_overwritten(self):
        st = state.default_state()
        st["laws"]["L001"] = {"form": "old", "support": 3,
                              "source": "experiment_evidence"}
        conceptcard.ensure_cards(st)
        self.assertEqual(st["laws"]["L001"]["support"], 3)
        self.assertEqual(st["laws"]["L001"]["source"], "experiment_evidence")


class ChannelTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.st = state.default_state()
        self.patches.extend([
            mock.patch.object(skills, "INBOX", self.base / "brain" / "inbox.md"),
            mock.patch.object(skills, "OUTBOX", self.base / "brain" / "outbox"),
            mock.patch.object(teacherless, "audit", return_value=None),
        ])
        for patch in self.patches[-3:]:
            patch.start()

    def tearDown(self):
        super().tearDown()

    def test_all_blocked_actions_are_denied(self):
        for action in teacherless.BLOCKED_SKILLS:
            self.assertFalse(teacherless.allow_action(action))

    def test_ask_brain_writes_nothing(self):
        ok, detail = skills.do_ask_brain(self.st)
        self.assertFalse(ok)
        self.assertIn("硬禁用", detail)
        self.assertEqual(self.st["brain_questions"], [])
        self.assertFalse((self.base / "brain" / "inbox.md").exists())
        self.assertFalse((self.base / "brain" / "outbox").exists())

    def test_read_brain_reads_no_teacher_answer(self):
        outbox = self.base / "brain" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "Q-1.md").write_text("[VERDICT:真] 教师回答", encoding="utf-8")
        ok, detail = skills.do_read_brain(self.st)
        self.assertFalse(ok)
        self.assertIn("硬禁用", detail)
        self.assertEqual(self.st["brain_read"], [])
        self.assertEqual(self.st["compiled_rules"], {})

    def test_explore_wider_never_scans(self):
        with mock.patch.object(skills, "scan_wider", side_effect=AssertionError("must not scan")):
            ok, _ = skills.do_explore_wider(self.st)
        self.assertFalse(ok)

    def test_llm_consult_never_calls_model(self):
        with mock.patch.object(llm_bridge, "consult", side_effect=AssertionError("must not call")):
            ok, _ = skills.do_llm_consult(self.st)
        self.assertFalse(ok)

    def test_sibling_letter_never_touches_shared_letters(self):
        with mock.patch.object(skills, "_sibling_name", side_effect=AssertionError("must not open")):
            ok, _ = skills.do_sibling_letter(self.st)
        self.assertFalse(ok)

    def test_llm_gates_even_if_env_enabled(self):
        with mock.patch.object(config, "LLM_ENABLED", True), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
            self.assertFalse(llm_bridge.available(self.st))
            self.assertFalse(llm_bridge.think_available(self.st))
            self.assertIsNone(llm_bridge.consult(self.st, "test", "prompt"))
            self.assertEqual(llm_bridge.save_proposals(
                self.st, {"proposals": ["x"]}, "topic"), 0)

    def test_harvest_drops_inherited_think_inbox(self):
        self.st["think_inbox"] = [{
            "focus": "旧焦点", "lines": ["继承来的联想"], "round": 1, "model": "x"}]
        self.assertEqual(llm_bridge.harvest_think(self.st), 0)
        self.assertEqual(self.st["think_inbox"], [])
        self.assertEqual(self.st["llm_proposals"], [])

    def test_shared_kb_disabled(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
            self.assertFalse(kb.kb_remember("内容", ["标签"], "来源"))
            self.assertEqual(kb.kb_recall("查询"), [])

    def test_working_memory_ignores_outbox(self):
        outbox = self.base / "brain" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "Q-1.md").write_text("教师回答", encoding="utf-8")
        wm = working_memory.build(self.st)
        self.assertNotEqual(wm["goal"], "read_teacher_response")
        self.assertEqual(wm["homeostasis"]["unread_responses"], 0)
        self.assertEqual(wm["mode"], "teacherless")
        for action in teacherless.BLOCKED_SKILLS:
            self.assertNotIn(action, wm["available_skills"])

    def test_decision_zeroes_blocked_weights(self):
        weights = decision.legacy_weights(self.st, self.st["emotions"], False)
        for action in teacherless.BLOCKED_SKILLS:
            self.assertEqual(weights[action], 0.0, action)
        self.assertGreater(weights["rest"], 0.0)

    def test_templates_do_not_backfill_from_inbox(self):
        inbox = self.base / "brain" / "inbox.md"
        inbox.parent.mkdir(parents=True)
        inbox.write_text("[Q-1] 老师我有个问题\n", encoding="utf-8")
        with mock.patch.object(templates, "INBOX", inbox):
            templates.ensure_asked_texts(self.st)
        self.assertEqual(self.st["asked_texts"], [])

    def test_conceptcard_does_not_light_answer_card(self):
        outbox = self.base / "brain" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "Q-1.md").write_text("教师回答", encoding="utf-8")
        hits = conceptcard.situation_match(self.st)
        self.assertNotIn("回答", hits)


class StandardModeTests(Sandbox):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(config, "TEACHERLESS", False)
        self.patches.append(patch)
        patch.start()

    def tearDown(self):
        super().tearDown()

    def test_standard_mode_still_reads_outbox(self):
        outbox = self.base / "brain" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "Q-1.md").write_text("教师回答", encoding="utf-8")
        st = state.default_state()
        wm = working_memory.build(st)
        self.assertEqual(wm["goal"], "read_teacher_response")
        self.assertTrue(teacherless.allow_action("read_brain"))
        weights = decision.legacy_weights(st, st["emotions"], False)
        self.assertGreater(weights["read_brain"], 0.0)
        self.assertIn("read_brain", wm["available_skills"])

    def test_standard_mode_still_backfills_inbox(self):
        inbox = self.base / "brain" / "inbox.md"
        inbox.parent.mkdir(parents=True)
        inbox.write_text("[Q-1] 老师我有个问题\n", encoding="utf-8")
        st = state.default_state()
        with mock.patch.object(templates, "INBOX", inbox):
            templates.ensure_asked_texts(st)
        self.assertEqual(st["asked_texts"], ["老师我有个问题"])


class ConfigModeTests(unittest.TestCase):
    def _run(self, extra):
        env = os.environ.copy()
        env.pop("SELFDRIVE_MODE", None)
        env.update(extra)
        code = ("from selfdrive import config; "
                "print(config.TEACHERLESS, config.LLM_ENABLED, "
                "repr(config.SIBLING_NAME), len(config.WIDER_ROOTS))")
        return subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                              env=env, capture_output=True, text=True)

    def test_teacherless_forces_channels_off(self):
        result = self._run({"SELFDRIVE_MODE": "teacherless",
                            "SELFDRIVE_LLM": "1",
                            "SELFDRIVE_SIBLING": "moon"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True False '' 0")

    def test_standard_mode_keeps_existing_flags(self):
        result = self._run({"SELFDRIVE_MODE": "standard",
                            "SELFDRIVE_LLM": "0",
                            "SELFDRIVE_SIBLING": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False False '' 2")

    def test_invalid_mode_rejected(self):
        result = self._run({"SELFDRIVE_MODE": "chaos"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SELFDRIVE_MODE", result.stderr)


if __name__ == "__main__":
    unittest.main()
