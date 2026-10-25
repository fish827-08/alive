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

NO_RESULTS_HTML = "<html><body>没有与此相关的结果</body></html>"

BAD_LINKS_HTML = """
<html><body>
<li class="b_algo"><h2><a href="">缺失链接</a></h2></li>
<li class="b_algo"><h2><a href="/relative/page">相对路径</a></h2></li>
<li class="b_algo"><h2><a href="javascript:alert(1)">脚本链接</a></h2></li>
<li class="b_algo"><h2><a href="ftp://example.org/f">其它协议</a></h2></li>
<li class="b_algo"><h2><a>没有href</a></h2></li>
</body></html>
"""

MIXED_LINKS_HTML = """
<html><body>
<li class="b_algo"><h2><a href="https://example.org/valid">有效来源</a></h2>
<p>有效摘要</p></li>
<li class="b_algo"><h2><a href="/relative/page">相对路径</a></h2></li>
<li class="b_algo"><h2><a href="javascript:alert(1)">脚本链接</a></h2></li>
<li class="b_algo"><h2><a href="https://example.org/valid">重复来源</a></h2></li>
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


class CandidateFairnessTests(ResearchSandbox):
    def test_existing_tasks_do_not_consume_new_candidate_quota(self):
        st = self.fresh()
        st["relations"] = {
            f"甲{i}|乙{i}": {"w": 1, "n": 1, "last_touch": 1}
            for i in range(14)
        }
        counts = []
        for round_no in range(100, 105):
            st["rounds"] = round_no
            research.ensure_tasks(st)
            counts.append(len(research.ensure_state(st)["tasks"]))
        # 旧实现按种类截断候选，已有任务挤满 10 条关系候选后不再新建；
        # 现在每轮 3 条预算持续推进，14 条弱关系在 5 轮内全部入队。
        self.assertEqual(counts, [3, 6, 9, 12, 14])
        tasks = list(research.ensure_state(st)["tasks"].values())
        self.assertEqual(len(tasks), 14)
        self.assertEqual(len({t["id"] for t in tasks}), 14)
        self.assertTrue(all(t["status"] == "open" for t in tasks))
        self.assertEqual({t["created_round"] for t in tasks}, {100, 101, 102, 103, 104})

    def test_round_budget_does_not_skip_existing_task_refresh(self):
        st = self.fresh()
        st["rounds"] = 99
        st["relations"] = {"光|影": {"w": 1, "n": 1, "last_touch": 1}}
        research.ensure_tasks(st)
        existing = self.tasks_of(st)[0]
        self.assertEqual(existing["created_round"], 99)

        st["rounds"] = 100
        st["claims"] = {
            f"C{i}": {"id": f"C{i}", "expression": f"待验证命题{i}",
                      "status": "hypothesis", "independent_support": 0,
                      "independent_counter": 0}
            for i in range(5)
        }
        research.ensure_tasks(st)
        # 3 条高优命题占满本轮预算，已有关系任务仍被刷新而不是被跳过
        self.assertEqual(existing["last_seen_round"], 100)
        self.assertEqual(len(self.tasks_of(st, "claim_evidence_gap")), 3)
        self.assertIn(existing, self.tasks_of(st))

    def test_miss_backlog_survives_edge_preds_window_eviction(self):
        st = self.fresh()
        st["rounds"] = 100
        st["claims"] = {
            f"C{i}": {"id": f"C{i}", "expression": f"占用预算命题{i}",
                      "status": "hypothesis", "independent_support": 0,
                      "independent_counter": 0}
            for i in range(3)
        }
        research.ensure_tasks(st)
        self.assertEqual(len(self.tasks_of(st, "claim_evidence_gap")), 3)

        st["edge_preds"] = [{"a": "加法", "b": "能量", "round": 50,
                             "settled_round": 90, "status": "miss"}]
        research.ensure_tasks(st)
        r = research.ensure_state(st)
        self.assertIn("加法|能量", r["miss_backlog"])
        self.assertFalse(any(t["kind"] == "prediction_miss"
                             for t in r["tasks"].values()))

        # 模拟落空预测被 edge_preds 最近 60 条窗口淘汰
        st["edge_preds"] = [
            {"a": f"甲{i}", "b": f"乙{i}", "round": 1,
             "settled_round": 2, "status": "hit"}
            for i in range(70)
        ]
        st["rounds"] = 101
        research.ensure_tasks(st)
        r = research.ensure_state(st)
        miss_tasks = [t for t in r["tasks"].values()
                      if t["kind"] == "prediction_miss"]
        self.assertEqual(len(miss_tasks), 1)
        self.assertEqual(miss_tasks[0]["target"]["relation"], "加法|能量")
        self.assertEqual(miss_tasks[0]["created_round"], 101)
        self.assertNotIn("加法|能量", r["miss_backlog"])

    def test_miss_backlog_is_bounded_and_evicts_oldest(self):
        st = self.fresh()
        st["rounds"] = 100
        st["edge_preds"] = [
            {"a": f"甲{i}", "b": f"乙{i}", "round": 1,
             "settled_round": i, "status": "miss"}
            for i in range(config.RESEARCH_MISS_BACKLOG + 10)
        ]
        r = research.ensure_state(st)
        research._update_miss_backlog(st, r)
        self.assertEqual(len(r["miss_backlog"]), config.RESEARCH_MISS_BACKLOG)
        key0 = "|".join(sorted(["甲0", "乙0"]))
        key_last = "|".join(sorted([f"甲{config.RESEARCH_MISS_BACKLOG + 9}",
                                    f"乙{config.RESEARCH_MISS_BACKLOG + 9}"]))
        self.assertNotIn(key0, r["miss_backlog"])
        self.assertIn(key_last, r["miss_backlog"])
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        evicted = [x for x in rows if x.get("event") == "miss_backlog_evicted"]
        self.assertEqual(evicted[-1]["count"], 10)


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
        # 载体换种类：旧种类尝试归档，新种类尝试/改写预算从零开始
        self.assertEqual(task["attempts"], [])
        self.assertEqual(task["rewrites"], 0)
        self.assertEqual([x["round"] for x in task["attempt_history"]],
                         [90, 95])
        self.assertEqual(task["attempt_history"][0]["kind"],
                         "prediction_miss")
        self.assertEqual(task["attempt_history"][1]["kind"],
                         "relation_unverified")
        self.assertIn("RQ:weak", task["query_ids"])
        self.assertIn("RS:weak", task["source_ids"])
        self.assertIn(task, research._viable_tasks(st))
        query = research.next_query(st)
        self.assertIsNotNone(query)
        self.assertEqual(query["task_id"], new_id)
        self.assertEqual(query["purpose"], "bridge")

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
        # 载体换种类：当前尝试归档、预算归零；两侧既有历史全部并入
        self.assertEqual(task["attempts"], [])
        self.assertEqual(task["rewrites"], 0)
        self.assertEqual([x["round"] for x in task["attempt_history"]],
                         [70, 90, 92, 95])
        self.assertEqual(task["attempt_history"][0]["attempts"],
                         [{"round": 69, "query_id": "RQ:miss-old",
                           "outcome": "failed", "result_count": 0}])
        self.assertEqual(task["attempt_history"][1]["kind"],
                         "prediction_miss")
        self.assertEqual(task["attempt_history"][1]["attempts"],
                         [{"round": 81, "query_id": "RQ:miss1",
                           "outcome": "failed", "result_count": 0}])
        self.assertEqual(task["attempt_history"][2]["attempts"],
                         [{"round": 91, "query_id": "RQ:weak-old",
                           "outcome": "failed", "result_count": 0}])
        self.assertEqual(task["attempt_history"][3]["kind"],
                         "relation_unverified")
        self.assertEqual(task["attempt_history"][3]["attempts"],
                         [{"round": 94, "query_id": "RQ:weak",
                           "outcome": "used", "result_count": 1}])
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
                         [82, 83, 84, 90, 95])
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        migrated = [x for x in rows if x.get("event") == "task_id_migrated"][-1]
        self.assertEqual(migrated["attempt_history_own"], research.HISTORY_KEEP)
        self.assertEqual(migrated["attempt_history_imported"], 2)
        self.assertEqual(migrated["attempts_archived"], 2)
        self.assertEqual(migrated["attempt_history_kept"], research.HISTORY_KEEP)
        self.assertEqual(migrated["attempt_history_dropped"], 4)
        # 每条历史恰好落在 kept 或 dropped 之一
        self.assertEqual(migrated["attempt_history_own"]
                         + migrated["attempt_history_imported"]
                         + migrated["attempts_archived"],
                         migrated["attempt_history_kept"]
                         + migrated["attempt_history_dropped"])
        truncated = [x for x in rows
                     if x.get("event") == "attempt_history_truncated"][-1]
        self.assertEqual(truncated["task_id"], new_id)
        self.assertEqual(truncated["dropped"], 4)
        self.assertEqual(truncated["keep"], research.HISTORY_KEEP)


class SchemaV3MigrationTests(ResearchSandbox):
    RELATION = "光|影"

    def query_row(self, qid, task_id, status, purpose, text, created_round,
                  used_round=None, result_count=0):
        return {
            "id": qid, "task_id": task_id, "purpose": purpose,
            "purpose_label": research.PURPOSE_LABELS.get(purpose, purpose),
            "text": text, "normalized": research.normalize_query(text),
            "selection_reason": "测试", "status": status,
            "created_round": created_round, "used_round": used_round,
            "result_count": result_count, "failure": None,
            "rewrite_of": None, "rewrite_reason": None, "sources": [],
            "serp_url": None, "serp_sha256": None, "fetched_at": None,
            "detail": "",
        }

    def relation_task(self, task_id, kind, **overrides):
        task = {
            "id": task_id, "kind": kind,
            "kind_label": research.KIND_LABELS[kind],
            "question": "《光》和《影》是什么关系？",
            "reason": "测试任务", "target": {
                "type": "relation", "relation": self.RELATION,
                "concepts": ["光", "影"]},
            "priority": 0.7, "status": "active", "verification": "unverified",
            "created_round": 80, "updated_round": 95, "last_seen_round": 95,
            "attempts": [], "attempt_history": [], "kind_history": [],
            "query_ids": [], "source_ids": [], "result_count": 0,
            "rewrites": 0,
        }
        task.update(overrides)
        return task

    def new_id(self):
        return research._stable_id(
            "RT", research._task_key("prediction_miss",
                                     {"type": "relation", "relation": self.RELATION}))

    def test_fresh_state_is_schema_3_without_migration(self):
        st = state.default_state()
        self.assertEqual(research.RESEARCH_SCHEMA_VERSION, 3)
        self.assertEqual(st["research"]["schema_version"], 3)
        r = research.ensure_state(st)
        self.assertEqual(r["schema_version"], 3)
        self.assertFalse((self.logs / "research.jsonl").exists())

    def test_v2_normalized_id_repairs_and_replans_cancelled_query(self):
        st = self.fresh()
        st["rounds"] = 100
        r = st["research"]
        r["schema_version"] = 2
        task_id = self.new_id()
        old_q, used_q = "RQ:v2-old", "RQ:v2-used"
        bridge_text = "光和影有什么联系"
        r["queries"][old_q] = self.query_row(
            old_q, task_id, "planned", "relation", bridge_text, 84)
        r["queries"][used_q] = self.query_row(
            used_q, task_id, "used", "relation", "光和影有什么关系", 81,
            used_round=81, result_count=1)
        r["sources"].append({
            "id": "RS:v2", "task_id": task_id, "query_id": used_q,
            "url": "https://example.org/v2", "title": "光与影",
            "verification": "unverified",
        })
        task = self.relation_task(
            task_id, "prediction_miss",
            attempts=[
                {"round": 80, "query_id": used_q, "purpose": "relation",
                 "outcome": "used", "result_count": 1},
                {"round": 81, "query_id": used_q, "purpose": "relation",
                 "outcome": "used", "result_count": 1},
            ],
            query_ids=[used_q, old_q], source_ids=["RS:v2"],
            result_count=1, rewrites=1,
            kind_history=[{
                "round": 85, "from": "relation_unverified",
                "to": "prediction_miss", "archived_attempts": 0,
                "archived_rewrites": 0, "cancelled_queries": [],
            }],
        )
        r["tasks"][task_id] = task

        research.ensure_state(st)
        self.assertEqual(r["schema_version"], 3)
        self.assertEqual(task["attempts"], [])
        self.assertEqual(task["rewrites"], 0)
        self.assertEqual(len(task["attempt_history"]), 1)
        archived = task["attempt_history"][0]
        self.assertEqual(archived["kind"], "relation_unverified")
        self.assertEqual([a["round"] for a in archived["attempts"]], [80, 81])
        self.assertEqual(archived["repaired_by"], "schema-3")
        self.assertEqual(r["queries"][old_q]["status"], "cancelled")
        self.assertEqual(r["queries"][old_q]["failure"], "superseded")
        self.assertEqual(r["queries"][used_q]["status"], "used")
        self.assertEqual(task["kind_history"][-1]["cancelled_queries"], [old_q])

        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        migrated = [x for x in rows
                    if x.get("event") == "research_schema_migrated"][-1]
        self.assertEqual(migrated["from_version"], 2)
        self.assertEqual(migrated["to_version"], 3)
        self.assertEqual(migrated["tasks_repaired"], 1)
        self.assertEqual(migrated["tasks_unrecoverable"], 0)
        repaired = [x for x in rows
                    if x.get("event") == "task_migration_repaired"][-1]
        self.assertEqual(repaired["archived_attempts"], 2)
        self.assertEqual(repaired["cancelled_queries"], [old_q])

        snapshot = json.dumps(r, ensure_ascii=False, sort_keys=True)
        logs_before = (self.logs / "research.jsonl").read_text(encoding="utf-8")
        research.ensure_state(st)
        self.assertEqual(json.dumps(r, ensure_ascii=False, sort_keys=True),
                         snapshot)
        self.assertEqual(
            (self.logs / "research.jsonl").read_text(encoding="utf-8"),
            logs_before)
        self.assertEqual(len([x for x in rows
                              if x.get("event") == "research_schema_migrated"]), 1)

        # 作废查询不占“已用过文本”额度，同文本可按新种类重新规划
        used_norm = research._task_query_normalized(task, r)
        self.assertNotIn(research.normalize_query(bridge_text), used_norm)
        query = research.next_query(st)
        self.assertIsNotNone(query)
        self.assertEqual(query["task_id"], task_id)
        self.assertEqual(query["purpose"], "bridge")
        self.assertEqual(query["text"], bridge_text)
        self.assertNotEqual(query["id"], old_q)
        self.assertEqual(query["status"], "planned")

    def test_v2_unrecoverable_history_is_logged_and_left_untouched(self):
        st = self.fresh()
        st["rounds"] = 100
        r = st["research"]
        r["schema_version"] = 2
        task_id = self.new_id()
        old_q = "RQ:v2-unrecoverable"
        r["queries"][old_q] = self.query_row(
            old_q, task_id, "planned", "relation", "光和影有什么关系", 70)
        attempt = {"round": 80, "query_id": old_q, "purpose": "relation",
                   "outcome": "failed", "result_count": 0}
        task = self.relation_task(
            task_id, "prediction_miss", attempts=[attempt],
            query_ids=[old_q], rewrites=2,
            kind_history=[{"round": None, "from": "relation_unverified",
                           "to": "prediction_miss"}],
        )
        r["tasks"][task_id] = task

        research.ensure_state(st)
        # 无法定位迁移轮次：不猜测、不改写，只记录不可恢复
        self.assertEqual(task["attempts"], [attempt])
        self.assertEqual(task["rewrites"], 2)
        self.assertEqual(r["queries"][old_q]["status"], "planned")
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        unrecoverable = [x for x in rows
                         if x.get("event") == "task_migration_unrecoverable"]
        self.assertEqual(len(unrecoverable), 1)
        self.assertEqual(unrecoverable[0]["reason"], "invalid_transition_round")
        self.assertEqual(unrecoverable[0]["attempts"], 1)
        self.assertEqual(unrecoverable[0]["planned_queries"], 1)
        self.assertFalse([x for x in rows
                          if x.get("event") == "task_migration_repaired"])
        migrated = [x for x in rows
                    if x.get("event") == "research_schema_migrated"][-1]
        self.assertEqual(migrated["tasks_unrecoverable"], 1)

    def test_single_task_oversized_kind_history_is_bounded_and_logged(self):
        st = self.fresh()
        st["rounds"] = 100
        r = st["research"]
        r["schema_version"] = 2
        task_id = self.new_id()
        history = [{"round": 61 + i, "from": "relation_unverified",
                    "to": "prediction_miss"} for i in range(7)]
        task = self.relation_task(
            task_id, "prediction_miss", kind_history=history)
        r["tasks"][task_id] = task

        research.ensure_state(st)
        self.assertEqual([x["round"] for x in task["kind_history"]],
                         [63, 64, 65, 66, 67])
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        truncated = [x for x in rows
                     if x.get("event") == "kind_history_truncated"][-1]
        self.assertEqual(truncated["dropped"], 2)
        self.assertEqual(truncated["keep"], 5)
        migrated = [x for x in rows
                    if x.get("event") == "task_id_migrated"][-1]
        self.assertEqual(migrated["kind_history_own"], 7)
        self.assertEqual(migrated["kind_history_kept"], 5)
        self.assertEqual(migrated["kind_history_dropped"], 2)

    def carrier_state(self):
        st = self.fresh()
        st["rounds"] = 100
        r = st["research"]
        r["schema_version"] = 2
        new_id = self.new_id()
        miss_id = research._stable_id(
            "RT", f"prediction_miss|relation|{self.RELATION}")
        weak_id = research._stable_id(
            "RT", f"relation_unverified|relation|{self.RELATION}")
        miss = self.relation_task(
            miss_id, "prediction_miss", status="exhausted",
            updated_round=90, last_seen_round=90, created_round=70,
            priority=0.8, attempts=[{
                "round": 81, "query_id": "RQ:miss1",
                "outcome": "failed", "result_count": 0,
            }], query_ids=["RQ:miss1"],
        )
        weak = self.relation_task(
            weak_id, "relation_unverified", status="active",
            updated_round=95, last_seen_round=95, created_round=80,
            priority=0.6, attempts=[
                {"round": 93, "query_id": "RQ:weak1",
                 "outcome": "failed", "result_count": 0},
                {"round": 94, "query_id": "RQ:weak2",
                 "outcome": "used", "result_count": 1},
            ], query_ids=["RQ:weak1", "RQ:weak2", "RQ:weakplan"],
            source_ids=["RS:weak"], result_count=1, rewrites=1,
        )
        r["tasks"][miss_id] = miss
        r["tasks"][weak_id] = weak
        r["queries"]["RQ:miss1"] = self.query_row(
            "RQ:miss1", miss_id, "used", "bridge",
            "光和影之间有什么中间概念", 80, used_round=81)
        r["queries"]["RQ:weak1"] = self.query_row(
            "RQ:weak1", weak_id, "used", "relation", "光和影有什么关系", 92,
            used_round=93)
        r["queries"]["RQ:weak2"] = self.query_row(
            "RQ:weak2", weak_id, "used", "evidence", "光和影 相关证据", 93,
            used_round=94, result_count=1)
        r["queries"]["RQ:weakplan"] = self.query_row(
            "RQ:weakplan", weak_id, "planned", "counterexample",
            "光和影 无关的反例", 95)
        r["sources"].append({
            "id": "RS:weak", "task_id": weak_id, "query_id": "RQ:weak2",
            "url": "https://example.org/weak", "title": "光影",
            "verification": "unverified",
        })
        return st, r, new_id, weak_id, miss_id

    def test_carrier_kind_change_cancels_old_query_and_resets_budget(self):
        st, r, new_id, weak_id, miss_id = self.carrier_state()
        research.ensure_state(st)
        task = r["tasks"][new_id]
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(task["status"], "active")
        # 旧种类两条尝试归档、改写预算清零
        self.assertEqual(task["attempts"], [])
        self.assertEqual(task["rewrites"], 0)
        self.assertEqual([x["round"] for x in task["attempt_history"]],
                         [90, 95])
        self.assertEqual(task["attempt_history"][0]["kind"], "prediction_miss")
        self.assertEqual(len(task["attempt_history"][0]["attempts"]), 1)
        self.assertEqual(task["attempt_history"][1]["kind"],
                         "relation_unverified")
        self.assertEqual(len(task["attempt_history"][1]["attempts"]), 2)
        # 旧种类的计划查询作废；已执行查询、来源与关联保留
        self.assertEqual(r["queries"]["RQ:weakplan"]["status"], "cancelled")
        self.assertEqual(r["queries"]["RQ:weakplan"]["failure"], "superseded")
        self.assertEqual(r["queries"]["RQ:weak1"]["status"], "used")
        self.assertEqual(r["queries"]["RQ:weak2"]["status"], "used")
        self.assertEqual(r["queries"]["RQ:miss1"]["status"], "used")
        self.assertEqual(task["source_ids"], ["RS:weak"])
        self.assertIn("RS:weak", task["source_ids"])
        self.assertEqual(r["queries"]["RQ:weak2"]["task_id"], new_id)
        self.assertEqual(r["sources"][0]["task_id"], new_id)
        last = task["kind_history"][-1]
        self.assertEqual(last["from"], "relation_unverified")
        self.assertEqual(last["to"], "prediction_miss")
        self.assertEqual(last["archived_attempts"], 2)
        self.assertEqual(last["archived_rewrites"], 1)
        self.assertEqual(last["cancelled_queries"], ["RQ:weakplan"])

        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        migrated = [x for x in rows if x.get("event") == "task_id_migrated"][-1]
        self.assertEqual(migrated["attempt_history_own"], 0)
        self.assertEqual(migrated["attempt_history_imported"], 0)
        self.assertEqual(migrated["attempts_archived"], 2)
        self.assertEqual(migrated["attempt_rows_archived"], 3)
        self.assertEqual(migrated["attempt_history_kept"], 2)
        self.assertEqual(migrated["attempt_history_dropped"], 0)
        self.assertEqual(migrated["cancelled_queries"], ["RQ:weakplan"])

        query = research.next_query(st)
        self.assertIsNotNone(query)
        self.assertEqual(query["task_id"], new_id)
        self.assertEqual(query["purpose"], "bridge")
        self.assertEqual(query["text"], "光和影有什么联系")
        self.assertEqual(query["status"], "planned")

    def test_migration_preserves_final_kind_planned_query(self):
        st, r, new_id, weak_id, miss_id = self.carrier_state()
        r["queries"]["RQ:missplan"] = self.query_row(
            "RQ:missplan", miss_id, "planned", "bridge",
            "光和影有什么联系", 89)
        r["tasks"][miss_id]["query_ids"].append("RQ:missplan")
        research.ensure_state(st)
        task = r["tasks"][new_id]
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(r["queries"]["RQ:missplan"]["status"], "planned")
        self.assertNotIn("RQ:missplan", task["kind_history"][-1]["cancelled_queries"])
        query = research.next_query(st)
        self.assertEqual(query["id"], "RQ:missplan")
        self.assertEqual(query["purpose"], "bridge")

    def test_multi_task_merge_keeps_newest_history_and_reconciles(self):
        st = self.fresh()
        st["rounds"] = 100
        r = st["research"]
        r["schema_version"] = 2
        new_id = self.new_id()
        miss_id = research._stable_id(
            "RT", f"prediction_miss|relation|{self.RELATION}")
        weak_id = research._stable_id(
            "RT", f"relation_unverified|relation|{self.RELATION}")

        def history(kind, rounds, prefix):
            return [{
                "kind": kind, "round": rnd,
                "attempts": [{"round": rnd, "query_id": f"RQ:{prefix}-{rnd}",
                              "outcome": "failed", "result_count": 0}],
                "rewrites": 0, "result_count": 0,
            } for rnd in rounds]

        a = self.relation_task(
            new_id, "relation_unverified", status="active",
            updated_round=95, last_seen_round=95, created_round=80,
            attempts=[{"round": 93, "query_id": "RQ:a1", "outcome": "failed"},
                      {"round": 94, "query_id": "RQ:a2", "outcome": "failed"}],
            attempt_history=history("relation_unverified", [90, 91], "a"),
            kind_history=[{"round": 60, "from": "exploration",
                           "to": "relation_unverified"}],
            rewrites=1,
        )
        b = self.relation_task(
            miss_id, "prediction_miss", status="exhausted",
            updated_round=89, last_seen_round=89, created_round=60,
            priority=0.8,
            attempts=[{"round": 88, "query_id": "RQ:b1", "outcome": "failed"}],
            attempt_history=history("prediction_miss", [70, 71, 72], "b"),
            kind_history=[
                {"round": 64, "from": "exploration", "to": "relation_unverified"},
                {"round": 66, "from": "relation_unverified",
                 "to": "prediction_miss"},
                {"round": 68, "from": "prediction_miss",
                 "to": "relation_unverified"},
            ],
        )
        c = self.relation_task(
            weak_id, "relation_unverified", status="exhausted",
            updated_round=85, last_seen_round=85, created_round=50,
            priority=0.6,
            attempts=[{"round": 84, "query_id": "RQ:c1", "outcome": "failed"}],
            attempt_history=history("relation_unverified",
                                    [80, 81, 82, 83], "c"),
            kind_history=[
                {"round": 71, "from": "exploration", "to": "prediction_miss"},
                {"round": 73, "from": "prediction_miss",
                 "to": "relation_unverified"},
                {"round": 76, "from": "exploration", "to": "relation_unverified"},
            ],
        )
        r["tasks"][new_id] = a
        r["tasks"][miss_id] = b
        r["tasks"][weak_id] = c

        research.ensure_state(st)
        task = r["tasks"][new_id]
        self.assertEqual(task["kind"], "prediction_miss")
        self.assertEqual(task["attempts"], [])
        self.assertEqual(task["rewrites"], 0)
        # 朴素拼接后尾部截断会丢掉 A 的 90/91/95；排序保留最新五条
        self.assertEqual([x["round"] for x in task["attempt_history"]],
                         [85, 89, 90, 91, 95])
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        migrated = [x for x in rows if x.get("event") == "task_id_migrated"][-1]
        self.assertEqual(migrated["attempt_history_own"], 2)
        self.assertEqual(migrated["attempt_history_imported"], 7)
        self.assertEqual(migrated["attempts_archived"], 3)
        self.assertEqual(migrated["attempt_rows_archived"], 4)
        self.assertEqual(migrated["attempt_history_kept"], 5)
        self.assertEqual(migrated["attempt_history_dropped"], 7)
        self.assertEqual(migrated["attempt_history_own"]
                         + migrated["attempt_history_imported"]
                         + migrated["attempts_archived"],
                         migrated["attempt_history_kept"]
                         + migrated["attempt_history_dropped"])
        self.assertEqual([x["round"] for x in task["kind_history"]],
                         [68, 71, 73, 76, 100])
        self.assertEqual(migrated["kind_history_own"], 1)
        self.assertEqual(migrated["kind_history_imported"], 6)
        self.assertEqual(migrated["kind_history_added"], 1)
        self.assertEqual(migrated["kind_history_kept"], 5)
        self.assertEqual(migrated["kind_history_dropped"], 3)
        self.assertEqual(migrated["kind_history_own"]
                         + migrated["kind_history_imported"]
                         + migrated["kind_history_added"],
                         migrated["kind_history_kept"]
                         + migrated["kind_history_dropped"])
        truncated = [x for x in rows
                     if x.get("event") == "attempt_history_truncated"][-1]
        self.assertEqual(truncated["dropped"], 7)
        kind_truncated = [x for x in rows
                          if x.get("event") == "kind_history_truncated"][-1]
        self.assertEqual(kind_truncated["dropped"], 3)


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


class SourceValidationTests(ResearchSandbox):
    def test_parser_rejects_missing_relative_and_non_http_links(self):
        self.assertEqual(skills._parse_bing_results(BAD_LINKS_HTML, 5), [])

    def test_parser_dedups_valid_url_and_drops_invalid_neighbours(self):
        results = skills._parse_bing_results(MIXED_LINKS_HTML, 10)
        self.assertEqual(len(results), 1)
        title, _snippet, link = results[0]
        self.assertEqual(title, "有效来源")
        self.assertEqual(link, "https://example.org/valid")

    def test_record_execution_counts_only_recorded_valid_sources(self):
        st = self.fresh()
        self.add_miss(st)
        query = research.next_query(st)
        results = [
            {"title": "缺URL", "snippet": "s"},
            {"title": "相对", "snippet": "s", "url": "/relative/path"},
            {"title": "脚本", "snippet": "s", "url": "javascript:alert(1)"},
            {"title": "ftp", "snippet": "s", "url": "ftp://example.org/f"},
            {"title": "空白", "snippet": "s", "url": "   "},
            {"title": "有效", "snippet": "原文", "url": "https://example.org/ok"},
        ]
        query = research.record_execution(st, query, True, results=results)
        r = research.ensure_state(st)
        task = r["tasks"][query["task_id"]]
        self.assertEqual(query["status"], "used")
        self.assertEqual(query["failure"], None)
        self.assertEqual(query["result_count"], 1)
        self.assertEqual(task["result_count"], 1)
        self.assertEqual(task["verification"], "pending_verification")
        self.assertEqual(len(r["sources"]), 1)
        self.assertEqual(r["sources"][0]["url"], "https://example.org/ok")
        self.assertEqual(r["sources"][0]["verification"], "unverified")
        self.assertEqual(r["stats"]["sources_recorded"], 1)
        self.assertEqual(r["stats"]["source_candidates_rejected"], 5)
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        rejected = [x for x in rows
                    if x.get("event") == "source_candidates_rejected"][-1]
        self.assertEqual(rejected["count"], 5)

    def test_invalid_only_results_do_not_create_materials(self):
        st = self.fresh()
        self.add_miss(st)
        query = research.next_query(st)
        query = research.record_execution(st, query, True, results=[
            {"title": "空", "snippet": "s", "url": ""},
            {"title": "相对", "snippet": "s", "url": "/relative"},
            {"title": "邮件", "snippet": "s", "url": "mailto:a@example.org"},
        ])
        r = research.ensure_state(st)
        task = r["tasks"][query["task_id"]]
        self.assertEqual(query["status"], "failed")
        self.assertEqual(query["failure"], "invalid_results")
        self.assertEqual(query["result_count"], 0)
        self.assertEqual(task["result_count"], 0)
        self.assertEqual(task["verification"], "unverified")
        self.assertEqual(task["source_ids"], [])
        self.assertEqual(task["status"], "active")
        self.assertEqual(r["sources"], [])
        self.assertEqual(r["stats"]["sources_recorded"], 0)
        self.assertEqual(r["stats"]["query_failures"], 1)
        self.assertEqual(r["recent_queries"][-1]["outcome"], "failed")
        self.assertEqual(r["recent_queries"][-1]["result_count"], 0)

    def test_empty_results_are_failure_not_success(self):
        st = self.fresh()
        self.add_miss(st)
        query = research.next_query(st)
        query = research.record_execution(st, query, True, results=[])
        r = research.ensure_state(st)
        task = r["tasks"][query["task_id"]]
        self.assertEqual(query["status"], "failed")
        self.assertEqual(query["failure"], "no_results")
        self.assertEqual(task["result_count"], 0)
        self.assertEqual(task["verification"], "unverified")
        self.assertEqual(r["sources"], [])

    def test_duplicate_url_within_one_serp_is_recorded_once(self):
        st = self.fresh()
        self.add_miss(st)
        query = research.next_query(st)
        query = research.record_execution(st, query, True, results=[
            {"title": "第一标题", "snippet": "s1",
             "url": "https://example.org/same"},
            {"title": "第二标题", "snippet": "s2",
             "url": "https://example.org/same"},
        ])
        r = research.ensure_state(st)
        task = r["tasks"][query["task_id"]]
        self.assertEqual(query["result_count"], 1)
        self.assertEqual(task["result_count"], 1)
        self.assertEqual(len(r["sources"]), 1)
        self.assertEqual(r["stats"]["sources_recorded"], 1)
        self.assertEqual(r["sources"][0]["title"], "第一标题")


class SearchHostFallbackTests(ResearchSandbox):
    def test_no_results_on_cn_host_still_tries_www(self):
        st = self.fresh()
        self.add_miss(st)
        calls = []

        def fake_fetch(url, headers):
            calls.append(url)
            if "cn.bing.com" in url:
                return NO_RESULTS_HTML
            return FAKE_HTML

        with mock.patch.object(skills, "_fetch_url_hard", side_effect=fake_fetch):
            ok, detail = skills.do_web_search(st)
        self.assertTrue(ok, detail)
        self.assertEqual(len(calls), 2)
        self.assertIn("cn.bing.com", calls[0])
        self.assertIn("www.bing.com", calls[1])
        r = research.ensure_state(st)
        query = list(r["queries"].values())[0]
        self.assertEqual(query["status"], "used")
        self.assertIn("www.bing.com", query["serp_url"])
        self.assertEqual(query["result_count"], 2)
        task = self.tasks_of(st)[0]
        self.assertEqual(task["verification"], "pending_verification")
        self.assertEqual(task["status"], "active")
        self.assertEqual(task["rewrites"], 0)
        self.assertFalse([q for q in r["queries"].values()
                          if q.get("purpose") == "broadened"])
        self.assertEqual(r["stats"]["query_failures"], 0)

    def test_all_invalid_links_never_become_materials(self):
        st = self.fresh()
        self.add_miss(st)
        with mock.patch.object(skills, "_fetch_url_hard",
                               return_value=BAD_LINKS_HTML):
            ok, detail = skills.do_web_search(st)
        self.assertFalse(ok)
        self.assertIn("serp_blocked", detail)
        r = research.ensure_state(st)
        task = self.tasks_of(st)[0]
        query = r["queries"][task["query_ids"][0]]
        self.assertEqual(query["status"], "failed")
        self.assertEqual(query["failure"], "serp_blocked")
        self.assertEqual(query["result_count"], 0)
        self.assertEqual(task["result_count"], 0)
        self.assertEqual(task["verification"], "unverified")
        self.assertEqual(r["sources"], [])
        self.assertEqual(r["stats"]["sources_recorded"], 0)


class PendingArchiveTests(ResearchSandbox):
    def materials_task(self, r, i):
        task_id = f"RT:mat{i}"
        query_id = f"RQ:mat{i}"
        source_id = f"RS:mat{i}"
        updated = (i + 1) * 10
        r["tasks"][task_id] = {
            "id": task_id, "kind": "claim_evidence_gap",
            "kind_label": "命题证据缺口",
            "question": f"有哪些资料能支持或反驳「命题{i}」？",
            "reason": "命题证据缺口",
            "target": {"type": "claim", "claim_id": f"C{i}",
                       "expression": f"命题{i}", "concepts": [f"命题{i}"]},
            "priority": 0.7, "status": "materials_found",
            "verification": "pending_verification",
            "created_round": 10, "updated_round": updated,
            "last_seen_round": updated,
            "attempts": [{"round": updated, "query_id": query_id,
                          "outcome": "used", "result_count": 1}],
            "attempt_history": [], "kind_history": [],
            "query_ids": [query_id], "source_ids": [source_id],
            "result_count": 1, "rewrites": 0,
        }
        r["queries"][query_id] = {
            "id": query_id, "task_id": task_id, "purpose": "evidence",
            "text": f"命题{i} 支持证据", "normalized": f"命题{i}支持证据",
            "status": "used", "created_round": 10, "used_round": updated,
            "result_count": 1, "sources": [source_id],
        }
        r["sources"].append({
            "id": source_id, "task_id": task_id, "query_id": query_id,
            "url": f"https://example.org/{i}", "title": f"来源{i}",
            "verification": "unverified", "source_type": "web_runtime",
        })
        return task_id

    def test_material_overflow_is_archived_with_recoverable_links(self):
        st = self.fresh()
        st["rounds"] = 100
        r = research.ensure_state(st)
        ids = [self.materials_task(r, i) for i in range(4)]
        with mock.patch.object(config, "RESEARCH_TASK_KEEP", 3):
            research.ensure_tasks(st)
        r = research.ensure_state(st)
        self.assertEqual(len(r["tasks"]), 3)
        self.assertNotIn(ids[0], r["tasks"])
        self.assertEqual(len(r["archived_tasks"]), 1)
        entry = r["archived_tasks"][0]
        self.assertEqual(entry["id"], ids[0])
        self.assertEqual(entry["kind"], "claim_evidence_gap")
        self.assertEqual(entry["status"], "materials_found")
        self.assertEqual(entry["verification"], "pending_verification")
        self.assertEqual(entry["archive_reason"], "material_cap")
        self.assertEqual(entry["query_ids"], [f"RQ:mat0"])
        self.assertEqual(entry["source_ids"], [f"RS:mat0"])
        self.assertEqual(entry["queries"][0]["id"], "RQ:mat0")
        self.assertEqual(entry["queries"][0]["text"], "命题0 支持证据")
        self.assertEqual(entry["queries"][0]["status"], "used")
        self.assertEqual(entry["sources"][0]["id"], "RS:mat0")
        self.assertEqual(entry["sources"][0]["url"], "https://example.org/0")
        self.assertEqual(entry["sources"][0]["query_id"], "RQ:mat0")
        self.assertEqual(entry["sources"][0]["verification"], "unverified")
        # 查询/来源索引仍在，任务归档后关联可恢复
        self.assertIn("RQ:mat0", r["queries"])
        self.assertTrue(any(s["id"] == "RS:mat0" for s in r["sources"]))
        self.assertEqual(r["stats"]["tasks_archived"], 1)
        pending = research.archived_pending(st)
        self.assertEqual([x["id"] for x in pending], [ids[0]])
        self.assertEqual(research.summary(st)["archived_tasks"], 1)
        focus = research.focus(st, limit=5)
        archived = [x for x in focus if x["archived"]]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["id"], ids[0])
        self.assertEqual(archived[0]["reason"], "material_cap")
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        archived_log = [x for x in rows if x.get("event") == "task_archived"][-1]
        self.assertEqual(archived_log["task_id"], ids[0])
        self.assertTrue(archived_log["archived"])
        self.assertEqual(archived_log["reason"], "material_cap")
        self.assertEqual(archived_log["source_ids"], ["RS:mat0"])

    def test_archive_capacity_is_bounded_and_eviction_is_logged(self):
        st = self.fresh()
        st["rounds"] = 100
        r = research.ensure_state(st)
        ids = [self.materials_task(r, i) for i in range(5)]
        with mock.patch.object(config, "RESEARCH_TASK_KEEP", 2), \
                mock.patch.object(config, "RESEARCH_ARCHIVE_KEEP", 2):
            research.ensure_tasks(st)
        r = research.ensure_state(st)
        self.assertEqual(sorted(r["tasks"]), ids[3:])
        self.assertEqual([x["id"] for x in r["archived_tasks"]], ids[1:3])
        self.assertEqual(r["stats"]["tasks_archived"], 3)
        rows = [json.loads(line) for line in
                (self.logs / "research.jsonl").read_text(
                    encoding="utf-8").splitlines() if line]
        evicted = [x for x in rows
                   if x.get("event") == "task_archive_evicted"][-1]
        self.assertEqual(evicted["count"], 1)
        self.assertEqual(evicted["keep"], 2)
        self.assertEqual([x["id"] for x in research.archived_pending(st)],
                         [ids[2], ids[1]])


if __name__ == "__main__":
    unittest.main()
