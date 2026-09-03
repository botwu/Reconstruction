"""M2 前置 `UserTextProjection` 的稳定数据契约、闭合枚举、冻结白名单与固定派生函数。

本模块是该投影契约的唯一权威来源（[`../../../docs/m2-source-projection-spec.md`] §2/§4，
[`AGENTS.md`] §1 DRY）：注解产物 dataclass、schema 常量、闭合枚举、稳定 ID 公式，以及开头标签
文法与分类（`classify_leading_text`）——builder 与独立 validator 的篡改检测层都只从这里取用。

投影只做**结构判定**：一条 USER 事件正文"去空白后是否以一个白名单内的开标签起头"，不读语义、
不判断"像不像真实 query"。白名单是**冻结常量**，新成员只能经修订规格进入（规格 §2.3 准入三规则），
代码不得从数据学习。`UNKNOWN_TAGGED` 恒不携带标签名（内容安全：未知标签可能就是用户自写文本）。
上游 ID 一律当不透明外键；不 import M1B/M1D 私有派生公式。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from traceforge.trajectory.contracts import EventScope, SerializableContract
from traceforge.trajectory.event_payload import ContentBlocks, ContentPayload
from traceforge.trajectory.json_codec import stable_id
from traceforge.trajectory.privacy import (
    DATA_URL_SUMMARY_V1,
    TEXT_SEGMENT_V1,
    TEXT_WITH_DATA_URL_SEGMENTS_V1,
    privacy_envelope_kind,
)


class UserTextProjectionInputError(RuntimeError):
    """输入不可安全消费：上游 run 完整性/身份核验不过、透传字段类型非法或违反上游契约。"""


# --- 契约版本与 schema 常量 ------------------------------------------------

USER_TEXT_PROJECTION_CONTRACT_VERSION = "user-text-projection-v1"

PROJECTION_MANIFEST_SCHEMA = "traceforge.user-text-projection-manifest.v1"
PROJECTION_ARTIFACT_MANIFEST_SCHEMA = "traceforge.user-text-projection-artifact-manifest.v1"
PROJECTION_RUN_RECEIPT_SCHEMA = "traceforge.user-text-projection-run-receipt.v1"
PROJECTION_REPORT_SCHEMA = "traceforge.user-text-projection-report.v1"
USER_TEXT_ANNOTATION_SCHEMA = "traceforge.user-text-annotation.v1"

PROJECTION_RUN_ID_NAMESPACE = "user-text-projection-run-v1"

# 产物相对路径（规格 §4）：pipeline 写、validator 读，单一来源。
PROJECTION_MANIFEST_RELATIVE_PATH = "projection_manifest.json"
PROJECTION_REPORT_RELATIVE_PATH = "reports/projection_report.json"
ANNOTATIONS_RELATIVE_PATH = "private/user_text_annotations.jsonl"


# --- 闭合枚举 --------------------------------------------------------------


class Locality(StrEnum):
    """USER 事件相对观测窗口的可定位性（规格 §2.2），由 M1B ``event_scope`` 一一映射。"""

    OBSERVED = "OBSERVED"
    PREFIX_UNLOCALIZED = "PREFIX_UNLOCALIZED"


class ContentForm(StrEnum):
    """M1B ``payload.content`` 的发布形态（规格 §2.3），只透传、不解读。"""

    TEXT_STRING = "TEXT_STRING"
    TEXT_WITH_DATA_URL_SEGMENTS = "TEXT_WITH_DATA_URL_SEGMENTS"
    DATA_URL_SUMMARY = "DATA_URL_SUMMARY"
    CONTENT_BLOCKS = "CONTENT_BLOCKS"


class TextClass(StrEnum):
    """开头文本的结构类别（规格 §2.3，v0.3 冻结）。"""

    PLAIN_USER_TEXT = "PLAIN_USER_TEXT"
    EMPTY_TEXT = "EMPTY_TEXT"
    HARNESS_CONTEXT = "HARNESS_CONTEXT"
    HARNESS_CAPABILITY = "HARNESS_CAPABILITY"
    CONTROL_SIGNAL = "CONTROL_SIGNAL"
    UNKNOWN_TAGGED = "UNKNOWN_TAGGED"
    NO_LEADING_TEXT = "NO_LEADING_TEXT"


_LOCALITY_BY_SCOPE = {
    EventScope.OBSERVED_REQUEST_WINDOW.value: Locality.OBSERVED,
    EventScope.PRE_FIRST_OBSERVED_TERMINAL.value: Locality.PREFIX_UNLOCALIZED,
}


def locality_for_scope(event_scope: str) -> Locality:
    """M1B 闭合 ``EventScope`` → ``Locality``；枚举外的值属上游契约违约，fail-closed。"""

    try:
        return _LOCALITY_BY_SCOPE[event_scope]
    except KeyError as exc:
        raise UserTextProjectionInputError("输入 M1B 事件 scope 不在闭合枚举内") from exc


# --- 冻结白名单（规格 §2.3；v0.3 依 v4 run 探针与准入三规则冻结）------------

LEADING_TAG_WHITELIST: Mapping[TextClass, frozenset[str]] = {
    TextClass.HARNESS_CONTEXT: frozenset(
        {
            "environment_context",
            "in-app-browser-context",
            "system-reminder",
            "system-conventions",
            "codex_internal_context",
            "local-command-caveat",
            "available-deferred-tools",
        }
    ),
    TextClass.HARNESS_CAPABILITY: frozenset({"recommended_plugins", "codex_delegation"}),
    TextClass.CONTROL_SIGNAL: frozenset(
        {"turn_aborted", "user_interjection", "subagent_notification"}
    ),
}
TAGGED_TEXT_CLASSES = frozenset(LEADING_TAG_WHITELIST)

# 开标签文法（规格 §2.3）：`<` 紧跟标签名（字母或下划线起头；字母数字 `_ . : -`），名后必须紧跟
# 空白、`>` 或 `/`（属性、bare、自闭合三种形态皆可）。区分大小写；`</x>`、`<3`、`<<`、`< x`、
# `<x` 后直接结束均不匹配。
LEADING_TAG_PATTERN = re.compile(r"^<([A-Za-z_][A-Za-z0-9_.:-]*)(?=[\s>/])")
# 参与判定的开头窗口：左侧按 ``str.isspace()`` 去空白后取前 256 个码点。窗口内没有终结符的超长
# 标签名不匹配、按普通文本处理——确定且与长度上界无关。
HEAD_WINDOW_CODE_POINTS = 256


def leading_head_window(text: str) -> str:
    """规格 §2.3 的判定窗口：``text.lstrip()`` 的前 ``HEAD_WINDOW_CODE_POINTS`` 个码点。"""

    return text.lstrip()[:HEAD_WINDOW_CODE_POINTS]


def content_form_and_head(content: ContentPayload) -> tuple[ContentForm, str | None]:
    """把 typed reader 的 ``ContentPayload`` 映成 ``content_form`` 与开头文本（未取窗）。

    开头文本为 ``None`` 表示"没有可判定的开头文本"（Data URL 摘要、首段为 Data URL 的分段
    envelope、内容块）。分段 envelope 首段为文本段时取其 ``value``。M1B validator 已对每条事件
    跑过 typed reader，故非字符串 value 必是两种审计 envelope 之一；其余形态属契约违约。
    """

    if isinstance(content, ContentBlocks):
        return ContentForm.CONTENT_BLOCKS, None
    value = content.value
    if isinstance(value, str):
        return ContentForm.TEXT_STRING, value
    kind = privacy_envelope_kind(value)
    if kind == DATA_URL_SUMMARY_V1:
        return ContentForm.DATA_URL_SUMMARY, None
    if kind == TEXT_WITH_DATA_URL_SEGMENTS_V1:
        first_segment = value["segments"][0]
        if privacy_envelope_kind(first_segment) == TEXT_SEGMENT_V1:
            return ContentForm.TEXT_WITH_DATA_URL_SEGMENTS, first_segment["value"]
        return ContentForm.TEXT_WITH_DATA_URL_SEGMENTS, None
    raise UserTextProjectionInputError("输入 M1B USER content.value 不是字符串也不是审计 envelope")


def classify_leading_text(head_window: str | None) -> tuple[TextClass, str | None]:
    """规格 §2.3 的唯一分类实现：``(text_class, leading_tag)``。

    ``head_window`` 为 ``None`` → ``NO_LEADING_TEXT``；去空白后为空 → ``EMPTY_TEXT``；不以开标签
    起头 → ``PLAIN_USER_TEXT``；开标签名命中某白名单 → 该类别并回传标签名；否则
    ``UNKNOWN_TAGGED`` 且标签名恒为 ``None``（不落盘、不上报）。
    """

    if head_window is None:
        return TextClass.NO_LEADING_TEXT, None
    window = leading_head_window(head_window)
    if not window:
        return TextClass.EMPTY_TEXT, None
    match = LEADING_TAG_PATTERN.match(window)
    if match is None:
        return TextClass.PLAIN_USER_TEXT, None
    tag = match.group(1)
    for text_class, names in LEADING_TAG_WHITELIST.items():
        if tag in names:
            return text_class, tag
    return TextClass.UNKNOWN_TAGGED, None


# --- 产物 dataclass --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserTextAnnotationV1(SerializableContract):
    """每条 USER 事件恰一条的结构注解（规格 §2.2）；不含正文、不含未知标签名。"""

    schema_version: str
    event_occurrence_id: str
    capture_occurrence_id: str
    locality: str
    request_boundary_id: str | None
    user_block_id: str | None
    content_form: str
    text_class: str
    leading_tag: str | None
    utf8_byte_length: int | None


@dataclass(frozen=True, slots=True)
class ProjectionManifestV1(SerializableContract):
    """输入身份绑定（内容寻址，非路径，规格 §4）；M1D 可选，未绑定时两字段为 ``None``。"""

    schema_version: str
    projection_run_id: str
    projection_contract_version: str
    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    m1d_run_id: str | None
    m1d_artifact_manifest_sha256: str | None
    source_schema: str


@dataclass(frozen=True, slots=True)
class ProjectionArtifactManifestV1(SerializableContract):
    """确定性业务文件清单；不含自身与 run_receipt.json。"""

    schema_version: str
    projection_run_id: str
    projection_contract_version: str
    m1b_run_id: str
    m1b_artifact_manifest_sha256: str
    m1d_run_id: str | None
    m1d_artifact_manifest_sha256: str | None
    files: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ProjectionReportV1(SerializableContract):
    """公共报告：仅固定聚合计数，无原始 ID/正文/路径/URL（规格 §2.4 门④）。"""

    schema_version: str
    projection_run_id: str
    m1b_run_id: str
    m1d_run_id: str | None
    counts: dict[str, int]


# --- 稳定 ID 公式（规格 §4，单一权威）-------------------------------------


def projection_run_id(
    *,
    m1b_run_id: str,
    m1b_artifact_manifest_sha256: str,
    m1d_run_id: str | None,
    m1d_artifact_manifest_sha256: str | None,
) -> str:
    return stable_id(
        PROJECTION_RUN_ID_NAMESPACE,
        {
            "projection_contract_version": USER_TEXT_PROJECTION_CONTRACT_VERSION,
            "m1b_run_id": m1b_run_id,
            "m1b_artifact_manifest_sha256": m1b_artifact_manifest_sha256,
            "m1d_run_id": m1d_run_id,
            "m1d_artifact_manifest_sha256": m1d_artifact_manifest_sha256,
        },
    )


# --- 报告聚合计数（固定 allowlist，validator 断言闭合）---------------------


def _content_form_key(form: ContentForm) -> str:
    return f"content_form_{form.value.lower()}_count"


def _class_key(locality: Locality, text_class: TextClass) -> str:
    return f"{locality.value.lower()}_{text_class.value.lower()}_count"


PROJECTION_COUNT_KEYS = frozenset(
    {
        "capture_count",
        "user_event_count",
        "captures_with_plain_user_text",
        "captures_with_plain_user_text_only_in_prefix",
        "captures_with_observed_plain_user_text",
        *(_content_form_key(form) for form in ContentForm),
        *(_class_key(locality, text_class) for locality in Locality for text_class in TextClass),
    }
)


def build_report_counts(
    *,
    capture_count: int,
    user_event_count: int,
    content_form_counts: Mapping[ContentForm, int],
    class_counts: Mapping[tuple[Locality, TextClass], int],
    captures_with_plain_user_text: int,
    captures_with_plain_user_text_only_in_prefix: int,
    captures_with_observed_plain_user_text: int,
) -> dict[str, int]:
    """按固定 allowlist 组装报告计数，键集恒等于 ``PROJECTION_COUNT_KEYS``，按键排序。

    ``capture_count`` 即 M1B 已编译 capture 数（诚实分母的分母）；三个 capture 级分母含义见规格
    §2.4 门④。未出现的形态/类别显式记 0，使报告形状与数据无关。
    """

    only_in_prefix = captures_with_plain_user_text_only_in_prefix
    counts = {
        "capture_count": capture_count,
        "user_event_count": user_event_count,
        "captures_with_plain_user_text": captures_with_plain_user_text,
        "captures_with_plain_user_text_only_in_prefix": only_in_prefix,
        "captures_with_observed_plain_user_text": captures_with_observed_plain_user_text,
    }
    for form in ContentForm:
        counts[_content_form_key(form)] = content_form_counts.get(form, 0)
    for locality in Locality:
        for text_class in TextClass:
            counts[_class_key(locality, text_class)] = class_counts.get((locality, text_class), 0)
    return dict(sorted(counts.items()))
