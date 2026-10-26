# -*- coding: utf-8 -*-
"""阶段 3.0 回归：候选验证闭环、抓取安全、来源独立性与重复执行。"""
import hashlib
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from selfdrive import config, research, state, teacherless, verification

SUPPORT_HTML = (
    "<html><head><title>加法与能量</title></head><body>"
    "<p>研究发现，加法与能量之间存在相关关系。</p>"
    "<p>加法可以描述能量的变化过程。</p>"
    "</body></html>"
)
SUPPORT_HTML_2 = (
    "<html><head><title>加法和能量</title></head><body>"
    "<p>研究表明，加法与能量存在关联。</p>"
    "<p>这一结论来自对多个案例的观察与比较。</p>"
    "</body></html>"
)
CONTRADICT_HTML = (
    "<html><head><title>不同概念</title></head><body>"
    "<p>加法与能量没有关系，二者是不同概念。</p>"
    "<p>加法不等于能量。</p>"
    "</body></html>"
)
UNCERTAIN_HTML = (
    "<html><head><title>并列提及</title></head><body>"
    "<p>这一页同时提到了加法与能量。</p>"
    "</body></html>"
)
EMPTY_HTML = "<html><body><script>var x = 1;</script></body></html>"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ok_result(url, body, content_type="text/html; charset=utf-8",
              content_encoding="identity"):
    return {
        "status": "ok",
        "url": url,
        "final_url": url,
        "http_status": 200,
        "content_type": content_type,
        "charset": "utf-8",
        "content_encoding": content_encoding,
        "body": body,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "truncated": False,
        "fetched_at": "2026-10-26T10:00:00",
        "redirects": [],
    }


class VerificationSandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.logs = self.root / "logs"
        self.brain = self.root / "brain"
        for path in (self.logs, self.brain):
            path.mkdir(parents=True, exist_ok=True)
        self.patches = [
            mock.patch.object(config, "BRAIN", self.brain),
            mock.patch.object(state, "LOGS", self.logs),
            mock.patch.object(state, "STATE_FILE", self.root / "state.json"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def fresh_state(self):
        st = state.default_state()
        st["rounds"] = 100
        return st

    def add_task(self, st, urls, *, task_id="RT-phase3-0001",
                 query_id="RQ-phase3-0001"):
        task_id = task_id
        sources = []
        for index, url in enumerate(urls):
            source_id = f"RS-{task_id}-{index:02d}"
            sources.append({
                "id": source_id,
                "task_id": task_id,
                "query_id": query_id,
                "url": url,
                "title": f"合成来源 {index}",
                "excerpt": "合成摘要",
                "source_type": "web_runtime",
                "verification": "unverified",
            })
        task = {
            "id": task_id,
            "kind": "prediction_miss",
            "question": "《加法》和《能量》之间缺少什么中间概念或条件？",
            "target": {"type": "relation", "relation": "加法|能量",
                       "concepts": ["加法", "能量"]},
            "status": "materials_found",
            "verification": "pending_verification",
            "query_ids": [query_id],
            "source_ids": [s["id"] for s in sources],
            "sources": sources,
        }
        st["research"]["tasks"][task_id] = task
        st["research"]["sources"].extend(sources)
        return task

    def ledger_rows(self):
        return verification.read_ledger()

    def fetcher(self, mapping):
        def fetch(url):
            item = mapping[url]
            if isinstance(item, Exception):
                raise item
            if isinstance(item, str):
                return ok_result(url, item.encode("utf-8"))
            if isinstance(item, bytes):
                return ok_result(url, item)
            return ok_result(url, item[0], *item[1:])
        return fetch

    def assert_no_promotion(self, st, before):
        self.assertEqual(json.dumps(st, ensure_ascii=False, sort_keys=True),
                         before)
        self.assertEqual(st.get("claims"), {})
        self.assertEqual(st.get("evidence"), [])
        self.assertEqual(st.get("laws"), {})
        self.assertEqual(st.get("relations"), {})
        self.assertEqual(teacherless.artifact_counts(st),
                         {k: 0 for k in teacherless.TEACHER_ARTIFACT_KEYS})


class EndToEndTests(VerificationSandbox):
    def test_research_task_to_ledger_is_traceable_and_read_only(self):
        st = self.fresh_state()
        st["edge_preds"] = [{
            "a": "加法", "b": "能量", "round": 10, "deadline": 70,
            "status": "miss", "settled_round": 75,
        }]
        research.ensure_tasks(st)
        tasks = list(research.ensure_state(st)["tasks"].values())
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["kind"], "prediction_miss")
        query = research.next_query(st)
        self.assertIsNotNone(query)
        research.record_execution(
            st, query, True,
            results=[{
                "url": "https://example.org/a",
                "title": "加法与能量的关系",
                "snippet": "摘要原文一：加法与能量在同一条资料里出现。",
            }],
            serp_url="https://www.bing.com/search?q=test",
            serp_sha256="b" * 64,
            fetched_at="2026-10-26T09:59:00",
        )
        research.ensure_tasks(st)
        stored_task = research.ensure_state(st)["tasks"][task["id"]]
        self.assertEqual(stored_task["verification"], "pending_verification")
        sources = verification.sources_for_task(st, stored_task)
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["url"], "https://example.org/a")

        before = json.dumps(st, ensure_ascii=False, sort_keys=True)
        result = verification.verify_task(
            st, stored_task,
            fetcher=self.fetcher({"https://example.org/a": SUPPORT_HTML}))
        self.assertEqual(result["appended"], 1)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(result["failures"], 0)
        self.assertEqual(len(self.ledger_rows()), 1)
        row = self.ledger_rows()[0]

        # 每一步都能从任务 → 查询 → 来源 → 账本追溯
        self.assertEqual(row["task_id"], stored_task["id"])
        self.assertEqual(row["task_kind"], "prediction_miss")
        self.assertEqual(row["query_id"], query["id"])
        self.assertEqual(row["source_id"], source["id"])
        self.assertEqual(row["url"], source["url"])
        self.assertEqual(row["body_sha256"], hashlib.sha256(
            SUPPORT_HTML.encode("utf-8")).hexdigest())
        self.assertEqual(row["fetched_at"], "2026-10-26T10:00:00")
        self.assertTrue(row["event_at"])
        self.assertEqual(row["extraction_status"], "ok")
        self.assertEqual(row["judgment"], "support")
        self.assertIn("相关", row["evidence_excerpt"])
        self.assertIn("terms-cues-v1", row["basis"])
        self.assertEqual(row["verifier_version"], verification.VERIFIER_VERSION)
        self.assertEqual(row["config_sha256"], verification.CONFIG_SHA256)
        self.assertEqual(row["conclusion_status"], "candidate")
        self.assertEqual(row["promotion"], "none")
        self.assertTrue(row["evidence_counted"])
        self.assertEqual(row["independence_group"], "domain:example.org")

        # 只读：任务、来源与状态均未被验证器修改
        self.assertEqual(stored_task["verification"], "pending_verification")
        self.assertEqual(source["verification"], "unverified")
        self.assert_no_promotion(st, before)

        summary = verification.summarize_task(self.ledger_rows(), stored_task["id"])
        self.assertEqual(summary["conclusion"], "candidate_support")
        self.assertEqual(summary["support_groups"], 1)
        self.assertEqual(summary["promotion"], "none")
        candidates = verification.latest_candidates(st)
        self.assertEqual(candidates[0]["task_id"], stored_task["id"])
        self.assertEqual(candidates[0]["conclusion_status"], "candidate")

    def test_repeated_execution_is_idempotent(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        fetch = self.fetcher({"https://example.org/a": SUPPORT_HTML})
        first = verification.verify_task(st, task, fetcher=fetch)
        second = verification.verify_task(st, task, fetcher=fetch)
        self.assertEqual(first["appended"], 1)
        self.assertEqual(second["appended"], 0)
        self.assertEqual(second["duplicates"], 1)
        self.assertEqual(len(self.ledger_rows()), 1)
        summary = verification.summarize_task(self.ledger_rows(), task["id"])
        self.assertEqual(summary["records"], 1)
        self.assertEqual(summary["support_groups"], 1)

    def test_failed_fetch_repeat_is_deduped(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        failure = {"status": "failed", "url": "https://example.org/a",
                   "final_url": "https://example.org/a",
                   "failure_kind": "network_error", "detail": "refused",
                   "http_status": 0, "fetched_at": "2026-10-26T10:00:00",
                   "redirects": []}
        first = verification.verify_task(st, task, fetcher=lambda _url: dict(failure))
        second = verification.verify_task(st, task, fetcher=lambda _url: dict(failure))
        self.assertEqual(first["failures"], 1)
        self.assertEqual(second["duplicates"], 1)
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["failure_kind"], "network_error")
        self.assertEqual(rows[0]["extraction_status"], "fetch_failed")
        self.assertFalse(rows[0]["evidence_counted"])
        self.assertEqual(rows[0]["promotion"], "none")

    def test_invalid_source_url_never_calls_fetcher(self):
        st = self.fresh_state()
        task = self.add_task(st, ["ftp://example.org/a", ""])
        called = []

        def fetch(url):
            called.append(url)
            return verification.fetch_public_url(url)

        result = verification.verify_task(st, task, fetcher=fetch)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(called, ["ftp://example.org/a"])
        first, second = self.ledger_rows()
        self.assertEqual(first["failure_kind"], "invalid_url")
        self.assertEqual(second["failure_kind"], "invalid_url")
        self.assertEqual(second["basis"], "fetch_failed:invalid_url; 来源记录缺少 URL")

    def test_shared_source_is_rebound_to_requesting_task_query(self):
        st = self.fresh_state()
        sid = "RS-shared"
        url = "https://example.org/shared"
        source = {"id": sid, "task_id": "RT-A", "query_id": "RQ-A",
                  "url": url, "title": "共享来源", "verification": "unverified"}
        task_a = {
            "id": "RT-A", "kind": "prediction_miss",
            "question": "A", "target": {"type": "relation",
                                        "concepts": ["加法", "能量"]},
            "status": "materials_found", "verification": "pending_verification",
            "query_ids": ["RQ-A"], "source_ids": [sid], "sources": [source],
        }
        task_b = {
            "id": "RT-B", "kind": "relation_unverified",
            "question": "B", "target": {"type": "relation",
                                        "concepts": ["加法", "能量"]},
            "status": "materials_found", "verification": "pending_verification",
            "query_ids": ["RQ-B"], "source_ids": [sid], "sources": [source],
        }
        st["research"]["tasks"] = {"RT-A": task_a, "RT-B": task_b}
        st["research"]["queries"] = {
            "RQ-A": {"id": "RQ-A", "task_id": "RT-A", "sources": [sid]},
            "RQ-B": {"id": "RQ-B", "task_id": "RT-B", "sources": [sid]},
        }
        st["research"]["sources"] = [source]
        before = json.dumps(st, ensure_ascii=False, sort_keys=True)
        verification.verify_task(
            st, task_b,
            fetcher=self.fetcher({url: SUPPORT_HTML}))
        row = self.ledger_rows()[0]
        self.assertEqual(row["task_id"], "RT-B")
        self.assertEqual(row["query_id"], "RQ-B")
        self.assertEqual(row["source_id"], sid)
        self.assert_no_promotion(st, before)

    def test_verify_pending_consumes_current_and_archived_materials(self):
        st = self.fresh_state()
        current = self.add_task(st, ["https://a.example.org/current"])
        archived = self.add_task(
            st, ["https://b.example.net/archived"],
            task_id="RT-phase3-archived", query_id="RQ-phase3-archived")
        st["research"]["tasks"].pop(archived["id"])
        st["research"]["archived_tasks"].append(archived)
        before = json.dumps(st, ensure_ascii=False, sort_keys=True)
        result = verification.verify_pending(
            st, limit_tasks=5,
            fetcher=self.fetcher({
                "https://a.example.org/current": SUPPORT_HTML,
                "https://b.example.net/archived": SUPPORT_HTML_2,
            }))
        self.assertEqual(len(result["tasks"]), 2)
        self.assertEqual(result["records_appended"], 2)
        task_ids = {row["task_id"] for row in self.ledger_rows()}
        self.assertEqual(task_ids, {current["id"], archived["id"]})
        archived_row = next(row for row in self.ledger_rows()
                            if row["task_id"] == archived["id"])
        self.assertEqual(archived_row["query_id"], "RQ-phase3-archived")
        self.assertEqual(archived_row["source_id"], archived["source_ids"][0])
        self.assert_no_promotion(st, before)

    def test_partial_verifier_exception_leaves_other_records_intact(self):
        st = self.fresh_state()
        task = self.add_task(st, [
            "https://a.example.org/ok",
            "https://b.example.net/bad",
        ])
        real_extract = verification.extract_document

        def extract(fetch_result):
            if fetch_result.get("final_url") == "https://b.example.net/bad":
                raise RuntimeError("parser exploded")
            return real_extract(fetch_result)

        before = json.dumps(st, ensure_ascii=False, sort_keys=True)
        with mock.patch.object(verification, "extract_document", side_effect=extract):
            result = verification.verify_task(
                st, task,
                fetcher=self.fetcher({
                    "https://a.example.org/ok": SUPPORT_HTML,
                    "https://b.example.net/bad": SUPPORT_HTML_2,
                }))
        self.assertEqual(result["appended"], 1)
        self.assertEqual(result["failures"], 1)
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["failure_kind"], None)
        self.assertEqual(rows[0]["judgment"], "support")
        self.assertEqual(rows[1]["failure_kind"], "verifier_exception")
        self.assertEqual(rows[1]["promotion"], "none")
        self.assert_no_promotion(st, before)


class JudgmentTests(VerificationSandbox):
    def test_contradictory_evidence_is_candidate_conflict(self):
        st = self.fresh_state()
        task = self.add_task(st, [
            "https://a.example.org/support",
            "https://b.example.net/counter",
        ])
        result = verification.verify_task(
            st, task,
            fetcher=self.fetcher({
                "https://a.example.org/support": SUPPORT_HTML,
                "https://b.example.net/counter": CONTRADICT_HTML,
            }))
        self.assertEqual(result["appended"], 2)
        rows = self.ledger_rows()
        by_url = {row["url"]: row for row in rows}
        self.assertEqual(by_url["https://a.example.org/support"]["judgment"],
                         "support")
        self.assertEqual(by_url["https://b.example.net/counter"]["judgment"],
                         "contradict")
        self.assertTrue(by_url["https://a.example.org/support"]["evidence_counted"])
        self.assertTrue(by_url["https://b.example.net/counter"]["evidence_counted"])
        summary = verification.summarize_task(rows, task["id"])
        self.assertEqual(summary["conclusion"], "candidate_conflict")
        self.assertEqual(summary["support_groups"], 1)
        self.assertEqual(summary["contradict_groups"], 1)
        self.assertEqual(summary["promotion"], "none")

    def test_uncertain_when_terms_lack_directional_cue(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        logic = verification.judge_text(
            {"target": {"concepts": ["加法", "能量"]}}, "这一页同时提到了加法与能量。")
        self.assertEqual(logic["judgment"], "uncertain")
        self.assertIn(logic["reason"], ("no_directional_cue", "terms_not_co_occurring"))
        verification.verify_task(
            st, task, fetcher=self.fetcher({"https://example.org/a": UNCERTAIN_HTML}))
        row = self.ledger_rows()[0]
        self.assertEqual(row["judgment"], "uncertain")
        self.assertFalse(row["evidence_counted"])
        summary = verification.summarize_task(self.ledger_rows(), task["id"])
        self.assertEqual(summary["conclusion"], "candidate_uncertain")


class SourceIndependenceTests(VerificationSandbox):
    def test_same_registrable_domain_counts_once(self):
        st = self.fresh_state()
        task = self.add_task(st, [
            "https://a.example.org/one",
            "https://b.example.org/two",
        ])
        verification.verify_task(
            st, task,
            fetcher=self.fetcher({
                "https://a.example.org/one": SUPPORT_HTML,
                "https://b.example.org/two": SUPPORT_HTML_2,
            }))
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["independence_group"], "domain:example.org")
        self.assertEqual(second["independence_group"], "domain:example.org")
        self.assertTrue(first["evidence_counted"])
        self.assertFalse(second["evidence_counted"])
        self.assertEqual(second["independent_reason"], "same_source_group")
        summary = verification.summarize_task(rows, task["id"])
        self.assertEqual(summary["support_groups"], 1)

    def test_identical_body_across_domains_is_one_source(self):
        st = self.fresh_state()
        task = self.add_task(st, [
            "https://one.example.org/a",
            "https://two.example.net/b",
        ])
        verification.verify_task(
            st, task,
            fetcher=self.fetcher({
                "https://one.example.org/a": SUPPORT_HTML,
                "https://two.example.net/b": SUPPORT_HTML,
            }))
        first, second = self.ledger_rows()
        self.assertTrue(first["evidence_counted"])
        self.assertFalse(second["evidence_counted"])
        self.assertEqual(second["duplicate_of"], first["verification_id"])
        self.assertEqual(second["independent_reason"], "duplicate_content")
        summary = verification.summarize_task(self.ledger_rows(), task["id"])
        self.assertEqual(summary["support_groups"], 1)
        self.assertEqual(summary["duplicate_content"], 1)

    def test_redirect_landing_domain_determines_independence(self):
        st = self.fresh_state()
        task = self.add_task(st, [
            "https://a.example.org/start",
            "https://b.example.net/other",
        ])

        def fetch(url):
            if url == "https://a.example.org/start":
                result = ok_result(url, SUPPORT_HTML.encode())
                result["final_url"] = "https://b.example.net/final"
                result["redirects"] = [{
                    "from": url, "to": result["final_url"], "http_status": 301,
                }]
                return result
            return ok_result(url, SUPPORT_HTML_2.encode())

        verification.verify_task(st, task, fetcher=fetch)
        first, second = self.ledger_rows()
        self.assertEqual(first["independence_group"], "domain:example.net")
        self.assertEqual(second["independence_group"], "domain:example.net")
        self.assertTrue(first["evidence_counted"])
        self.assertFalse(second["evidence_counted"])
        self.assertEqual(second["independent_reason"], "same_source_group")

    def test_unparseable_host_is_not_independent(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        task["source_ids"] = [task["sources"][0]["id"]]
        task["sources"][0]["url"] = "not a url"
        result = verification.verify_task(
            st, task,
            fetcher=lambda _url: ok_result("not a url", SUPPORT_HTML.encode()))
        self.assertEqual(result["failures"], 0)
        row = self.ledger_rows()[0]
        self.assertEqual(row["independence_group"], "")
        self.assertFalse(row["evidence_counted"])
        self.assertEqual(row["independent_reason"], "unknown_source_group")


class ExtractionTests(VerificationSandbox):
    def test_unsupported_content_type_is_explicit_uncertain(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        verification.verify_task(
            st, task,
            fetcher=self.fetcher({
                "https://example.org/a": (b"%PDF-1.7", "application/pdf"),
            }))
        row = self.ledger_rows()[0]
        self.assertEqual(row["extraction_status"], "unsupported_content_type")
        self.assertEqual(row["judgment"], "uncertain")
        self.assertEqual(row["judgment_reason"],
                         "extraction_unsupported_content_type")
        self.assertFalse(row["evidence_counted"])
        self.assertEqual(row["evidence_excerpt"], "")
        summary = verification.summarize_task(self.ledger_rows(), task["id"])
        self.assertEqual(summary["extraction_failures"], 1)
        self.assertEqual(summary["conclusion"], "candidate_uncertain")

    def test_empty_html_body_is_explicit_uncertain(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        verification.verify_task(
            st, task, fetcher=self.fetcher({"https://example.org/a": EMPTY_HTML}))
        row = self.ledger_rows()[0]
        self.assertEqual(row["extraction_status"], "empty")
        self.assertEqual(row["judgment"], "uncertain")
        self.assertIn("text_too_short", row["extraction_detail"])
        self.assertFalse(row["evidence_counted"])

    def test_unsupported_content_encoding_is_explicit(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        verification.verify_task(
            st, task,
            fetcher=self.fetcher({
                "https://example.org/a": (
                    b"\x1f\x8b\x08\x00", "text/html", "gzip"),
            }))
        row = self.ledger_rows()[0]
        self.assertEqual(row["extraction_status"], "unsupported_content_encoding")
        self.assertEqual(row["judgment"], "uncertain")
        self.assertFalse(row["evidence_counted"])


class FetchSafetyTests(VerificationSandbox):
    def test_private_and_malformed_urls_are_rejected_without_network(self):
        cases = [
            ("http://127.0.0.1/", "non_public_address"),
            ("http://10.0.0.5/secret", "non_public_address"),
            ("http://[::1]/", "non_public_address"),
            ("notaurl", "invalid_url"),
            ("ftp://example.org/a", "invalid_url"),
            ("http://user:pass@example.org/a", "invalid_url"),
            ("http://example.org:8080/a", "invalid_url"),
            ("http://exa mple.org/a", "invalid_url"),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                result = verification.fetch_public_url(url)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["failure_kind"], expected)

    def test_localhost_is_rejected_before_dns(self):
        called = []

        def resolver(host, port, type=None):
            called.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("127.0.0.1", port))]

        result = verification.fetch_public_url("http://localhost/", resolver=resolver)
        self.assertEqual(result["failure_kind"], "invalid_url")
        self.assertEqual(called, [])

    def test_redirect_target_is_revalidated(self):
        calls = []

        def resolver(host, port, type=None):
            if host == "public.example.org":
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                         ("93.184.216.34", 443))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("10.0.0.5", 80))]

        def http_get(info, ip, timeout, deadline):
            calls.append((info["url"], ip))
            return {"status": "redirect", "http_status": 302,
                    "location": "http://internal.example.org/secret",
                    "url": info["url"]}

        result = verification.fetch_public_url(
            "https://public.example.org/start",
            resolver=resolver, http_get=http_get)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "unsafe_redirect")
        self.assertEqual(result["redirect_rejection"], "non_public_address")
        self.assertEqual(len(result["redirects"]), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "93.184.216.34")

    def test_public_redirect_is_followed_and_recorded(self):
        def resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34", 443))]

        def http_get(info, ip, timeout, deadline):
            if info["host"] == "a.example.org":
                return {"status": "redirect", "http_status": 301,
                        "location": "https://b.example.net/final",
                        "url": info["url"]}
            return ok_result(info["url"], SUPPORT_HTML.encode())

        result = verification.fetch_public_url(
            "https://a.example.org/start", resolver=resolver, http_get=http_get)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["final_url"], "https://b.example.net/final")
        self.assertEqual(len(result["redirects"]), 1)
        self.assertEqual(result["redirects"][0]["http_status"], 301)

    def test_network_failure_and_timeout_are_explicit(self):
        def resolver(host, port, type=None):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     ("93.184.216.34", 443))]

        def refuse(info, ip, timeout, deadline):
            raise OSError("connection refused")

        result = verification.fetch_public_url(
            "https://example.org/a", resolver=resolver, http_get=refuse)
        self.assertEqual(result["failure_kind"], "network_error")

        def slow(info, ip, timeout, deadline):
            raise TimeoutError("timed out")

        result = verification.fetch_public_url(
            "https://example.org/a", resolver=resolver, http_get=slow)
        self.assertEqual(result["failure_kind"], "timeout")

    def test_fetcher_exception_is_recorded_as_failed_source(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        result = verification.verify_task(
            st, task, fetcher=lambda _url: (_ for _ in ()).throw(OSError("boom")))
        self.assertEqual(result["failures"], 1)
        row = self.ledger_rows()[0]
        self.assertEqual(row["failure_kind"], "fetcher_exception")
        self.assertEqual(row["judgment"], "uncertain")
        self.assertFalse(row["evidence_counted"])

    def test_body_size_cap_marks_truncated(self):
        class Response:
            def __init__(self, body):
                self.body = body
                self.pos = 0

            def read(self, size):
                chunk = self.body[self.pos:self.pos + size]
                self.pos += len(chunk)
                return chunk

        data, truncated = verification._read_limited(
            Response(b"x" * 100), 10, time.monotonic() + 5)
        self.assertEqual(data, b"x" * 10)
        self.assertTrue(truncated)

    def test_read_deadline_raises(self):
        class Response:
            def read(self, size):
                return b"x"

        with self.assertRaises(TimeoutError):
            verification._read_limited(Response(), 10, time.monotonic() - 1)


class VerifierSafetyTests(VerificationSandbox):
    def test_verifier_exception_does_not_promote_or_mutate_state(self):
        st = self.fresh_state()
        task = self.add_task(st, ["https://example.org/a"])
        before = json.dumps(st, ensure_ascii=False, sort_keys=True)
        with mock.patch.object(verification, "extract_document",
                               side_effect=RuntimeError("parser exploded")):
            result = verification.verify_task(
                st, task, fetcher=self.fetcher({
                    "https://example.org/a": SUPPORT_HTML}))
        self.assertEqual(result["failures"], 1)
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["failure_kind"], "verifier_exception")
        self.assertEqual(rows[0]["extraction_status"], "verifier_exception")
        self.assertEqual(rows[0]["judgment"], "uncertain")
        self.assertEqual(rows[0]["promotion"], "none")
        self.assert_no_promotion(st, before)

    def test_state_snapshot_and_config_are_self_describing(self):
        snapshot = verification.config_snapshot()
        self.assertEqual(snapshot["version"], verification.CONFIG_VERSION)
        self.assertEqual(verification.CONFIG_SHA256,
                         verification._canonical_hash(snapshot))
        self.assertEqual(verification.VERIFIER_VERSION, "phase3-verifier-v1")
        self.assertIn("max_body_bytes", snapshot)
        self.assertIn("support_strong_cues", snapshot)
        self.assertIn("contradict_cues", snapshot)


if __name__ == "__main__":
    unittest.main()
