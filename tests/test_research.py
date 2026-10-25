# -*- coding: utf-8 -*-
"""阶段 2 回归：研究任务来源、定向查询、重复抑制、失败改写与来源追溯。"""
import base64
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
            self.assertEqual(len(row["content_sha256"]), 64)
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
