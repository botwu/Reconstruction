"""`UserTextProjection` 契约层纯函数：开头标签文法、冻结白名单、content_form 映射、报告键闭合。

对应规格 §2.3 的判定向量（[`docs/m2-source-projection-spec.md`] §6 列表①②③）。这里只测纯函数，不建
run；所有字面量均为虚构，不含任何真实回流正文。
"""

from __future__ import annotations

import pytest

from traceforge.source_projection.contracts import (
    HEAD_WINDOW_CODE_POINTS,
    LEADING_TAG_WHITELIST,
    PROJECTION_COUNT_KEYS,
    ContentForm,
    Locality,
    TextClass,
    UserTextProjectionInputError,
    build_report_counts,
    classify_leading_text,
    content_form_and_head,
    leading_head_window,
    locality_for_scope,
)
from traceforge.trajectory.event_payload import ContentBlocks, TextContent
from traceforge.trajectory.json_codec import sha256_bytes
from traceforge.trajectory.privacy import (
    TEXT_WITH_DATA_URL_SEGMENTS_V1,
    privacy_envelope_kind,
    sanitize_value,
)

# --- 冻结白名单（规格 §2.3 v0.3：A7 / B2 / C3）---------------------------------


def test_whitelist_members_are_frozen_and_disjoint() -> None:
    """白名单是规格冻结常量：成员逐一对照 v0.3，且三类互不相交。"""

    assert LEADING_TAG_WHITELIST[TextClass.HARNESS_CONTEXT] == {
        "environment_context",
        "in-app-browser-context",
        "system-reminder",
        "system-conventions",
        "codex_internal_context",
        "local-command-caveat",
        "available-deferred-tools",
    }
    assert LEADING_TAG_WHITELIST[TextClass.HARNESS_CAPABILITY] == {
        "recommended_plugins",
        "codex_delegation",
    }
    assert LEADING_TAG_WHITELIST[TextClass.CONTROL_SIGNAL] == {
        "turn_aborted",
        "user_interjection",
        "subagent_notification",
    }
    all_tags = [tag for names in LEADING_TAG_WHITELIST.values() for tag in names]
    assert len(all_tags) == len(set(all_tags)) == 12
    assert set(LEADING_TAG_WHITELIST) == {
        TextClass.HARNESS_CONTEXT,
        TextClass.HARNESS_CAPABILITY,
        TextClass.CONTROL_SIGNAL,
    }


# --- 分类向量 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        # 无开头文本 / 空文本 / 普通文本
        (None, (TextClass.NO_LEADING_TEXT, None)),
        ("", (TextClass.EMPTY_TEXT, None)),
        (" \n\t\u3000", (TextClass.EMPTY_TEXT, None)),
        ("请帮我整理一个虚构的问题。", (TextClass.PLAIN_USER_TEXT, None)),
        ("请看 <environment_context> 这个词", (TextClass.PLAIN_USER_TEXT, None)),
        # 三种开标签形态：bare / 属性 / 自闭合，均命中白名单
        (
            "<environment_context>\n虚构环境\n</environment_context>",
            (
                TextClass.HARNESS_CONTEXT,
                "environment_context",
            ),
        ),
        (
            '<in-app-browser-context url="x">页面</in-app-browser-context>',
            (
                TextClass.HARNESS_CONTEXT,
                "in-app-browser-context",
            ),
        ),
        ("<turn_aborted/>", (TextClass.CONTROL_SIGNAL, "turn_aborted")),
        ("<turn_aborted>", (TextClass.CONTROL_SIGNAL, "turn_aborted")),
        ("<recommended_plugins>\n插件", (TextClass.HARNESS_CAPABILITY, "recommended_plugins")),
        ("<codex_delegation depth=1>", (TextClass.HARNESS_CAPABILITY, "codex_delegation")),
        ("<subagent_notification>x", (TextClass.CONTROL_SIGNAL, "subagent_notification")),
        # 左侧空白不影响判定（判定窗口先 lstrip）
        ("  \n<system-reminder>提醒", (TextClass.HARNESS_CONTEXT, "system-reminder")),
        # 合法开标签但不在白名单：UNKNOWN_TAGGED 且恒不回传标签名
        ("<mystery_tag>用户自写标记</mystery_tag>", (TextClass.UNKNOWN_TAGGED, None)),
        ("<Environment_Context>大小写不同</Environment_Context>", (TextClass.UNKNOWN_TAGGED, None)),
        ("<environment_contextual>", (TextClass.UNKNOWN_TAGGED, None)),
        ("<a.b:c-d>", (TextClass.UNKNOWN_TAGGED, None)),
        ("<_x/>", (TextClass.UNKNOWN_TAGGED, None)),
        # 不是开标签：闭标签、数字起头、双尖括号、尖括号后空白、名后直接结束、标签名超出窗口
        ("</environment_context>", (TextClass.PLAIN_USER_TEXT, None)),
        ("<3 个苹果", (TextClass.PLAIN_USER_TEXT, None)),
        ("<< x", (TextClass.PLAIN_USER_TEXT, None)),
        ("< environment_context>", (TextClass.PLAIN_USER_TEXT, None)),
        ("<environment_context", (TextClass.PLAIN_USER_TEXT, None)),
        ("<", (TextClass.PLAIN_USER_TEXT, None)),
        ("<-x>", (TextClass.PLAIN_USER_TEXT, None)),
        # 前置 BOM / 零宽字符不是 str.isspace() 空白：`<` 不在去空白后的首位 → 普通文本（规格 §2.3）
        ("\ufeff<environment_context>", (TextClass.PLAIN_USER_TEXT, None)),
        ("\u200b<turn_aborted/>", (TextClass.PLAIN_USER_TEXT, None)),
    ],
)
def test_classify_leading_text_vectors(
    head: str | None, expected: tuple[TextClass, str | None]
) -> None:
    assert classify_leading_text(head) == expected


def test_head_window_is_lstrip_then_first_256_code_points() -> None:
    """判定窗口：先 lstrip 再截 256 码点；名后终结符落在窗口外的超长标签名按普通文本处理。"""

    assert HEAD_WINDOW_CODE_POINTS == 256
    assert leading_head_window("  \n<x>rest") == "<x>rest"
    assert len(leading_head_window("a" * 1000)) == 256

    # 恰好填满窗口而无终结符 → 普通文本；留一格给 `>` → 合法开标签（未知）
    assert classify_leading_text("<" + "a" * 255) == (TextClass.PLAIN_USER_TEXT, None)
    assert classify_leading_text("<" + "a" * 254 + ">") == (TextClass.UNKNOWN_TAGGED, None)
    # 白名单标签名跨过窗口边界仍能命中：名后紧跟终结符落在窗口内
    padded = " " * 500 + "<turn_aborted>"
    assert classify_leading_text(padded) == (TextClass.CONTROL_SIGNAL, "turn_aborted")


def test_classification_is_pure_and_only_reads_the_head() -> None:
    """同输入恒同输出；白名单标签之后的任意正文（含其它标签）不改变判定。"""

    tagged = "<environment_context>\n" + "<mystery>" * 50 + "\n正文"
    assert classify_leading_text(tagged) == classify_leading_text(tagged)
    assert classify_leading_text(tagged) == (TextClass.HARNESS_CONTEXT, "environment_context")


# --- Locality / content_form 映射 -----------------------------------------------


def test_locality_for_scope_maps_closed_enum_and_fails_closed() -> None:
    assert locality_for_scope("OBSERVED_REQUEST_WINDOW") is Locality.OBSERVED
    assert locality_for_scope("PRE_FIRST_OBSERVED_TERMINAL") is Locality.PREFIX_UNLOCALIZED
    with pytest.raises(UserTextProjectionInputError):
        locality_for_scope("SOMETHING_ELSE")


_DATA_URL = "data:image/png;base64,U0VDUkVUX0JBU0U2NA=="


def _text(raw: str) -> TextContent:
    """用 M1B 的真实隐私变换产出发布形态 value，避免手工拼 envelope 与契约漂移。"""

    value = sanitize_value(raw, "/messages/0/content")
    encoded = raw.encode("utf-8")
    return TextContent(value=value, utf8_byte_length=len(encoded), sha256=sha256_bytes(encoded))


def test_content_form_and_head_covers_all_published_forms() -> None:
    """四种发布形态 → (content_form, 开头文本)；只有字符串与首段文本的分段形态有开头文本。"""

    assert content_form_and_head(_text("  普通文本")) == (ContentForm.TEXT_STRING, "  普通文本")
    assert content_form_and_head(_text(_DATA_URL)) == (ContentForm.DATA_URL_SUMMARY, None)

    text_first = _text(f"看图 {_DATA_URL} 结束")
    assert privacy_envelope_kind(text_first.value) == TEXT_WITH_DATA_URL_SEGMENTS_V1
    assert content_form_and_head(text_first) == (ContentForm.TEXT_WITH_DATA_URL_SEGMENTS, "看图 ")

    data_first = _text(f"{_DATA_URL} 后文")
    assert content_form_and_head(data_first) == (ContentForm.TEXT_WITH_DATA_URL_SEGMENTS, None)

    # 首段为全空白文本段：有开头文本但去空白后为空 → EMPTY_TEXT（规格 §3 不变量注）
    blank_first = _text(f"  \n{_DATA_URL}")
    form, head = content_form_and_head(blank_first)
    assert form is ContentForm.TEXT_WITH_DATA_URL_SEGMENTS
    assert classify_leading_text(head) == (TextClass.EMPTY_TEXT, None)

    blocks = ContentBlocks(blocks=[{"type": "input_text", "text": "块"}], block_count=1)
    assert content_form_and_head(blocks) == (ContentForm.CONTENT_BLOCKS, None)


def test_content_form_and_head_rejects_non_envelope_mapping() -> None:
    """非字符串且不是两种审计 envelope 的 value 属上游契约违约，fail-closed。"""

    bogus = TextContent(value={"not": "an envelope"}, utf8_byte_length=1, sha256="f" * 64)
    with pytest.raises(UserTextProjectionInputError):
        content_form_and_head(bogus)


# --- 报告键闭合 --------------------------------------------------------------


def test_report_counts_have_fixed_key_set_independent_of_data() -> None:
    """报告键集恒等于 allowlist（5 个总量/分母 + 4 形态 + 2×7 类别），未出现项显式为 0。"""

    counts = build_report_counts(
        capture_count=3,
        user_event_count=2,
        content_form_counts={ContentForm.TEXT_STRING: 2},
        class_counts={(Locality.OBSERVED, TextClass.PLAIN_USER_TEXT): 2},
        captures_with_plain_user_text=1,
        captures_with_plain_user_text_only_in_prefix=0,
        captures_with_observed_plain_user_text=1,
    )
    assert frozenset(counts) == PROJECTION_COUNT_KEYS
    assert len(PROJECTION_COUNT_KEYS) == 5 + 4 + 2 * 7
    assert list(counts) == sorted(counts)
    assert counts["observed_plain_user_text_count"] == 2
    assert counts["prefix_unlocalized_unknown_tagged_count"] == 0
    assert counts["content_form_content_blocks_count"] == 0
    assert "leading_tag" not in " ".join(counts)  # 报告永不携带标签名
