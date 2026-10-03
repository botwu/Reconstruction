"""新入口的输入绑定、候选版本和失败清理；模型效果由真实运行验证。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from traceforge.reconstruction.researcher import ReconstructionRuntime, author_feedback, check_candidate, snapshot_candidate
from traceforge.reconstruction.agents import AgentSession, COMPLETION_REPLAYED_ROLE, SUFFICIENCY_ROLE
from traceforge.reconstruction.agents.runtime import AgentResult
from traceforge.reconstruction.agents.session import execute_tool
from traceforge.reconstruction.pipeline import ReconstructionError, run_prepared_task


class ReconstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_independent_reviewer_can_inspect_original_task_time_context(self) -> None:
        source = {"raw_session": {"harness": "真实协议", "messages": [
            {"role": "system", "content": "工具定义与执行约束"},
            {"role": "user", "content": "原始任务"},
            {"role": "assistant", "content": "后续假设", "reasoning_content": "假设依据"},
            {"role": "tool", "tool_call_id": "later-call", "content": "后续成功原始返回"},
        ]}}
        native = Mock(model_name="test", backend="test")
        native.run.return_value = AgentResult(role="sufficiency", backend="test", completed=True,
                                             payload={"label": "UNKNOWN"})
        adapter = ReconstructionRuntime(native, source=source, task={}, runtime_factory=Mock())
        adapter.agent = native
        session = AgentSession(allow_write=False)
        adapter.run(role=SUFFICIENCY_ROLE, instruction="核对初态", session=session,
                    output_root=self.root / "review")
        self.assertEqual(json.loads(session.session_context), source["raw_session"])
        role = native.run.call_args.kwargs["role"]
        self.assertIn("read_session_message", role.tools)
        self.assertFalse(role.allow_write)
        instruction = native.run.call_args.kwargs["instruction"]
        self.assertIn("task_start_message_index", instruction)
        from traceforge.reconstruction.session_source import indexed_session
        source_lines = [line for line in instruction.splitlines()
                        if line.startswith("SOURCE_SESSION=")]
        self.assertEqual(len(source_lines), 1)
        self.assertEqual(json.loads(source_lines[0].removeprefix("SOURCE_SESSION=")),
                         indexed_session(source["raw_session"]))
        author_session = AgentSession()
        adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="恢复任务", session=author_session,
                    output_root=self.root / "author")
        # 作者也须携带权威原文，压缩保护不能只拿独立 reviewer 的上下文。
        self.assertEqual(json.loads(author_session.session_context), source["raw_session"])
        self.assertIs(author_session.conversation, adapter.conversation)


    def test_actual_author_index_exposes_uncertain_observation_without_promoting_it(self) -> None:
        native = Mock(model_name="test", backend="test")
        native.run.return_value = AgentResult(
            role="completion", backend="test", completed=True, payload={})
        adapter = ReconstructionRuntime(native, source={}, task={}, runtime_factory=Mock())
        adapter.agent = native
        state = AgentSession(evidence=[{
            "evidence_ref_id": "later", "initial_state_eligible": False,
            "session_parse": {"file_ops": [], "reference_file_ops": [{
                "kind": "read", "path": "library/source.py", "source_path": "/original/source.py",
                "line_numbers": [42], "line_contents": ["original body"],
                "initial_state_blockers": [{"reason": "read_after_unparsed_mutation",
                                            "source_event_id": "later",
                                            "path": "library/source.py"}],
            }]},
        }])
        adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="", session=state,
                    output_root=self.root / "author")
        instruction = native.run.call_args.kwargs["instruction"]
        self.assertIn('"initial_state_eligible": false', instruction)
        self.assertIn('"path": "library/source.py"', instruction)
        self.assertIn('"source_path": "/original/source.py"', instruction)
        self.assertIn('"reason": "read_after_unparsed_mutation"', instruction)
        self.assertIn("不能直接恢复或自动覆盖原文件", instruction)

    def test_probe_history_is_readable_but_not_a_current_candidate_check(self) -> None:
        probe = {"probe_id": "old", "executions": [{"stdout": "完整旧输出"}]}
        adapter = ReconstructionRuntime(Mock(model_name="test"), source={}, task={},
                                        runtime_factory=Mock(), python_runtime=self.root / "locked",
                                        initial_feedback={"environment_probes": [probe]})
        adapter.agent = Mock()
        adapter.agent.run.return_value = AgentResult(role="completion", backend="test",
                                                     completed=True, payload={})
        state = AgentSession()
        adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="", session=state,
                    output_root=self.root / "author")
        self.assertEqual(state.environment_probes, [])
        self.assertEqual(execute_tool("read_probe_output", {"probe_id": "old"}, state), "完整旧输出")
        self.assertEqual(state.dependency_bundle, self.root / "locked")
        role = adapter.agent.run.call_args.kwargs["role"]
        self.assertIn("read_probe_output", role.tools)
        self.assertIn("restore_dependency_source", role.tools)

    def test_snapshot_checks_latest_write_without_mutating_seed(self) -> None:
        session = AgentSession(replay_files={"src/a.py": "seed"}, writes=[
            {"path": "src/a.py", "content": "first"},
            {"path": "src/a.py", "content": "latest"},
        ])
        destination = self.root / "candidate"
        hashes = snapshot_candidate(session, destination)
        self.assertEqual((destination / "src/a.py").read_text(), "latest")
        self.assertEqual(hashes["src/a.py"], hashlib.sha256(b"latest").hexdigest())
        self.assertEqual(session.replay_files["src/a.py"], "seed")

    def test_snapshot_rejects_escape_and_symlink(self) -> None:
        with self.assertRaises(ValueError):
            snapshot_candidate(AgentSession(replay_files={"../bad": "x"}), self.root / "bad")
        workspace = self.root / "seed"
        workspace.mkdir()
        (workspace / "link").symlink_to(self.root / "outside")
        with self.assertRaises(ValueError):
            snapshot_candidate(AgentSession(workspace=workspace), self.root / "linked")

    def run_check(self, *, prepare_error: Exception | None = None,
                  cleanup_error: Exception | None = None) -> tuple[dict, Mock]:
        runtime = Mock(stop=AsyncMock(side_effect=cleanup_error))
        session = AgentSession(replay_files={"a.py": "x=1"})
        with patch("traceforge.reconstruction.researcher.prepare_role_sandbox",
                   new=AsyncMock(side_effect=prepare_error)), patch(
            "traceforge.reconstruction.researcher.run_environment_probe",
            return_value={"status": "PASS", "executions": [{"exit_code": 0, "stdout": "ok"}]},
        ):
            result = check_candidate(
                session=session,
                runtime_factory=lambda: runtime, output_root=self.root / "checks",
                python_runtime=None, python_code="assert True", purpose="load", timeout_seconds=5,
            )
        runtime.stop.assert_awaited_once_with(delete=True)
        self.assertEqual(session.environment_probes, [json.loads(Path(result["record_path"]).read_text())])
        return result, runtime

    def test_check_persists_version_and_real_result(self) -> None:
        result, _ = self.run_check()
        stored = json.loads(Path(result["record_path"]).read_text())
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["cleanup_verified"])
        self.assertEqual(result["candidate_sha256"], stored["candidate_sha256"])
        self.assertIn("a.py", stored["candidate_files"])

    def test_prepare_failure_still_cleans_up_and_is_not_pass(self) -> None:
        result, _ = self.run_check(prepare_error=RuntimeError("dependency failure"))
        self.assertEqual(result["status"], "INFRA_ERROR")
        self.assertIn("dependency failure", result["error"])

    def test_cleanup_failure_invalidates_pass(self) -> None:
        result, _ = self.run_check(cleanup_error=RuntimeError("still alive"))
        self.assertEqual(result["status"], "INFRA_ERROR")
        self.assertFalse(result["cleanup_verified"])

    def test_missing_execution_backend_is_explicit(self) -> None:
        self.assertIn("没有配置", execute_tool("run_candidate", {}, AgentSession()))

    def test_completion_can_recover_public_sources_and_retains_fetch_failure(self) -> None:
        native = Mock(model_name="test")
        adapter = ReconstructionRuntime(native, source={}, task={}, runtime_factory=Mock())
        adapter.agent = native
        session = AgentSession()
        with patch("traceforge.reconstruction.researcher.SearchTools") as search:
            network = search.return_value
            network.search.return_value = {"success": True, "results": [{"link": "https://example.org/code"}]}
            network.open.return_value = {"success": False, "error": "HTTP 404"}

            def complete(**kwargs):
                self.assertIn("web_search", kwargs["role"].tools)
                self.assertIn("web_open", kwargs["role"].tools)
                found = json.loads(execute_tool("web_search", {"query": "public-package 1.0"}, session))
                self.assertTrue(found["success"])
                fetched = json.loads(execute_tool("web_open", {"url": "https://example.org/code"}, session))
                self.assertFalse(fetched["success"])
                self.assertEqual(fetched["error"], "HTTP 404")
                return AgentResult(role="completion", backend="test", completed=True, payload={})

            native.run.side_effect = complete
            adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="", session=session,
                        output_root=self.root / "completion")
            network.search.assert_called_once_with("public-package 1.0")
            network.open.assert_called_once()

    def test_template_keeps_feedback_and_exposes_execution_tool(self) -> None:
        source = {"session_parser": {"system_context": [{"meaning": "历史工具协议"}]}}
        adapter = ReconstructionRuntime(Mock(model_name="test"), source=source,
                                        task={"task_id": "t"}, runtime_factory=Mock(),
                                        initial_feedback={"failure": "上一轮拒写位置"})
        adapter.agent = Mock()
        adapter.agent.run.return_value = AgentResult(role="completion", backend="test", payload={},
                                                     completed=True)
        session = AgentSession(session_context='{"messages":[]}',
                               repair_feedback={"failure": "具体导入错误"},
                               prior_capture_repairs={"a.py": [{"old_text": "large old body",
                                                               "new_text": "large new body",
                                                               "reason": "修复采集损坏"}]})
        adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="old", session=session,
                    output_root=self.root / "author")
        args = adapter.agent.run.call_args.kwargs
        self.assertIn("run_candidate", args["role"].tools)
        self.assertIn("restore_observed_file", args["role"].tools)
        self.assertIn("edit_candidate_file", args["role"].tools)
        self.assertIs(session.conversation, adapter.conversation)
        self.assertIn("具体导入错误", args["instruction"])
        self.assertNotIn("上一轮拒写位置", args["instruction"])
        self.assertIn("历史工具协议", args["instruction"])
        self.assertIn("修复采集损坏", args["instruction"])
        self.assertNotIn("large old body", args["instruction"])
        self.assertNotIn("large new body", args["instruction"])
        self.assertEqual(session.prior_capture_repairs["a.py"][0]["old_text"], "large old body")
        self.assertEqual(session.session_context, '{"messages":[]}')
        session.repair_feedback = None
        adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="old", session=session,
                    output_root=self.root / "retry")
        self.assertIn("上一轮拒写位置", adapter.agent.run.call_args.kwargs["instruction"])

    def test_repair_ready_requires_fresh_execution(self) -> None:
        for execute_check in (False, True):
            with self.subTest(execute_check=execute_check):
                adapter = ReconstructionRuntime(Mock(model_name="test"), source={}, task={},
                                                runtime_factory=Mock())
                adapter.agent = Mock()
                session = AgentSession(repair_feedback={"missing_context": ["broken import"]})
                calls = []

                def run(**kwargs):
                    calls.append(kwargs["instruction"])
                    if len(calls) == 2 and execute_check:
                        session.environment_probes.append({"status": "PASS"})
                    return AgentResult(role="completion", backend="test", completed=True,
                                       payload={"candidates": [{"decision": "READY"}]})

                adapter.agent.run.side_effect = run
                result = adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="old", session=session,
                                     output_root=self.root / str(execute_check))
                self.assertEqual(len(calls), 2)
                self.assertEqual("AUTHOR_NO_PROGRESS" in result.errors, not execute_check)

    def test_author_feedback_keeps_full_error_without_duplicate_snapshots(self) -> None:
        probe = {"python_code": "import package", "status": "FAIL",
                 "runtime_stdout": "duplicate serialized receipt", "workspace_before": {"x": "hash"},
                 "executions": [{"stdout": "prefix" + "x" * 10000, "stderr": "actual import error",
                                 "workspace_after": {"x": "hash"}, "exit_code": 1}]}
        feedback = {"environment_probes": [probe], "failed_probes": [probe]}
        compact = author_feedback(feedback)
        self.assertEqual(compact["environment_probes"][0]["executions"][0]["stderr"], "actual import error")
        self.assertEqual(compact["environment_probes"][0]["executions"][0]["stdout"],
                         probe["executions"][0]["stdout"])
        self.assertNotIn("runtime_stdout", compact["environment_probes"][0])
        self.assertIn("runtime_stdout", feedback["environment_probes"][0])

    def test_prepared_entry_routes_search_without_terminal_execution(self) -> None:
        source = {"session_parser": {"status": "READY"}, "tasks": [{"task_id": "t"}],
                  "domain_route": "retrieval"}
        from traceforge.reconstruction.verification import VerificationConfig

        config = VerificationConfig(
            harbor_root=self.root, model_name="author", rollout_model="anthropic/solver",
            execute_rollout=True,
        )
        with patch("traceforge.reconstruction.search_environment.run_search_task",
                   return_value={"status": "ROLLOUT_COMPLETED"}) as search:
            result = run_prepared_task(source=source, task=source["tasks"][0],
                                       agent=Mock(), output_root=self.root, verification_config=config)
        search.assert_called_once()
        self.assertIs(search.call_args.kwargs["verification_config"], config)
        self.assertEqual(result["status"], "ROLLOUT_COMPLETED")
        source["session_parser"]["status"] = "ERROR"
        with self.assertRaisesRegex(ReconstructionError, "Session Parser"):
            run_prepared_task(source=source, task=source["tasks"][0], agent=Mock(), output_root=self.root)

    def test_environment_review_does_not_inherit_author_assumptions(self) -> None:
        adapter = ReconstructionRuntime(Mock(model_name="test"), source={}, task={}, runtime_factory=Mock())
        adapter.conversation.messages = [{"role": "assistant", "content": "已关闭备注功能但我认为足够"}]
        adapter.agent = Mock()
        adapter.agent.run.return_value = AgentResult(role="sufficiency", backend="test", payload={}, completed=True)
        session = AgentSession()
        (self.root / "review").mkdir()
        adapter.run(role=SUFFICIENCY_ROLE, instruction="检查当前环境", session=session,
                    output_root=self.root / "review")
        self.assertIsNot(session.conversation, adapter.conversation)
        self.assertEqual(session.conversation.messages, [])
        self.assertEqual(adapter.agent.run.call_args.kwargs["role"].identity, SUFFICIENCY_ROLE.identity)
        self.assertFalse(adapter.phases[-1]["authoring_session"])
        self.assertEqual(len(adapter.conversation.messages), 1)

    def test_original_behavior_failure_cannot_be_waived_by_reviewer(self) -> None:
        for status in ("FAIL", "PASS"):
            with self.subTest(status=status):
                probe = {"code": "assert original_entry() == expected", "purpose": "load",
                         "timeout_seconds": 60}
                adapter = ReconstructionRuntime(Mock(model_name="test"), source={}, task={},
                                                runtime_factory=Mock(),
                                                initial_feedback={"primary_behavior_probe": probe})
                adapter.agent = Mock()
                adapter.agent.run.return_value = AgentResult(
                    role="sufficiency", backend="test", completed=True,
                    payload={"label": "SUFFICIENT", "decision": "READY"},
                )
                session = AgentSession(environment_probes=[
                    {"purpose": purpose, "status": "PASS"} for purpose in ("load", "dependency", "reset")
                ])
                output = self.root / status
                output.mkdir()
                with patch("traceforge.reconstruction.researcher.check_candidate", return_value={
                    "status": status, "executions": [{"stderr": "实际入口失败" if status == "FAIL" else ""}],
                    "record_path": str(output / "checks/check-0001/result.json"),
                }) as check:
                    result = adapter.run(role=SUFFICIENCY_ROLE, instruction="独立检查当前候选",
                                         session=session, output_root=output)
                check.assert_called_once()
                self.assertEqual(check.call_args.kwargs["python_code"], probe["code"])
                self.assertIn(status, adapter.agent.run.call_args.kwargs["instruction"])
                self.assertEqual(result.errors, ["INITIAL_BEHAVIOR_PROBE_FAILED"] if status == "FAIL" else [])

    def test_missing_review_probes_stay_with_the_independent_reviewer(self) -> None:
        for case in ("complete", "failed_load", "no_progress", "source_gap", "infra"):
            with self.subTest(case=case):
                adapter = ReconstructionRuntime(Mock(model_name="test"), source={}, task={},
                                                runtime_factory=Mock())
                adapter.conversation.messages = [{"role": "assistant", "content": "作者旧判断"}]
                adapter.agent = Mock()
                session = AgentSession()

                def reviewer(**kwargs):
                    first = adapter.agent.run.call_count == 1
                    if first:
                        self.assertIn("SOURCE_SESSION=", kwargs["instruction"])
                        session.environment_probes.append({"purpose": "load", "status": "FAIL" if case == "failed_load" else "PASS"})
                        if session.conversation is not None:
                            session.conversation.messages.append({"role": "assistant", "content": "已读取并加载源码"})
                    else:
                        self.assertNotIn("SOURCE_SESSION=", kwargs["instruction"])
                        self.assertEqual(session.conversation.messages[0]["content"], "已读取并加载源码")
                        self.assertIn("dependency", kwargs["instruction"])
                        self.assertIn("reset", kwargs["instruction"])
                        if case in {"complete", "failed_load"}:
                            session.environment_probes.extend([
                                {"purpose": purpose, "status": "PASS"} for purpose in ("load", "dependency", "reset")
                            ])
                        if case == "failed_load":
                            self.assertIn("load", kwargs["instruction"])
                    return AgentResult(
                        role="sufficiency", backend="test", completed=True,
                        payload={"label": "INSUFFICIENT" if case == "source_gap" else "SUFFICIENT",
                                 "decision": "REVIEW" if first else "READY"},
                        errors=["MODEL_API_FAILED"] if case == "infra" else [],
                    )

                adapter.agent.run.side_effect = reviewer
                output = self.root / case
                output.mkdir()
                result = adapter.run(role=SUFFICIENCY_ROLE, instruction="独立检查当前候选",
                                     session=session, output_root=output)
                self.assertEqual(adapter.agent.run.call_count, 1 if case in {"source_gap", "infra"} else 2)
                self.assertEqual(adapter.conversation.messages, [{"role": "assistant", "content": "作者旧判断"}])
                if case in {"complete", "failed_load"}:
                    self.assertEqual(result.errors, [])
                    self.assertEqual(result.payload["decision"], "READY")
                elif case == "no_progress":
                    self.assertIn("REVIEWER_NO_PROGRESS", result.errors)

    def test_author_continues_real_failure_with_current_files_and_history(self) -> None:
        adapter = ReconstructionRuntime(
            Mock(model_name="test"), source={"session_parser": {"system_context": []}},
            task={}, runtime_factory=Mock(),
        )
        session = AgentSession(replay_files={"a.py": "original"})
        adapter.agent = Mock()

        def author(**kwargs):
            self.assertIs(kwargs["session"], session)
            if adapter.agent.run.call_count == 1:
                session.writes.append({"path": "a.py", "content": "partially repaired"})
                session.environment_probes.append({"status": "FAIL", "candidate_sha256": "v1",
                                                   "cleanup_verified": True})
                adapter.conversation.messages = [{"role": "assistant", "content": "实际失败"}]
                return AgentResult(role="completion", backend="test", completed=True,
                                   payload={"candidates": [{"decision": "REVIEW"}], "open_questions": []})
            self.assertEqual(session.replay_files["a.py"], "partially repaired")
            self.assertEqual(adapter.conversation.messages[0]["content"], "实际失败")
            self.assertIn("FAIL", kwargs["instruction"])
            return AgentResult(role="completion", backend="test", completed=True,
                               payload={"candidates": [{"decision": "READY"}], "open_questions": []})

        adapter.agent.run.side_effect = author
        result = adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="恢复初态", session=session,
                             output_root=self.root / "continued")
        self.assertEqual(adapter.agent.run.call_count, 2)
        self.assertEqual(result.payload["candidates"][0]["decision"], "READY")
        self.assertEqual(len(adapter.phases), 2)
        saved = json.loads((self.root / "continued/private/conversation.json").read_text())
        self.assertEqual(saved, adapter.conversation.messages)

    def test_author_stops_on_missing_facts_infrastructure_or_repeated_candidate(self) -> None:
        for case, expected_calls in (("missing", 1), ("infra", 1), ("repeat", 2), ("unchecked", 2)):
            with self.subTest(case=case):
                adapter = ReconstructionRuntime(
                    Mock(model_name="test"), source={"session_parser": {"system_context": []}},
                    task={}, runtime_factory=Mock(),
                )
                session = AgentSession()
                adapter.agent = Mock()

                def author(**kwargs):
                    if case != "unchecked" or adapter.agent.run.call_count == 1:
                        session.environment_probes.append({
                            "status": "INFRA_ERROR" if case == "infra" else "FAIL",
                            "candidate_sha256": "unchanged", "cleanup_verified": case != "infra",
                        })
                    return AgentResult(role="completion", backend="test", completed=True,
                                       payload={"candidates": [{"decision": "REVIEW"}],
                                                "open_questions": ["缺少原始输入"] if case == "missing" else []})

                adapter.agent.run.side_effect = author
                result = adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="恢复初态", session=session,
                                     output_root=self.root / case)
                self.assertEqual(adapter.agent.run.call_count, expected_calls)
                self.assertEqual(result.payload["candidates"][0]["decision"], "REVIEW")
                if case in {"repeat", "unchecked"}:
                    self.assertIn("AUTHOR_NO_PROGRESS", result.errors)

    def test_author_ready_must_pass_original_behavior_before_handoff(self) -> None:
        for statuses, expected_errors in ((["FAIL", "PASS"], []),
                                          (["INFRA_ERROR"], ["INITIAL_BEHAVIOR_PROBE_FAILED"])):
            with self.subTest(statuses=statuses):
                adapter = ReconstructionRuntime(
                    Mock(model_name="test"), source={"session_parser": {"system_context": []}},
                    task={}, runtime_factory=Mock(), initial_feedback={"primary_behavior_probe": {
                        "code": "assert original_entry() == expected", "purpose": "load", "timeout_seconds": 60,
                    }},
                )
                adapter.agent = Mock()
                adapter.agent.run.return_value = AgentResult(
                    role="completion", backend="test", completed=True,
                    payload={"candidates": [{"decision": "READY"}], "open_questions": []},
                )
                session = AgentSession()

                def check(**kwargs):
                    status = statuses[len(session.environment_probes)]
                    result = {"status": status, "candidate_sha256": "candidate",
                              "cleanup_verified": status != "INFRA_ERROR",
                              "record_path": str(self.root / "checks/check-0001/result.json")}
                    session.environment_probes.append(result)
                    return result

                with patch("traceforge.reconstruction.researcher.check_candidate", side_effect=check):
                    result = adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="恢复初态", session=session,
                                         output_root=self.root / statuses[0])
                self.assertEqual(adapter.agent.run.call_count, len(statuses))
                self.assertEqual(result.errors, expected_errors)

    def test_candidate_files_come_from_tools_not_repeated_model_manifest(self) -> None:
        for inline in (False, True):
            with self.subTest(inline=inline):
                adapter = ReconstructionRuntime(
                    Mock(model_name="test"), source={"session_parser": {"system_context": []}},
                    task={}, runtime_factory=Mock(),
                )
                session = AgentSession(replay_files={"a.py": "observed"}, writes=[
                    {"path": "a.py", "content": "executed and checked", "evidence_ref_ids": ["e"]},
                ])
                declared = [{"path": "a.py", "content": "never written"}] if inline else ["/old/host/a.py"]
                adapter.agent = Mock()
                adapter.agent.run.return_value = AgentResult(
                    role="completion", backend="test", completed=True,
                    payload={"candidates": [{"files": declared, "decision": "READY", "uncertainties": ["推断说明"]}]},
                )
                result = adapter.run(role=COMPLETION_REPLAYED_ROLE, instruction="恢复初态", session=session,
                                     output_root=self.root / str(inline))
                if inline:
                    self.assertIn("AUTHOR_INLINE_FILES_NOT_APPLIED", result.errors)
                else:
                    self.assertEqual(result.payload["candidates"][0]["files"], [])
                    self.assertEqual(result.payload["candidates"][0]["uncertainties"], ["推断说明"])
                    self.assertEqual(session.writes[0]["content"], "executed and checked")
                    self.assertEqual(result.errors, [])

    def test_prepared_entry_repairs_existing_candidate_instead_of_starting_over(self) -> None:
        module = "traceforge.reconstruction.pipeline."
        source = {"session_parser": {"status": "READY"}, "tasks": [{"task_id": "t"}],
                  "input_domain": "terminal"}
        candidate, feedback = {"workspace": "previous"}, {"failure": "原测试收集失败"}
        route = {"allow_completion": True, "env_origin": "REPLAYED"}
        with patch(module + "_replay_and_route", return_value=({}, Mock(), route, self.root)), patch(
            module + "execution_support_route", return_value=route,
        ), patch(module + "repair_workspace_completion", return_value={
            "status": "REVIEW", "errors": ["仍需修复"],
        }) as repair, patch(module + "complete_from_replayed") as initial:
            result = run_prepared_task(source=source, task=source["tasks"][0], agent=Mock(),
                                       output_root=self.root, completion_seed=candidate,
                                       completion_feedback=feedback)
        initial.assert_not_called()
        self.assertEqual(repair.call_args.kwargs["candidate"], candidate)
        self.assertEqual(repair.call_args.kwargs["feedback"], feedback)
        self.assertEqual(result["errors"], ["仍需修复"])
        with self.assertRaisesRegex(ReconstructionError, "检查点.*反馈"):
            run_prepared_task(source=source, task=source["tasks"][0], agent=Mock(),
                              output_root=self.root, completion_seed=candidate)
