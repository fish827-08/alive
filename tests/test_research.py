# -*- coding: utf-8 -*-
"""阶段 2 回归：研究任务来源、定向查询、重复抑制、失败改写与来源追溯。"""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from selfdrive import (config, decision, research, skills, state, teacherless,
                       working_memory)

FAKE_HTML = """
<html><body>
<li class="b_algo"><h2><a href="https://example.org/a">加法与能量的关系</a></h2>
<p>摘要原文一：加法与能量在同一条资料里出现。</p></li>
<li class="b_algo"><h2><a href="https://example.org/b">能量的计量</a></h2>
<p>摘要原文二：能量可以计量。</p></li>
</body></html>
"""


class ResearchSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.brain = self.root / "brain"
        self.notes = self.root / "workspace" / "notes"
        self.web = self.root / "workspace" / "web"
        self.logs = self.root / "logs"
        for path in (self.brain, self.notes, self.web, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        self.patches = [
            mock.patch.object(config, "BRAIN", self.brain),
            mock.patch.object(config, "WEB", self.web),
            mock.patch.object(config, "OUTBOX", self.brain / "outbox"),
            mock.patch.object(config, "LLM_ENABLED", False),
            mock.patch.object(state, "STATE_FILE", self.root / "state.json"),
            mock.patch.object(state, "LOGS", self.logs),
            mock.patch.object(state, "NOTES", self.notes),
            mock.patch.object(skills, "WEB", self.web),
            mock.patch.object(skills, "NOTES", self.notes),
            mock.patch.object(skills, "kb_remember", lambda *a, **k: True),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def fresh(self):
        st = state.default_state()
        st["rounds"] = 100
        st["last_web_search_round"] = -999
        return st

    def add_miss(self, st, a="加法", b="能量"):
        st.setdefault("edge_preds", []).append({
            "a": a, "b": b, "round": 10, "deadline": 70,
            "status": "miss", "settled_round": 75,
        })

    def tasks_of(self, st, kind=None):
        tasks = list(research.ensure_state(st)["tasks"].values())
        return [t for t in tasks if kind is None or t["kind"] == kind]

    def source_rows(self):
        path = self.logs / "research_sources.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TaskSourceTests(ResearchSandbox):
    def test_prediction_miss_creates_specific_task_without_duplicates(self):
        st = self.fresh()
        self.add_miss(st)
        research.ensure_tasks(st)
        tasks = self.tasks_of(st)
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertIn("中间概念", task["question"])
        self.assertIn("预测落空", task["reason"])
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["verification"], "unverified")
        self.assertGreaterEqual(task["priority"], 0.8)
        research.ensure_tasks(st)
        self.assertEqual(len(self.tasks_of(st)), 1)

    def test_claim_evidence_gap_but_supported_claim_is_skipped(self):
        st = self.fresh()
        st["claims"] = {
            "C-gap": {"id": "C-gap", "expression": "水在标准气压下沸腾",
                      "status": "hypothesis", "independent_support": 0,
                      "independent_counter": 0},
            "C-ok": {"id": "C-ok", "expression": "一加一等于二",
                     "status": "believed", "independent_support": 1,
                     "independent_counter": 0},
        }
        research.ensure_tasks(st)
        gaps = self.tasks_of(st, "claim_evidence_gap")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["target"]["claim_id"], "C-gap")
        self.assertIn("独立支持 0", gaps[0]["reason"])

    def test_weak_relation_creates_task_and_strong_relation_does_not(self):
        st = self.fresh()
        st["concepts"] = {"加法": {"n": 3, "last": 1},
                          "能量": {"n": 3, "last": 1},
                          "减法": {"n": 3, "last": 1}}
        st["relations"] = {
            "加法|能量": {"w": 1, "n": 1, "last_touch": 1},
            "加法|减法": {"w": 5, "n": 3, "last_touch": 1},
        }
        research.ensure_tasks(st)
        rel_tasks = self.tasks_of(st, "relation_unverified")
        self.assertEqual(len(rel_tasks), 1)
        self.assertEqual(rel_tasks[0]["target"]["relation"], "加法|能量")
        self.assertIn("弱关系未验证", rel_tasks[0]["reason"])

    def test_isolated_concept_creates_task(self):
        st = self.fresh()
        st["concepts"] = {"涌现": {"n": 4, "last": 1},
                          "加法": {"n": 3, "last": 1},
                          "能量": {"n": 3, "last": 1}}
        st["relations"] = {"加法|能量": {"w": 1, "n": 1, "last_touch": 1}}
        research.ensure_tasks(st)
        isolated = self.tasks_of(st, "isolated_concept")
        self.assertEqual([t["target"]["concept"] for t in isolated], ["涌现"])

    def test_task_creation_is_capped_per_round(self):
        st = self.fresh()
        st["claims"] = {
            f"C{i}": {"id": f"C{i}", "expression": f"待验证命题{i}",
                      "status": "hypothesis", "independent_support": 0,
                      "independent_counter": 0}
            for i in range(10)
        }
        with mock.patch.object(config, "RESEARCH_NEW_PER_ROUND", 2):
            research.ensure_tasks(st)
        self.assertEqual(len(self.tasks_of(st, "claim_evidence_gap")), 2)

    def test_recent_active_concept_raises_claim_priority(self):
        st = self.fresh()
        st["recent_active"] = [{"c": "加法", "round": 99}]
        st["claims"] = {
            "C-add": {"id": "C-add", "expression": "加法满足交换律",
                      "status": "hypothesis", "independent_support": 0,
                      "independent_counter": 0},
            "C-sub": {"id": "C-sub", "expression": "减法需要借位",
                      "status": "hypothesis", "independent_support": 0,
                      "independent_counter": 0},
        }
        research.ensure_tasks(st)
        tasks = {t["target"]["claim_id"]: t for t in self.tasks_of(st)}
        self.assertGreater(tasks["C-add"]["priority"], tasks["C-sub"]["priority"])


class TaskCompetitionAndLimitTests(ResearchSandbox):
    def test_prediction_miss_upgrades_weak_relation_task(self):
        st = self.fresh()
        st["relations"] = {"光|影": {"w": 1, "n": 1, "last_touch": 1}}
        research.ensure_tasks(st)
        task = self.tasks_of(st)[0]
        task_id = task["id"]
        self.assertEqual(task["kind"], "relation_unverified")
        self.add_miss(st, "光", "影")
        research.ensure_tasks(st)
        self.assertEqual(len(self.tasks_of(st)), 1)
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertIn("预测落空", task["reason"])
        self.assertEqual(task["status"], "open")
        upgrade = task["kind_history"][-1]
        self.assertEqual(upgrade["from"], "relation_unverified")
        self.assertEqual(upgrade["to"], "prediction_miss")
        query = research.next_query(st)
        self.assertEqual(query["task_id"], task_id)
        self.assertEqual(query["purpose"], "bridge")

    def test_prediction_miss_reopens_exhausted_relation_task_with_history(self):
        st = self.fresh()
        st["relations"] = {"光|影": {"w": 1, "n": 1, "last_touch": 1}}
        research.ensure_tasks(st)
        task = self.tasks_of(st)[0]
        for _ in range(config.RESEARCH_ATTEMPTS_PER_TASK):
            query = research.next_query(st)
            research.record_execution(st, query, False, failure_kind="network_error")
        self.assertEqual(task["status"], "exhausted")
        self.add_miss(st, "光", "影")
        research.ensure_tasks(st)
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["attempts"], [])
        self.assertEqual(len(task["attempt_history"]), 1)
        self.assertEqual(len(task["attempt_history"][0]["attempts"]),
                         config.RESEARCH_ATTEMPTS_PER_TASK)
        self.assertEqual(research.next_query(st)["purpose"], "bridge")

    def test_prediction_miss_reopens_materials_found_relation_task(self):
        st = self.fresh()
        st["relations"] = {"光|影": {"w": 1, "n": 1, "last_touch": 1}}
        research.ensure_tasks(st)
        task = self.tasks_of(st)[0]
        query = research.next_query(st)
        research.record_execution(st, query, True, results=[
            {"title": "光影", "snippet": "摘要", "url": "https://example.org/l"}])
        for _ in range(config.RESEARCH_ATTEMPTS_PER_TASK - 1):
            query = research.next_query(st)
            research.record_execution(st, query, False, failure_kind="network_error")
        self.assertEqual(task["status"], "materials_found")
        self.assertEqual(task["verification"], "pending_verification")
        self.add_miss(st, "光", "影")
        research.ensure_tasks(st)
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(task["status"], "open")
        self.assertEqual(task["verification"], "pending_verification")
        self.assertGreater(task["result_count"], 0)
        self.assertEqual(task["attempts"], [])

    def test_exploration_fallback_respects_total_task_cap(self):
        st = self.fresh()
        r = research.ensure_state(st)
        for i in range(config.RESEARCH_TASK_KEEP):
            r["tasks"][f"RT:old{i}"] = {
                "id": f"RT:old{i}", "kind": "exploration",
                "target": {"type": "exploration", "topic": f"旧{i}", "bucket": 0},
                "status": "exhausted", "priority": 0.3,
                "created_round": 1, "updated_round": 1,
                "query_ids": [], "attempts": [],
            }
        self.assertIsNone(research.next_query(st, fallback_topic="新主题"))
        self.assertEqual(len(r["tasks"]), config.RESEARCH_TASK_KEEP)
        self.assertIn('"reason": "total_cap"',
                      (self.logs / "research.jsonl").read_text(encoding="utf-8"))

    def test_exploration_fallback_respects_round_cap(self):
        st = self.fresh()
        r = research.ensure_state(st)
        for i in range(config.RESEARCH_NEW_PER_ROUND):
            r["tasks"][f"RT:new{i}"] = {
                "id": f"RT:new{i}", "kind": "exploration",
                "target": {"type": "exploration", "topic": f"新{i}", "bucket": 0},
                "status": "exhausted", "priority": 0.3,
                "created_round": st["rounds"], "updated_round": st["rounds"],
                "query_ids": [], "attempts": [],
            }
        self.assertIsNone(research.next_query(st, fallback_topic="另一个主题"))
        self.assertEqual(len(r["tasks"]), config.RESEARCH_NEW_PER_ROUND)
        self.assertIn('"reason": "round_cap"',
                      (self.logs / "research.jsonl").read_text(encoding="utf-8"))

    def test_exploration_fallback_respects_open_task_cap(self):
        st = self.fresh()
        r = research.ensure_state(st)
        for i in range(config.RESEARCH_MAX_TASKS):
            r["tasks"][f"RT:open{i}"] = {
                "id": f"RT:open{i}", "kind": "exploration",
                "target": {"type": "exploration", "topic": f"旧{i}", "bucket": 0},
                "status": "open", "priority": 0.3,
                "created_round": 1, "updated_round": 1,
                "query_ids": [], "attempts": [],
            }
        self.assertIsNone(research._ensure_exploration_task(st, "新主题"))
        self.assertEqual(len(r["tasks"]), config.RESEARCH_MAX_TASKS)
        self.assertIn('"reason": "open_cap"',
                      (self.logs / "research.jsonl").read_text(encoding="utf-8"))

    def test_round_cap_does_not_skip_later_relation_upgrade(self):
        st = self.fresh()
        st["rounds"] = 99
        st["relations"] = {"光|影": {"w": 1, "n": 1, "last_touch": 1}}
        research.ensure_tasks(st)
        task = self.tasks_of(st)[0]
        task_id = task["id"]
        self.assertEqual(task["kind"], "relation_unverified")

        st["rounds"] = 100
        st["recent_active"] = [{"c": "加法", "round": 99}]
        st["last_kw"] = "加法"
        st["claims"] = {
            f"C{i}": {"id": f"C{i}", "expression": "加法满足交换律",
                      "status": "hypothesis", "independent_support": 0,
                      "independent_counter": 0}
            for i in range(4)
        }
        self.add_miss(st, "光", "影")
        ordered = sorted(research._task_candidates(st),
                         key=lambda c: (-c["priority"], c["kind"]))
        # 前 3 条高优命题占满本轮新建额度，第 4 条同样会触发 round_cap
        self.assertEqual([c["kind"] for c in ordered[:4]],
                         ["claim_evidence_gap"] * 4)
        research.ensure_tasks(st)
        self.assertEqual(task["id"], task_id)
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(task["status"], "open")
        self.assertEqual(len(self.tasks_of(st, "claim_evidence_gap")), 3)
        self.assertEqual(len(self.tasks_of(st)), 4)


class LegacyStateMigrationTests(ResearchSandbox):
    def legacy_state(self):
        st = self.fresh()
        r = st["research"]
        r.pop("schema_version", None)
        old_key = "relation_unverified|relation|影|光"
        old_id = research._stable_id("RT", old_key)
        query_id = "RQ:legacy"
        source_id = "RS:legacy"
        task = {
            "id": old_id, "kind": "relation_unverified",
            "kind_label": "关系未验证",
            "question": "《影》和《光》是什么关系？",
            "reason": "弱关系未验证",
            "target": {"type": "relation", "relation": "影|光",
                       "concepts": ["影", "光"], "w": 1, "n": 1},
            "priority": 0.6, "status": "materials_found",
            "verification": "pending_verification",
            "created_round": 90, "updated_round": 95, "last_seen_round": 95,
            "attempts": [{"round": 94, "query_id": query_id,
                          "success": True, "result_count": 1}],
            "query_ids": [query_id],
            "source_ids": [source_id], "result_count": 1, "rewrites": 0,
        }
        r["tasks"][old_id] = task
        r["queries"][query_id] = {
            "id": query_id, "task_id": old_id, "purpose": "relation",
            "text": "光和影有什么关系", "normalized": "光和影有什么关系",
            "status": "success", "sources": [source_id],
        }
        r["sources"].append({
            "id": source_id, "task_id": old_id, "query_id": query_id,
            "url": "https://example.org/legacy", "title": "光影",
            "content_sha256": "abc123", "serp_sha256": "def456",
            "verification": "unverified",
        })
        new_id = research._stable_id(
            "RT", research._task_key(
                "relation_unverified",
                {"type": "relation", "relation": "光|影"}))
        return st, r, old_id, new_id, query_id, source_id

    def test_legacy_relation_ids_and_source_fields_are_migrated(self):
        st, _r, old_id, new_id, query_id, source_id = self.legacy_state()
        state.save_state(st)
        st = state.load_state()
        r = st["research"]
        self.assertNotIn("schema_version", r)
        research.ensure_state(st)
        self.assertNotIn(old_id, r["tasks"])
        task = r["tasks"][new_id]
        self.assertEqual(task["id"], new_id)
        self.assertEqual(task["target"]["relation"], "影|光")
        self.assertEqual(r["queries"][query_id]["task_id"], new_id)
        self.assertEqual(r["sources"][0]["task_id"], new_id)
        self.assertEqual(r["sources"][0]["title_excerpt_sha256"], "abc123")
        self.assertNotIn("content_sha256", r["sources"][0])
        self.assertEqual(r["schema_version"], research.RESEARCH_SCHEMA_VERSION)

        research.ensure_state(st)
        self.assertEqual(len(r["tasks"]), 1)
        self.assertEqual(len(r["sources"]), 1)

        self.add_miss(st, "光", "影")
        research.ensure_tasks(st)
        self.assertEqual(len(r["tasks"]), 1)
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertIn("预测落空", task["reason"])
        self.assertEqual(task["query_ids"], [query_id])
        self.assertEqual(task["source_ids"], [source_id])

    def test_legacy_and_new_relation_tasks_merge_without_losing_links(self):
        st, r, old_id, new_id, query_id, source_id = self.legacy_state()
        new_task = {
            "id": new_id, "kind": "prediction_miss",
            "kind_label": "预测落空",
            "question": "《光》和《影》之间缺少什么中间概念？",
            "reason": "预测落空",
            "target": {"type": "relation", "relation": "光|影",
                       "concepts": ["光", "影"]},
            "priority": 0.8, "status": "open", "verification": "unverified",
            "created_round": 97, "updated_round": 97, "last_seen_round": 97,
            "attempts": [{"round": 97, "query_id": "RQ:new", "success": False}],
            "query_ids": ["RQ:new"], "source_ids": [], "result_count": 0,
            "rewrites": 0,
        }
        r["tasks"][new_id] = new_task
        r["queries"]["RQ:new"] = {
            "id": "RQ:new", "task_id": new_id, "purpose": "bridge",
            "text": "光和影之间有什么联系", "normalized": "光和影之间有什么联系",
            "status": "failed", "sources": [],
        }
        research.ensure_state(st)
        self.assertEqual(len(r["tasks"]), 1)
        task = r["tasks"][new_id]
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertIn(query_id, task["query_ids"])
        self.assertIn("RQ:new", task["query_ids"])
        self.assertEqual(task["source_ids"], [source_id])
        self.assertEqual(task["attempt_history"][-1]["kind"],
                         "relation_unverified")
        self.assertEqual(task["attempt_history"][-1]["attempts"],
                         [{"round": 94, "query_id": query_id,
                           "success": True, "result_count": 1}])
        self.assertEqual(r["queries"][query_id]["task_id"], new_id)
        self.assertEqual(r["sources"][0]["task_id"], new_id)

    def collision_state(self):
        st = self.fresh()
        r = st["research"]
        r.pop("schema_version", None)
        relation = "影|光"
        miss_id = research._stable_id(
            "RT", f"prediction_miss|relation|{relation}")
        weak_id = research._stable_id(
            "RT", f"relation_unverified|relation|{relation}")
        new_id = research._stable_id(
            "RT", research._task_key(
                "prediction_miss", {"type": "relation", "relation": "光|影"}))
        miss = {
            "id": miss_id, "kind": "prediction_miss", "kind_label": "预测落空",
            "question": "《光》和《影》之间缺少什么中间概念？",
            "reason": "预测落空",
            "target": {"type": "relation", "relation": relation,
                       "concepts": ["光", "影"]},
            "priority": 0.8, "status": "exhausted", "verification": "unverified",
            "created_round": 80, "updated_round": 90, "last_seen_round": 90,
            "attempts": [{"round": 81, "query_id": "RQ:miss1",
                          "outcome": "failed", "result_count": 0}],
            "query_ids": ["RQ:miss1"], "source_ids": [],
            "result_count": 0, "rewrites": 0,
        }
        weak = {
            "id": weak_id, "kind": "relation_unverified",
            "kind_label": "关系未验证",
            "question": "《光》和《影》是什么关系？",
            "reason": "弱关系未验证",
            "target": {"type": "relation", "relation": relation,
                       "concepts": ["光", "影"]},
            "priority": 0.6, "status": "active",
            "verification": "unverified",
            "created_round": 85, "updated_round": 95, "last_seen_round": 95,
            "attempts": [{"round": 94, "query_id": "RQ:weak",
                          "outcome": "used", "result_count": 1}],
            "query_ids": ["RQ:weak"], "source_ids": ["RS:weak"],
            "result_count": 1, "rewrites": 0,
        }
        r["tasks"][miss_id] = miss
        r["tasks"][weak_id] = weak
        r["queries"]["RQ:weak"] = {
            "id": "RQ:weak", "task_id": weak_id, "purpose": "relation",
            "text": "光和影有什么关系", "normalized": "光和影有什么关系",
            "status": "used", "sources": ["RS:weak"],
        }
        return st, r, miss_id, weak_id, new_id

    def test_merge_keeps_active_task_executable(self):
        st, r, miss_id, weak_id, new_id = self.collision_state()
        research.ensure_state(st)
        self.assertEqual(len(r["tasks"]), 1)
        task = r["tasks"][new_id]
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["attempts"],
                         [{"round": 94, "query_id": "RQ:weak",
                           "outcome": "used", "result_count": 1}])
        self.assertEqual(task["attempt_history"][-1]["kind"],
                         "prediction_miss")
        self.assertEqual(task["attempt_history"][-1]["attempts"],
                         [{"round": 81, "query_id": "RQ:miss1",
                           "outcome": "failed", "result_count": 0}])
        self.assertIn("RQ:weak", task["query_ids"])
        self.assertIn("RS:weak", task["source_ids"])
        self.assertIn(task, research._viable_tasks(st))
        query = research.next_query(st)
        self.assertIsNotNone(query)
        self.assertEqual(query["task_id"], new_id)

    def test_merge_keeps_pending_materials_discoverable(self):
        st, r, miss_id, weak_id, new_id = self.collision_state()
        weak = r["tasks"][weak_id]
        weak["status"] = "materials_found"
        weak["verification"] = "pending_verification"
        weak["result_count"] = 2
        weak["source_ids"] = ["RS:weak", "RS:weak2"]
        research.ensure_state(st)
        self.assertEqual(len(r["tasks"]), 1)
        task = r["tasks"][new_id]
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(task["status"], "materials_found")
        self.assertEqual(task["verification"], "pending_verification")
        self.assertEqual(task["result_count"], 2)
        self.assertEqual(task["source_ids"], ["RS:weak", "RS:weak2"])
        self.assertEqual(task["attempt_history"][-1]["kind"],
                         "relation_unverified")
        self.assertNotIn(task, research._viable_tasks(st))
        focused = {t["id"]: t for t in research.focus(st)}
        self.assertEqual(focused[new_id]["status"], "materials_found")
        self.assertEqual(focused[new_id]["verification"],
                         "pending_verification")

    def test_merge_preserves_secondary_attempt_history(self):
        st, r, miss_id, weak_id, new_id = self.collision_state()
        miss_old = {
            "kind": "relation_unverified", "round": 70,
            "attempts": [{"round": 69, "query_id": "RQ:miss-old",
                          "outcome": "failed", "result_count": 0}],
            "rewrites": 1, "result_count": 0,
        }
        weak_old = {
            "kind": "relation_unverified", "round": 92,
            "attempts": [{"round": 91, "query_id": "RQ:weak-old",
                          "outcome": "failed", "result_count": 0}],
            "rewrites": 0, "result_count": 0,
        }
        r["tasks"][miss_id]["attempt_history"] = [miss_old]
        r["tasks"][weak_id]["attempt_history"] = [weak_old]
        research.ensure_state(st)
        task = r["tasks"][new_id]
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["attempts"],
                         [{"round": 94, "query_id": "RQ:weak",
                           "outcome": "used", "result_count": 1}])
        self.assertEqual([x["round"] for x in task["attempt_history"]],
                         [70, 90, 92])
        self.assertEqual(task["attempt_history"][0]["attempts"],
                         [{"round": 69, "query_id": "RQ:miss-old",
                           "outcome": "failed", "result_count": 0}])
        self.assertEqual(task["attempt_history"][1]["kind"],
                         "prediction_miss")
        self.assertEqual(task["attempt_history"][2]["attempts"],
                         [{"round": 91, "query_id": "RQ:weak-old",
                           "outcome": "failed", "result_count": 0}])
        self.assertIn("RQ:weak", task["query_ids"])
        self.assertIn("RQ:miss1", task["query_ids"])
        self.assertEqual(task["source_ids"], ["RS:weak"])
        self.assertIn(task, research._viable_tasks(st))

    def test_merge_history_is_bounded_and_logged(self):
        st, r, miss_id, weak_id, new_id = self.collision_state()
        r["tasks"][weak_id]["attempt_history"] = [
            {"kind": "relation_unverified", "round": 80 + i,
             "attempts": [{"round": 80 + i, "query_id": f"RQ:weak-old-{i}",
                           "outcome": "failed", "result_count": 0}],
             "rewrites": 0, "result_count": 0}
            for i in range(research.HISTORY_KEEP)
        ]
        r["tasks"][miss_id]["attempt_history"] = [
            {"kind": "relation_unverified", "round": 70 + i,
             "attempts": [{"round": 70 + i, "query_id": f"RQ:miss-old-{i}",
                           "outcome": "failed", "result_count": 0}],
             "rewrites": 0, "result_count": 0}
            for i in range(2)
        ]
        research.ensure_state(st)
        task = r["tasks"][new_id]
        self.assertEqual(len(task["attempt_history"]), research.HISTORY_KEEP)
        self.assertEqual([x["round"] for x in task["attempt_history"]],
                         [81, 82, 83, 84, 90])
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        migrated = [x for x in rows if x.get("event") == "task_id_migrated"][-1]
        self.assertEqual(migrated["attempt_history_imported"], 2)
        self.assertEqual(migrated["attempt_history_dropped"], 3)
        truncated = [x for x in rows
                     if x.get("event") == "attempt_history_truncated"][-1]
        self.assertEqual(truncated["task_id"], new_id)
        self.assertEqual(truncated["dropped"], 3)
        self.assertEqual(truncated["keep"], research.HISTORY_KEEP)


class QueryPlanningTests(ResearchSandbox):
    def test_same_goal_gets_distinct_purposes_and_specific_text(self):
        task = {"kind": "claim_evidence_gap", "target": {
            "type": "claim", "claim_id": "C1",
            "expression": "加法满足交换律", "concepts": ["加法"]}}
        queries = research.plan_queries(task)
        self.assertGreaterEqual(len(queries), 2)
        self.assertEqual(len({q["purpose"] for q in queries}), len(queries))
        self.assertTrue(any("证据" in q["text"] for q in queries))
        self.assertTrue(any("反例" in q["text"] for q in queries))
        self.assertTrue(all("加法" in q["text"] for q in queries))

    def test_relation_queries_include_both_terms(self):
        task = {"kind": "relation_unverified", "target": {
            "type": "relation", "relation": "加法|能量",
            "concepts": ["加法", "能量"]}}
        queries = research.plan_queries(task)
        self.assertEqual(len(queries), 3)
        self.assertTrue(all("加法" in q["text"] and "能量" in q["text"]
                            for q in queries))

    def test_next_query_links_task_and_selection_reason(self):
        st = self.fresh()
        self.add_miss(st)
        query = research.next_query(st)
        self.assertIsNotNone(query)
        self.assertEqual(query["status"], "planned")
        self.assertIn(query["task_id"], research.ensure_state(st)["tasks"])
        self.assertIn("预测落空", query["selection_reason"])
        self.assertIn(query["purpose"], research.PURPOSE_LABELS)

    def test_blacklisted_task_is_not_searched(self):
        st = self.fresh()
        st["relations"] = {"密码|爆破": {"w": 1, "n": 1, "last_touch": 1}}
        self.assertIsNone(research.next_query(st))
        task = self.tasks_of(st)[0]
        self.assertEqual(task["status"], "exhausted")

    def test_fallback_creates_traceable_exploration_task(self):
        st = self.fresh()
        query = research.next_query(st, fallback_topic="涌现")
        self.assertIsNotNone(query)
        self.assertIn("涌现", query["text"])
        task = research.ensure_state(st)["tasks"][query["task_id"]]
        self.assertEqual(task["kind"], "exploration")
        self.assertEqual(query["status"], "planned")


class DedupAndFailureTests(ResearchSandbox):
    def test_same_task_uses_different_purposes_after_success(self):
        st = self.fresh()
        self.add_miss(st)
        q1 = research.next_query(st)
        research.record_execution(st, q1, True, results=[
            {"title": "t1", "snippet": "s1", "url": "https://example.org/1"}])
        q2 = research.next_query(st)
        self.assertIsNotNone(q2)
        self.assertEqual(q1["task_id"], q2["task_id"])
        self.assertNotEqual(q1["normalized"], q2["normalized"])
        self.assertNotEqual(q1["purpose"], q2["purpose"])

    def test_similar_query_is_suppressed_by_jaccard(self):
        st = self.fresh()
        r = research.ensure_state(st)
        old = "加法满足交换律 支持证据"
        r["recent_queries"].append({
            "query_id": "old", "task_id": "T", "text": old,
            "normalized": research.normalize_query(old),
            "round": 1, "outcome": "used", "result_count": 1, "failure": None,
        })
        dup = research.duplicate_reason(st, old + "啊")
        self.assertIsNotNone(dup)
        self.assertEqual(dup["kind"], "similar")
        self.assertGreaterEqual(dup["score"], config.RESEARCH_DUP_JACCARD)

    def test_all_planned_queries_duplicate_marks_task_exhausted(self):
        st = self.fresh()
        self.add_miss(st)
        research.ensure_tasks(st)
        task = self.tasks_of(st)[0]
        r = research.ensure_state(st)
        for spec in research.plan_queries(task):
            r["recent_queries"].append({
                "query_id": "old", "task_id": task["id"],
                "text": spec["text"], "normalized": research.normalize_query(spec["text"]),
                "round": 1, "outcome": "used", "result_count": 1, "failure": None,
            })
        self.assertIsNone(research.next_query(st))
        self.assertEqual(task["status"], "exhausted")
        self.assertGreaterEqual(r["stats"]["duplicates_blocked"], 3)
        self.assertEqual(len(r["queries"]), 0)

    def test_no_results_plans_one_broadened_rewrite(self):
        st = self.fresh()
        self.add_miss(st)
        q1 = research.next_query(st)
        research.record_execution(st, q1, False, failure_kind="no_results")
        r = research.ensure_state(st)
        rewrites = [q for q in r["queries"].values()
                    if q.get("rewrite_of") == q1["id"]]
        self.assertEqual(len(rewrites), 1)
        self.assertEqual(rewrites[0]["purpose"], "broadened")
        self.assertNotEqual(rewrites[0]["normalized"], q1["normalized"])
        self.assertEqual(research.next_query(st)["id"], rewrites[0]["id"])
        self.assertEqual(r["stats"]["rewrites_planned"], 1)

    def test_network_error_switches_to_another_purpose_without_rewrite(self):
        st = self.fresh()
        self.add_miss(st)
        q1 = research.next_query(st)
        research.record_execution(st, q1, False, failure_kind="network_error")
        r = research.ensure_state(st)
        self.assertFalse([q for q in r["queries"].values() if q.get("rewrite_of")])
        q2 = research.next_query(st)
        self.assertIsNotNone(q2)
        self.assertNotEqual(q1["normalized"], q2["normalized"])
        self.assertNotEqual(q1["purpose"], q2["purpose"])

    def test_attempts_are_bounded(self):
        st = self.fresh()
        self.add_miss(st)
        for _ in range(3):
            query = research.next_query(st)
            self.assertIsNotNone(query)
            research.record_execution(st, query, False, failure_kind="network_error")
        task = self.tasks_of(st)[0]
        self.assertEqual(task["status"], "exhausted")
        self.assertEqual(len(task["attempts"]), config.RESEARCH_ATTEMPTS_PER_TASK)
        self.assertIsNone(research.next_query(st))


class ProvenanceAndIntegrationTests(ResearchSandbox):
    def test_success_records_url_excerpt_hash_and_task_link(self):
        st = self.fresh()
        self.add_miss(st)
        query = research.next_query(st)
        results = [
            {"title": "加法与能量", "snippet": "摘要原文", "url": "https://example.org/a"},
            {"title": "能量的计量", "snippet": "另一段原文", "url": "https://example.org/b"},
        ]
        research.record_execution(
            st, query, True, results=results,
            serp_url="https://cn.bing.com/search?q=x", serp_sha256="serp-hash")
        rows = self.source_rows()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["task_id"], query["task_id"])
            self.assertEqual(row["query_id"], query["id"])
            self.assertEqual(row["source_type"], "web_runtime")
            self.assertEqual(row["verification"], "unverified")
            self.assertEqual(row["serp_sha256"], "serp-hash")
            self.assertTrue(row["url"].startswith("https://example.org/"))
            self.assertTrue(row["excerpt"])
            self.assertEqual(row["format"], research.SOURCE_FORMAT)
            self.assertNotIn("content_sha256", row)
            expected = hashlib.sha256(
                (row["title"] + "\n" + row["excerpt"]).encode("utf-8")).hexdigest()
            self.assertEqual(row["title_excerpt_sha256"], expected)
            self.assertTrue(row["fetched_at"])
            self.assertEqual(row["config_sha256"], research.ensure_state(st)["config_sha256"])
        task = research.ensure_state(st)["tasks"][query["task_id"]]
        self.assertEqual(task["verification"], "pending_verification")
        self.assertEqual(task["result_count"], 2)
        self.assertEqual(task["config_sha256"], research.ensure_state(st)["config_sha256"])
        sources = research.ensure_state(st)["sources"]
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0]["serp_url"], "https://cn.bing.com/search?q=x")
        self.assertEqual(sources[0]["purpose"], query["purpose"])
        self.assertEqual(st["claims"], {})
        self.assertEqual(st["evidence"], [])

    def test_bing_redirect_urls_are_unwrapped(self):
        target = "https://example.org/real-page"
        payload = "a1" + base64.urlsafe_b64encode(
            target.encode("utf-8")).decode("ascii").rstrip("=")
        wrapped = f"https://www.bing.com/ck/a?!&&p=abc&u={payload}&ntb=1"
        self.assertEqual(skills._unwrap_bing_url(wrapped), target)
        self.assertEqual(skills._unwrap_bing_url("https://example.org/plain"),
                         "https://example.org/plain")

    def test_same_url_is_indexed_once_but_logged_per_query(self):
        st = self.fresh()
        self.add_miss(st)
        q1 = research.next_query(st)
        result = [{"title": "同一结果", "snippet": "同一摘要",
                   "url": "https://example.org/same"}]
        research.record_execution(st, q1, True, results=result)
        q2 = research.next_query(st)
        research.record_execution(st, q2, True, results=result)
        sources = research.ensure_state(st)["sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["fetch_count"], 2)
        rows = self.source_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["query_id"] for row in rows}, {q1["id"], q2["id"]})

    def test_teacherless_mode_runs_without_teacher_or_llm_artifacts(self):
        with mock.patch.object(config, "TEACHERLESS", True):
            st = self.fresh()
            self.add_miss(st)
            before = teacherless.artifact_counts(st)
            query = research.next_query(st)
            research.record_execution(st, query, True, results=[
                {"title": "t", "snippet": "s", "url": "https://example.org/t"}])
            after = teacherless.artifact_counts(st)
        self.assertEqual(before, after)
        self.assertEqual(sum(after.values()), 0)

    def test_do_web_search_consumes_task_and_writes_traceable_file(self):
        st = self.fresh()
        self.add_miss(st)
        with mock.patch.object(skills, "_fetch_url_hard", return_value=FAKE_HTML):
            ok, detail = skills.do_web_search(st)
        self.assertTrue(ok, detail)
        self.assertIn("候选材料", detail)
        self.assertIn("未验证", detail)
        files = list(self.web.glob("*.md"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("https://example.org/a", text)
        self.assertIn("研究任务：RT:", text)
        self.assertIn("查询ID：RQ:", text)
        self.assertIn("unverified", text)
        task = self.tasks_of(st)[0]
        query = research.ensure_state(st)["queries"][task["query_ids"][0]]
        self.assertEqual(query["config_sha256"], research.ensure_state(st)["config_sha256"])
        self.assertEqual(query["status"], "used")
        self.assertEqual(query["result_count"], 2)
        self.assertEqual(task["verification"], "pending_verification")
        self.assertEqual(len(research.ensure_state(st)["sources"]), 2)
        log_text = (self.logs / "research.jsonl").read_text(encoding="utf-8")
        for event in ("task_created", "query_planned", "query_result",
                      "sources_recorded"):
            self.assertIn(f'"event": "{event}"', log_text)

    def test_do_web_search_failure_records_and_switches_strategy(self):
        st = self.fresh()
        self.add_miss(st)
        with mock.patch.object(skills, "_fetch_url_hard",
                               side_effect=RuntimeError("offline")):
            ok, detail = skills.do_web_search(st)
        self.assertFalse(ok)
        self.assertIn("已记录", detail)
        task = self.tasks_of(st)[0]
        self.assertEqual(task["status"], "active")
        q1 = research.ensure_state(st)["queries"][task["query_ids"][0]]
        self.assertEqual(q1["status"], "failed")
        self.assertEqual(q1["failure"], "network_error")
        q2 = research.next_query(st)
        self.assertIsNotNone(q2)
        self.assertNotEqual(q1["normalized"], q2["normalized"])

    def test_search_falls_back_to_second_host_on_transport_error(self):
        st = self.fresh()
        self.add_miss(st)
        calls = []

        def fake_fetch(url, headers):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("cn.bing TLS EOF")
            return FAKE_HTML

        with mock.patch.object(skills, "_fetch_url_hard", side_effect=fake_fetch):
            ok, detail = skills.do_web_search(st)
        self.assertTrue(ok, detail)
        self.assertEqual(len(calls), 2)
        self.assertIn("cn.bing.com", calls[0])
        self.assertIn("www.bing.com", calls[1])
        query = list(research.ensure_state(st)["queries"].values())[0]
        self.assertEqual(query["status"], "used")
        self.assertIn("www.bing.com", query["serp_url"])

    def test_do_web_search_no_results_plans_broadened_query(self):
        st = self.fresh()
        self.add_miss(st)
        with mock.patch.object(skills, "_fetch_url_hard",
                               return_value="<html><body>没有与此相关的结果</body></html>"):
            ok, _detail = skills.do_web_search(st)
        self.assertFalse(ok)
        r = research.ensure_state(st)
        task = self.tasks_of(st)[0]
        rewrites = [q for q in r["queries"].values() if q.get("purpose") == "broadened"]
        self.assertEqual(len(rewrites), 1)
        self.assertEqual(task["rewrites"], 1)
        self.assertEqual(rewrites[0]["status"], "planned")

    def test_non_result_page_is_blocked_not_no_results(self):
        st = self.fresh()
        self.add_miss(st)
        with mock.patch.object(skills, "_fetch_url_hard",
                               return_value="<html><title>必应</title></html>"):
            ok, detail = skills.do_web_search(st)
        self.assertFalse(ok)
        self.assertIn("serp_blocked", detail)
        r = research.ensure_state(st)
        task = self.tasks_of(st)[0]
        self.assertEqual(task["status"], "active")
        self.assertFalse([q for q in r["queries"].values()
                          if q.get("purpose") == "broadened"])
        q1 = r["queries"][task["query_ids"][0]]
        self.assertEqual(q1["failure"], "serp_blocked")

    def test_unbacked_recent_topic_is_not_searched(self):
        st = self.fresh()
        st["last_kw"] = "涌现"          # 最近词，但不在概念库
        (self.notes / "journey.md").write_text("今天想到了涌现\n", encoding="utf-8")
        with mock.patch.object(skills, "_fetch_url_hard") as fetch:
            ok, detail = skills.do_web_search(st)
        self.assertFalse(ok)
        self.assertIn("没有可执行的研究查询", detail)
        fetch.assert_not_called()
        self.assertFalse([t for t in self.tasks_of(st) if t["kind"] == "exploration"])

    def test_working_memory_exposes_research_focus_read_only(self):
        st = self.fresh()
        self.add_miss(st)
        research.ensure_tasks(st)
        before = {k: v for k, v in st.items() if k != "working_memory"}
        wm = working_memory.build(st)
        after = {k: v for k, v in st.items() if k != "working_memory"}
        self.assertEqual(before, after)
        self.assertTrue(wm["research_focus"])
        self.assertIn("中间概念", wm["research_focus"][0]["question"])
        self.assertIn("预测落空", wm["research_focus"][0]["reason"])

    def test_decision_boosts_web_search_for_open_task(self):
        baseline_state = self.fresh()
        baseline = decision.legacy_weights(
            baseline_state, baseline_state["emotions"], False)["web_search"]
        st = self.fresh()
        self.add_miss(st)
        research.ensure_tasks(st)
        boosted = decision.legacy_weights(st, st["emotions"], False)["web_search"]
        self.assertGreater(boosted, baseline)

    def test_config_change_is_recorded_only_when_changed(self):
        st = self.fresh()
        (self.brain / "ask_config.json").write_text(
            json.dumps({"web_search": {"top_k": 5}}), encoding="utf-8")
        sha1 = research.record_config(st)
        research.record_config(st)
        r = research.ensure_state(st)
        self.assertEqual(len(r["config_history"]), 1)
        (self.brain / "ask_config.json").write_text(
            json.dumps({"web_search": {"top_k": 2}}), encoding="utf-8")
        sha2 = research.record_config(st)
        self.assertNotEqual(sha1, sha2)
        self.assertEqual(len(r["config_history"]), 2)
        self.assertEqual(r["config_history"][-1]["values"]["web_search"]["top_k"], 2)


if __name__ == "__main__":
    unittest.main()
