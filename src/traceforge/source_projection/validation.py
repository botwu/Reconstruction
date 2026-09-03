"""对一个已发布 `UserTextProjection` run 做独立、可重算的验收（规格 §3）。

两层信任边界，与 M1D validator 同构（[`AGENTS.md`] §1 DRY/SRP）：

1. **篡改检测**——从上游 M1B（与可选 M1D）已发布字段重新读入（`reader`，内部先跑上游权威
   validator），调用与 pipeline **同一个纯 fold** `builder.build_user_text_projection_graph` 重建
   期望注解，与已发布 ``private/user_text_annotations.jsonl`` 做**双向集合相等**（canonical 字节
   比对）。
   删行、造幻影、改类别/标签/回指、同步重签，均被推翻。不复刻 fold（两份同 bug 必互相一致）。
2. **正交不变量**——只用上游视图 + 已发布注解表、**不经 fold 也不经分类函数**断言：USER 事件与注解
   一一覆盖；来源事实（capture、locality、boundary、content_form、utf8_byte_length）与 M1B 逐条
   一致；
   ``leading_tag`` 出现与否及取值受白名单闭合约束；类别与"有无开头文本/是否为空"的结构事实相容；
   M1D 绑定时回指与 UserBlock 索引逐条一致、前缀事件恒不回指；公共报告计数（含三个分母）由
   **已发布表**独立重算；跨模块不变量 ``captures_with_observed_plain_user_text ≤ 有 UserBlock 的
   capture 数``。这层用于检出 fold/分类自身的派生缺陷。

产物完整性用 `trajectory.run_validation` 的公共物理层原语核验。取三参
``validate_user_text_projection_run(projection_run_dir, m1b_run_dir, m1d_run_dir=None)``：投影只以
内容寻址身份绑定上游（规格 §4 禁存路径），故须同时拿到上游 run 作 oracle；manifest 绑定了 M1D 时
第三参必填，未绑定时不得提供——两者都是 fail-closed 的身份错误。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from traceforge.source_projection.builder import build_user_text_projection_graph
from traceforge.source_projection.contracts import (
    ANNOTATIONS_RELATIVE_PATH,
    LEADING_TAG_WHITELIST,
    PROJECTION_ARTIFACT_MANIFEST_SCHEMA,
    PROJECTION_COUNT_KEYS,
    PROJECTION_MANIFEST_RELATIVE_PATH,
    PROJECTION_MANIFEST_SCHEMA,
    PROJECTION_REPORT_RELATIVE_PATH,
    PROJECTION_REPORT_SCHEMA,
    PROJECTION_RUN_RECEIPT_SCHEMA,
    TAGGED_TEXT_CLASSES,
    USER_TEXT_ANNOTATION_SCHEMA,
    USER_TEXT_PROJECTION_CONTRACT_VERSION,
    ContentForm,
    Locality,
    ProjectionArtifactManifestV1,
    ProjectionManifestV1,
    ProjectionReportV1,
    TextClass,
    UserTextAnnotationV1,
    UserTextProjectionInputError,
    build_report_counts,
    locality_for_scope,
    projection_run_id,
)
from traceforge.source_projection.reader import load_m1b_user_text_view, load_m1d_block_view
from traceforge.source_projection.view import M1bUserTextView, M1dBlockView
from traceforge.trajectory.json_codec import canonical_json_bytes
from traceforge.trajectory.run_validation import (
    IssueCollector,
    ValidationIssue,
    bijection,
    check_artifact_bytes,
    check_contract,
    check_inventory,
    check_receipt,
    is_int,
    iter_canonical_jsonl,
    manifest_entries,
    read_json,
    scan_control_pathlike,
)

_MANIFEST = PROJECTION_MANIFEST_RELATIVE_PATH
_REPORT = PROJECTION_REPORT_RELATIVE_PATH
_DETERMINISTIC_FILES = frozenset({_MANIFEST, _REPORT, ANNOTATIONS_RELATIVE_PATH})
_ALL_FILES = _DETERMINISTIC_FILES | {"artifact_manifest.json", "run_receipt.json"}

_CONTENT_FORM_VALUES = frozenset(member.value for member in ContentForm)
_LOCALITY_VALUES = frozenset(member.value for member in Locality)
_TEXT_CLASS_VALUES = frozenset(member.value for member in TextClass)

# 已发布注解表：event_occurrence_id → 记录。
_Annotations = dict[str, dict[str, Any]]


@dataclass(frozen=True, slots=True)
class UserTextProjectionValidationResult:
    """完整投影 run 的验收结果。"""

    ok: bool
    issues: tuple[ValidationIssue, ...]
    observed_counts: dict[str, int]
    checked_file_count: int

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(f"[{issue.code}] {issue.location}: {issue.message}" for issue in self.issues)


# --- 顶层编排 --------------------------------------------------------------


def validate_user_text_projection_run(
    projection_run_dir: str | Path,
    m1b_run_dir: str | Path,
    m1d_run_dir: str | Path | None = None,
) -> UserTextProjectionValidationResult:
    """独立校验一个已发布投影 run，取上游 M1B（与 manifest 绑定时的 M1D）run 作复算 oracle。"""

    root = Path(projection_run_dir)
    issues = IssueCollector()
    if not root.is_dir():
        issues.add("PROJECTION_RUN_NOT_DIRECTORY", "run", "projection_run_dir 不是可读目录")
        return UserTextProjectionValidationResult(False, issues.finish(), {}, 0)

    artifact_manifest = read_json(root / "artifact_manifest.json", issues)
    entries = manifest_entries(
        root,
        artifact_manifest,
        contract=ProjectionArtifactManifestV1,
        schema=PROJECTION_ARTIFACT_MANIFEST_SCHEMA,
        expected_files=_DETERMINISTIC_FILES,
        issues=issues,
    )
    checked = check_artifact_bytes(root, entries, issues)
    check_inventory(root, _ALL_FILES, issues)

    manifest = read_json(root / _MANIFEST, issues)
    check_contract(manifest, ProjectionManifestV1, PROJECTION_MANIFEST_SCHEMA, _MANIFEST, issues)
    report = read_json(root / _REPORT, issues)
    _check_report_shape(report, issues)
    receipt = read_json(root / "run_receipt.json", issues)
    check_receipt(
        root,
        receipt,
        artifact_manifest,
        receipt_schema=PROJECTION_RUN_RECEIPT_SCHEMA,
        manifest_run_id_field="projection_run_id",
        issues=issues,
    )
    for control, location in (
        (manifest, _MANIFEST),
        (artifact_manifest, "artifact_manifest.json"),
        (report, _REPORT),
        (receipt, "run_receipt.json"),
    ):
        scan_control_pathlike(
            control, location, code="PROJECTION_CONTROL_ABSOLUTE_PATH", issues=issues
        )

    # 规格 §2.1 先校验再消费：上游完整性与身份核验全权委托上游权威 validator（reader 内部调用）。
    view: M1bUserTextView | None
    try:
        view = load_m1b_user_text_view(m1b_run_dir)
    except UserTextProjectionInputError:
        issues.add("M1B_INPUT_INVALID", "m1b_run", "上游 M1B run 未通过独立完整性/身份校验")
        view = None

    block_view, binding_consistent = _resolve_m1d_binding(
        manifest, m1b_run_dir, m1d_run_dir, issues
    )

    observed_counts: dict[str, int] = {}
    if view is not None:
        _check_bindings(
            root, view, block_view, manifest, artifact_manifest, report, receipt, issues
        )
        if binding_consistent:
            observed_counts = _check_annotations(root, view, block_view, report, issues)

    finished = issues.finish()
    return UserTextProjectionValidationResult(
        ok=not finished,
        issues=finished,
        observed_counts=observed_counts,
        checked_file_count=checked,
    )


def _resolve_m1d_binding(
    manifest: dict[str, Any] | None,
    m1b_run_dir: str | Path,
    m1d_run_dir: str | Path | None,
    issues: IssueCollector,
) -> tuple[M1dBlockView | None, bool]:
    """按 manifest 声明决定是否需要 M1D oracle；返回 ``(block_view, 绑定可用于复算)``。"""

    declared = manifest.get("m1d_run_id") if isinstance(manifest, dict) else None
    if declared is None:
        if m1d_run_dir is not None:
            issues.add(
                "M1D_BINDING_MISMATCH", "m1d_run", "投影未绑定 M1D run，但校验时提供了 M1D oracle"
            )
            return None, False
        return None, True
    if m1d_run_dir is None:
        issues.add("M1D_RUN_REQUIRED", "m1d_run", "投影绑定了 M1D run，校验必须提供该 M1D oracle")
        return None, False
    try:
        block_view = load_m1d_block_view(m1d_run_dir, m1b_run_dir)
    except UserTextProjectionInputError:
        issues.add("M1D_INPUT_INVALID", "m1d_run", "上游 M1D run 未通过独立完整性/绑定校验")
        return None, False
    consistent = True
    if block_view.m1d_run_id != declared:
        issues.add("M1D_RUN_ID_BINDING_MISMATCH", _MANIFEST, "绑定的 m1d_run_id 与所给上游不一致")
        consistent = False
    if isinstance(manifest, dict) and (
        block_view.m1d_artifact_manifest_sha256 != manifest.get("m1d_artifact_manifest_sha256")
    ):
        issues.add(
            "M1D_MANIFEST_SHA_BINDING_MISMATCH",
            _MANIFEST,
            "绑定的 m1d_artifact_manifest_sha256 与所给上游不一致",
        )
        consistent = False
    return block_view, consistent


# --- 公共报告形状 --------------------------------------------------------------


def _check_report_shape(report: dict[str, Any] | None, issues: IssueCollector) -> None:
    """公共报告：闭合 schema + 仅固定聚合计数键 + 非负整数（不含原始 ID/正文/路径）。"""

    if not check_contract(report, ProjectionReportV1, PROJECTION_REPORT_SCHEMA, _REPORT, issues):
        return
    counts = report.get("counts")
    if not isinstance(counts, dict) or frozenset(counts) != PROJECTION_COUNT_KEYS:
        issues.add(
            "PROJECTION_REPORT_ALLOWLIST_MISMATCH",
            f"{_REPORT}/counts",
            "公共报告必须只包含固定聚合计数",
        )
        return
    if any(not is_int(value) or value < 0 for value in counts.values()):
        issues.add(
            "PROJECTION_REPORT_VALUE_INVALID", f"{_REPORT}/counts", "公共报告计数必须是非负整数"
        )


# --- 内容寻址绑定 ----------------------------------------------------------


def _check_bindings(
    root: Path,
    view: M1bUserTextView,
    block_view: M1dBlockView | None,
    manifest: dict[str, Any] | None,
    artifact_manifest: dict[str, Any] | None,
    report: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    issues: IssueCollector,
) -> None:
    """断言投影自报的上游身份==所给上游 run 的已发布身份，且内容寻址 run_id 处处一致。"""

    declared_m1d_run_id = manifest.get("m1d_run_id") if isinstance(manifest, dict) else None
    declared_m1d_sha = (
        manifest.get("m1d_artifact_manifest_sha256") if isinstance(manifest, dict) else None
    )
    expected_run_id = projection_run_id(
        m1b_run_id=view.m1b_run_id,
        m1b_artifact_manifest_sha256=view.m1b_artifact_manifest_sha256,
        m1d_run_id=block_view.m1d_run_id if block_view is not None else declared_m1d_run_id,
        m1d_artifact_manifest_sha256=(
            block_view.m1d_artifact_manifest_sha256 if block_view is not None else declared_m1d_sha
        ),
    )
    if root.name != expected_run_id:
        issues.add(
            "PROJECTION_RUN_DIRECTORY_ID_MISMATCH", "run", "目录名不等于内容寻址 projection_run_id"
        )

    for value, location in ((manifest, _MANIFEST), (artifact_manifest, "artifact_manifest.json")):
        if not isinstance(value, dict):
            continue
        if value.get("m1b_run_id") != view.m1b_run_id:
            issues.add(
                "M1B_RUN_ID_BINDING_MISMATCH", location, "绑定的 m1b_run_id 与所给上游不一致"
            )
        if value.get("m1b_artifact_manifest_sha256") != view.m1b_artifact_manifest_sha256:
            issues.add(
                "M1B_MANIFEST_SHA_BINDING_MISMATCH",
                location,
                "绑定的 m1b_artifact_manifest_sha256 与所给上游不一致",
            )
        if value.get("m1d_run_id") != declared_m1d_run_id or (
            value.get("m1d_artifact_manifest_sha256") != declared_m1d_sha
        ):
            issues.add("M1D_BINDING_MISMATCH", location, "两份 manifest 声明的 M1D 绑定不一致")
        if value.get("projection_run_id") != expected_run_id:
            issues.add("PROJECTION_RUN_ID_MISMATCH", location, "内容寻址 projection_run_id 不匹配")
        if value.get("projection_contract_version") != USER_TEXT_PROJECTION_CONTRACT_VERSION:
            issues.add("PROJECTION_CONTRACT_VERSION_MISMATCH", location, "投影契约版本不匹配")

    if isinstance(manifest, dict) and manifest.get("source_schema") != view.source_schema:
        issues.add("PROJECTION_SOURCE_SCHEMA_MISMATCH", _MANIFEST, "source_schema 不一致")
    if isinstance(report, dict):
        if report.get("projection_run_id") != expected_run_id:
            issues.add("PROJECTION_RUN_ID_MISMATCH", _REPORT, "projection_run_id 不匹配")
        if report.get("m1b_run_id") != view.m1b_run_id:
            issues.add("M1B_RUN_ID_BINDING_MISMATCH", _REPORT, "m1b_run_id 不一致")
        if report.get("m1d_run_id") != declared_m1d_run_id:
            issues.add("M1D_BINDING_MISMATCH", _REPORT, "m1d_run_id 与 manifest 声明不一致")
    if isinstance(receipt, dict) and receipt.get("run_id") != expected_run_id:
        issues.add(
            "PROJECTION_RUN_ID_MISMATCH", "run_receipt.json", "内容寻址 projection_run_id 不匹配"
        )


# --- 注解表：篡改检测（同一纯 fold 双向 bijection）+ 正交不变量 -----------------


def _check_annotations(
    root: Path,
    view: M1bUserTextView,
    block_view: M1dBlockView | None,
    report: dict[str, Any] | None,
    issues: IssueCollector,
) -> dict[str, int]:
    """读入注解表；bijection 抓篡改，不变量抓派生缺陷；返回由已发布表重算的聚合计数。"""

    annotations = _read_annotations(root, issues)

    try:
        graph = build_user_text_projection_graph(view=view, block_view=block_view)
    except UserTextProjectionInputError:
        # 上游 validator 已通过却仍不满足 fold 的输入前提（如 M1D 绑定下观测事件无 UserBlock 归属）
        # 属上游契约违约；不复算期望集，但正交不变量层照常运行。
        issues.add("PROJECTION_FOLD_INPUT_INVALID", "m1b_run", "上游视图不满足投影 fold 的输入前提")
    else:
        bijection(
            "USER_TEXT_ANNOTATION",
            ANNOTATIONS_RELATIVE_PATH,
            {
                annotation.event_occurrence_id: canonical_json_bytes(annotation.to_dict())
                for annotation in graph.annotations
            },
            {
                identifier: canonical_json_bytes(record)
                for identifier, record in annotations.items()
            },
            issues,
        )

    _check_coverage_and_source_facts(view, annotations, issues)
    _check_class_structure(view, annotations, issues)
    _check_block_binding(block_view, annotations, issues)
    counts = _counts_from_table(view, annotations)
    _check_cross_module_invariant(block_view, counts, issues)
    _compare_report(report, counts, issues)
    return counts


def _read_annotations(root: Path, issues: IssueCollector) -> _Annotations:
    """读入注解表：逐行 canonical/schema/隐私核验，键于 event_occurrence_id，检出重复。"""

    observed: _Annotations = {}
    for line_number, record in enumerate(
        iter_canonical_jsonl(
            root,
            ANNOTATIONS_RELATIVE_PATH,
            schema=USER_TEXT_ANNOTATION_SCHEMA,
            contract=UserTextAnnotationV1,
            issues=issues,
        ),
        1,
    ):
        location = f"{ANNOTATIONS_RELATIVE_PATH}:{line_number}"
        identifier = record.get("event_occurrence_id")
        if not isinstance(identifier, str) or not identifier:
            issues.add("USER_TEXT_PRIMARY_KEY_INVALID", location, "event_occurrence_id 非法")
            continue
        if identifier in observed:
            issues.add("USER_TEXT_DUPLICATE_ID", location, "event_occurrence_id 重复")
        observed[identifier] = record
    return observed


def _check_coverage_and_source_facts(
    view: M1bUserTextView, annotations: _Annotations, issues: IssueCollector
) -> None:
    """USER 事件 ⇄ 注解一一覆盖；来源事实逐条与 M1B 视图一致（不经 fold）。"""

    events_by_id = {event.event_id: event for event in view.user_events}
    for event_id in sorted(events_by_id.keys() - annotations.keys()):
        issues.add(
            "USER_TEXT_EVENT_UNCOVERED",
            ANNOTATIONS_RELATIVE_PATH,
            f"M1B USER 事件无注解：{event_id}",
        )
    for event_id in sorted(annotations.keys() - events_by_id.keys()):
        issues.add(
            "USER_TEXT_EVENT_FOREIGN",
            ANNOTATIONS_RELATIVE_PATH,
            f"注解引用了不存在的 M1B USER 事件：{event_id}",
        )
    for event_id in sorted(annotations.keys() & events_by_id.keys()):
        record = annotations[event_id]
        event = events_by_id[event_id]
        location = f"{ANNOTATIONS_RELATIVE_PATH}/{event_id}"
        try:
            expected_locality = locality_for_scope(event.event_scope).value
        except UserTextProjectionInputError:
            issues.add("USER_TEXT_SOURCE_FACT_MISMATCH", location, "M1B 事件 scope 不在闭合枚举内")
            continue
        facts = (
            (record["capture_occurrence_id"], event.capture_occurrence_id, "capture_occurrence_id"),
            (record["locality"], expected_locality, "locality"),
            (record["request_boundary_id"], event.request_boundary_id, "request_boundary_id"),
            (record["content_form"], event.content_form, "content_form"),
            (record["utf8_byte_length"], event.utf8_byte_length, "utf8_byte_length"),
        )
        for observed, expected, field in facts:
            if observed != expected:
                issues.add(
                    "USER_TEXT_SOURCE_FACT_MISMATCH", location, f"{field} 与 M1B 已发布事实不一致"
                )
        if expected_locality == Locality.OBSERVED.value and event.request_boundary_id is None:
            issues.add(
                "USER_TEXT_SOURCE_FACT_MISMATCH", location, "观测窗口事件缺少 request_boundary_id"
            )
        if expected_locality == Locality.PREFIX_UNLOCALIZED.value and (
            event.request_boundary_id is not None
        ):
            issues.add(
                "USER_TEXT_SOURCE_FACT_MISMATCH", location, "前缀事件不应携带 request_boundary_id"
            )


def _check_class_structure(
    view: M1bUserTextView, annotations: _Annotations, issues: IssueCollector
) -> None:
    """类别/标签与结构事实相容（不经分类函数）：

    - ``leading_tag ≠ null ⇔ text_class ∈ 带标签类别``，且取值恒在该类别白名单内；
    - ``NO_LEADING_TEXT ⇔ 无开头文本``；``EMPTY_TEXT ⇔ 有开头文本且判定窗口为空``；
      其余类别 ⇒ 判定窗口非空。
    """

    head_by_id = {event.event_id: event.head_window for event in view.user_events}
    for event_id in sorted(annotations.keys() & head_by_id.keys()):
        record = annotations[event_id]
        location = f"{ANNOTATIONS_RELATIVE_PATH}/{event_id}"
        text_class = record["text_class"]
        leading_tag = record["leading_tag"]
        if text_class not in _TEXT_CLASS_VALUES:
            issues.add("USER_TEXT_CLASS_INVALID", location, "text_class 不在闭合枚举内")
            continue
        if TextClass(text_class) in TAGGED_TEXT_CLASSES:
            if leading_tag not in LEADING_TAG_WHITELIST[TextClass(text_class)]:
                issues.add(
                    "USER_TEXT_LEADING_TAG_INVALID",
                    location,
                    "带标签类别的 leading_tag 不在白名单内",
                )
        elif leading_tag is not None:
            issues.add(
                "USER_TEXT_LEADING_TAG_INVALID", location, "非带标签类别不得携带 leading_tag"
            )

        head_window = head_by_id[event_id]
        if head_window is None:
            expected_class = TextClass.NO_LEADING_TEXT.value
        elif not head_window:
            expected_class = TextClass.EMPTY_TEXT.value
        else:
            expected_class = None  # 非空开头文本：具体类别由分类函数决定，此处只排除两类结构类别
        if expected_class is not None and text_class != expected_class:
            issues.add(
                "USER_TEXT_CLASS_HEAD_MISMATCH", location, "text_class 与开头文本的结构事实不相容"
            )
        if expected_class is None and text_class in {
            TextClass.NO_LEADING_TEXT.value,
            TextClass.EMPTY_TEXT.value,
        }:
            issues.add(
                "USER_TEXT_CLASS_HEAD_MISMATCH", location, "有非空开头文本却标为无文本/空文本类别"
            )


def _check_block_binding(
    block_view: M1dBlockView | None, annotations: _Annotations, issues: IssueCollector
) -> None:
    """M1D 回指（规格 §3）：绑定时观测事件恒等于 UserBlock 索引且 capture 一致、前缀事件恒空；
    未绑定时全表为空。"""

    for event_id in sorted(annotations):
        record = annotations[event_id]
        location = f"{ANNOTATIONS_RELATIVE_PATH}/{event_id}"
        block_id = record["user_block_id"]
        if block_view is None or record["locality"] != Locality.OBSERVED.value:
            if block_id is not None:
                issues.add(
                    "USER_TEXT_BLOCK_BINDING_INVALID",
                    location,
                    "未绑定 M1D 或前缀事件不得回指 UserBlock",
                )
            continue
        expected = block_view.user_block_id_by_event_id.get(event_id)
        if expected is None:
            issues.add(
                "USER_TEXT_BLOCK_BINDING_INVALID",
                location,
                "观测窗口事件在 M1D 中无 UserBlock 归属",
            )
            continue
        if block_id != expected:
            issues.add(
                "USER_TEXT_BLOCK_BINDING_INVALID",
                location,
                "user_block_id 与 M1D UserBlock 索引不一致",
            )
        elif block_view.capture_id_by_user_block_id[expected] != record["capture_occurrence_id"]:
            issues.add(
                "USER_TEXT_BLOCK_BINDING_INVALID",
                location,
                "回指的 UserBlock 与注解不属于同一 capture",
            )


def _counts_from_table(view: M1bUserTextView, annotations: _Annotations) -> dict[str, int]:
    """由**已发布注解表**独立重算公共报告计数（capture_count 取 M1B 已编译 capture 数）。"""

    content_form_counts: Counter[ContentForm] = Counter()
    class_counts: Counter[tuple[Locality, TextClass]] = Counter()
    plain_localities: defaultdict[str, set[str]] = defaultdict(set)
    for record in annotations.values():
        form = record["content_form"]
        locality = record["locality"]
        text_class = record["text_class"]
        if form in _CONTENT_FORM_VALUES:
            content_form_counts[ContentForm(form)] += 1
        if locality in _LOCALITY_VALUES and text_class in _TEXT_CLASS_VALUES:
            class_counts[(Locality(locality), TextClass(text_class))] += 1
        if text_class == TextClass.PLAIN_USER_TEXT.value:
            plain_localities[record["capture_occurrence_id"]].add(locality)
    return build_report_counts(
        capture_count=len(view.capture_occurrence_ids),
        user_event_count=len(annotations),
        content_form_counts=content_form_counts,
        class_counts=class_counts,
        captures_with_plain_user_text=len(plain_localities),
        captures_with_plain_user_text_only_in_prefix=sum(
            1
            for localities in plain_localities.values()
            if localities == {Locality.PREFIX_UNLOCALIZED.value}
        ),
        captures_with_observed_plain_user_text=sum(
            1 for localities in plain_localities.values() if Locality.OBSERVED.value in localities
        ),
    )


def _check_cross_module_invariant(
    block_view: M1dBlockView | None, counts: Mapping[str, int], issues: IssueCollector
) -> None:
    """规格 §2.4：绑定 M1D 时，观测窗口有普通用户文本的 capture 数 ≤ 有 UserBlock 的 capture 数。"""

    if block_view is None:
        return
    observed_plain = counts["captures_with_observed_plain_user_text"]
    if observed_plain > len(block_view.capture_ids_with_user_blocks):
        issues.add(
            "USER_TEXT_CROSS_MODULE_INVARIANT_VIOLATED",
            _REPORT,
            "captures_with_observed_plain_user_text 超过 M1D 有 UserBlock 的 capture 数",
        )


def _compare_report(
    report: Mapping[str, Any] | None, observed: Mapping[str, int], issues: IssueCollector
) -> None:
    if not isinstance(report, dict) or not isinstance(report.get("counts"), dict):
        return
    for key in sorted(PROJECTION_COUNT_KEYS):
        if report["counts"].get(key) != observed.get(key):
            issues.add(
                "PROJECTION_REPORT_COUNT_MISMATCH",
                f"{_REPORT}/counts/{key}",
                f"报告={report['counts'].get(key)}，重算={observed.get(key)}",
            )
