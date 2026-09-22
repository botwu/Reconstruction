"""验证不可重建边界不会吞掉基础设施故障或任务应修复的缺陷。"""

from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from traceforge.reconstruction.reconstructability import assess_reconstructability


def _missing_asset(**issue_fields):
    return {
        "status": "READY", "label": "SUFFICIENT", "errors": [],
        "integrity_report": {"issues": [{
            "id": "integrity-001", "code": "REFERENCED_ASSET_MISSING",
            "path": "scene.xml", "reference": "robot.xml", "reason": "引用资产不存在。",
            **issue_fields,
        }]},
    }


class ReconstructabilityTests(unittest.TestCase):
    def test_no_known_gap_is_ready_not_execution_proof(self):
        result = assess_reconstructability({"errors": [], "label": "SUFFICIENT"})
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["blockers"], [])

    def test_missing_asset_is_skipped_even_if_model_says_ready(self):
        result = assess_reconstructability(_missing_asset())
        self.assertEqual(result["status"], "SKIPPED_UNRECONSTRUCTABLE")
        self.assertEqual(result["blockers"][0]["reference"], "robot.xml")

    def test_baseline_requires_matching_task_evidence(self):
        payload = _missing_asset(
            classification="BASELINE_TASK_DEFECT", classification_reason="用户要求修复该 include。",
        )
        self.assertEqual(assess_reconstructability(payload)["status"], "REVIEW")
        issue = payload["integrity_report"]["issues"][0]
        issue["classification_evidence_ref_ids"] = ["user:repair"]
        self.assertEqual(assess_reconstructability(payload)["status"], "REVIEW")
        payload["task"] = {"acceptance_obligations": [{"evidence_ref_ids": ["user:repair"]}]}
        self.assertEqual(assess_reconstructability(payload)["status"], "READY")

    def test_unproven_irrelevance_retains_asset_blocker(self):
        payload = _missing_asset(classification="IRRELEVANT", classification_reason="可能不使用。")
        result = assess_reconstructability(payload)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["blockers"][0]["code"], "REFERENCED_ASSET_MISSING")

    def test_grounded_irrelevance_can_exclude_asset(self):
        payload = _missing_asset(
            classification="IRRELEVANT", classification_reason="只分析另一个文件。",
            classification_evidence_ref_ids=["user:scope"],
        )
        payload["task_evidence_ref_ids"] = ["user:scope"]
        self.assertEqual(assess_reconstructability(payload)["status"], "READY")

    def test_missing_binding_inventory_is_hard_gap(self):
        result = assess_reconstructability({"missing_binding_paths": ["src/core.py"]})
        self.assertEqual(result["status"], "SKIPPED_UNRECONSTRUCTABLE")
        self.assertEqual(result["blockers"][0]["path"], "src/core.py")

    def test_missing_binding_without_structured_path_is_review(self):
        result = assess_reconstructability({"errors": ["MISSING_BINDING_PATH"]})
        self.assertEqual(result["status"], "REVIEW")

    def test_prose_missing_context_is_not_proof(self):
        result = assess_reconstructability({
            "label": "INSUFFICIENT", "missing_context": ["缺少设备接口，猜测不可解。"],
        })
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["blockers"], [])

    def test_runtime_failure_preserves_missing_asset(self):
        for error in ("SANDBOX_INIT:ConnectionError", "MODEL_TIMEOUT", "SANDBOX_CLEANUP_FAILED"):
            with self.subTest(error=error):
                payload = _missing_asset()
                payload["errors"] = [error, "INVALID_JSON"]
                result = assess_reconstructability(payload)
                self.assertEqual(result["status"], "INFRA_ERROR")
                self.assertTrue(result["blockers"])

    def test_contract_failure_does_not_become_environment_skip(self):
        payload = _missing_asset()
        payload["errors"] = ["INTEGRITY_ISSUE_UNKNOWN"]
        result = assess_reconstructability(payload)
        self.assertEqual(result["status"], "SKIPPED_UNRECONSTRUCTABLE")
        self.assertTrue(result["blockers"])

    def test_malformed_errors_fail_closed(self):
        self.assertEqual(assess_reconstructability({"errors": "INVALID_JSON"})["status"], "PIPELINE_ERROR")

    def test_mutation_only_blocks_required_path_without_complete_initial_state(self):
        replay = SimpleNamespace(files=[], partial_evidence=[{
            "path": "src/core.py", "reason": "read_after_unparsed_mutation", "source_event_id": "c1",
        }])
        payload = {"required_paths": ["src/core.py"]}
        result = assess_reconstructability(payload, replay)
        self.assertEqual(result["status"], "SKIPPED_UNRECONSTRUCTABLE")
        self.assertEqual(result["blockers"][0]["source_event_id"], "c1")
        replay.files = [SimpleNamespace(path="src/core.py", completeness="COMPLETE")]
        self.assertEqual(assess_reconstructability(payload, replay)["status"], "READY")

    def test_unknown_mutation_does_not_poison_unrelated_or_unscoped_paths(self):
        for evidence in (
            {"path": "other.py", "reason": "read_after_unparsed_mutation"},
            {"path": None, "reason": "unparsed_mutation_unscoped"},
        ):
            replay = SimpleNamespace(files=[], partial_evidence=[evidence])
            required = [] if evidence["path"] is None else ["core.py"]
            self.assertEqual(assess_reconstructability({"required_paths": required}, replay)["status"], "READY")

    def test_unscoped_unknown_with_required_files_is_review_not_global_skip(self):
        replay = SimpleNamespace(files=[], partial_evidence=[{
            "path": None, "reason": "unparsed_mutation_unscoped", "source_event_id": "c2",
        }])
        result = assess_reconstructability({"required_paths": ["core.py"]}, replay)
        self.assertEqual(result["status"], "REVIEW")
        self.assertEqual(result["blockers"][0]["code"], "UNKNOWN_INITIAL_STATE_UNSCOPED")

    def test_directory_binding_scopes_unknown_initial_state(self):
        replay = SimpleNamespace(files=[], partial_evidence=[{
            "path": "src/core.py", "reason": "unparsed_mutation_scope",
        }])
        payload = {"task": {"environment_bindings": [{
            "verifier_kind": "FILE", "required_paths": ["src/"],
        }]}}
        self.assertEqual(assess_reconstructability(payload, replay)["status"], "SKIPPED_UNRECONSTRUCTABLE")

    def test_assessment_does_not_mutate_stage_evidence(self):
        payload = _missing_asset()
        before = copy.deepcopy(payload)
        assess_reconstructability(payload)
        self.assertEqual(payload, before)


if __name__ == "__main__":
    unittest.main()
