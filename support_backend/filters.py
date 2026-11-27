"""Filter rules, preset contact/feedback templates, and unresolved staff case matching."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
import unicodedata

EMERGENCY_CONTACT_REPLY = (
    "如您遇到紧急情况，请立即联系图书馆值班工作人员：\n"
    "• 桑浦山校区服务台：0754-8650 2958（校园网短号 882958）\n"
    "• 东海岸校区服务台：0754-8650 2999（校园网短号 882999）\n"
    "• 咨询台：0754-8650 3247（校园网短号 883247）\n"
    "• 图书馆办公室：0754-8650 2021（校园网短号 882021）\n"
    "若遇人身安全、医疗或火警等突发情况且服务台暂未接通或处于非开馆时段，"
    "请立即联系学校保卫处或拨打 110 / 119 / 120。"
)

FEEDBACK_RECEIVED_REPLY = (
    "感谢您的意见与反馈！我们已将您的留言记录到服务台反馈看板，供馆员跟进与改进参考。"
    "如需咨询具体馆藏或借阅问题，欢迎随时继续发送文字消息。"
)

DEFAULT_GRAY_ENROLL_KEYWORD = ""

GRAY_ENROLL_ADDED_REPLY = (
    "已将您加入图书馆 AI 客服自动回复灰度测试白名单！接下来您可以直接发送图书馆相关问题进行测试。"
)

GRAY_ENROLL_ALREADY_REPLY = (
    "您已在图书馆 AI 客服自动回复灰度测试白名单中，可直接发送图书馆相关问题进行测试。"
)

_URL_ONLY_RE = re.compile(
    r"^(?:https?://\S+|www\.\S+)(?:\s+(?:https?://\S+|www\.\S+))*$",
    re.IGNORECASE,
)
_WECHAT_EMOJI_RE = re.compile(r"(?:\[[^\]]{1,12}\]|/::?[A-Za-z)!$]{1,4})")

_INVALID_EXACT_TOKENS = frozenset(
    {
        "你好",
        "您好",
        "你好呀",
        "你好啊",
        "在吗",
        "在不在",
        "有人吗",
        "有人在吗",
        "在么",
        "hi",
        "hello",
        "hey",
        "test",
        "testing",
        "测试",
        "收到",
        "谢谢",
        "多谢",
        "感谢",
        "ok",
        "好的",
        "好吧",
        "嗯",
        "嗯嗯",
        "哦",
        "哦哦",
        "哈喽",
        "1",
        "0",
    }
)

# Non-physical lock/account words must never trip the physical trapped emergency rule.
_LOCK_NON_EMERGENCY_CONTEXT_RE = re.compile(
    r"(?:储物柜|存包柜|柜子|账号|账户|密码|借阅卡|借书证|校园卡|读者证|一卡通|门禁卡|系统|电脑|手机)"
)
_PHYSICAL_TRAPPED_RE = re.compile(
    r"(?:被锁在(?:图书馆|馆里|馆内|自习室|研讨室|楼里|楼内|楼梯|里面|洗手间|厕所|馆内了)|"
    r"锁在(?:图书馆|馆里|馆内|自习室|研讨室|楼里|里面|厕所|洗手间)|"
    r"关在(?:图书馆|电梯|馆里|里面)|"
    r"(?:被困在|困在)(?:图书馆|电梯|自习室|里面|楼梯|厕所)|"
    r"闭馆.{0,8}(?:还在馆|出不去|锁在里面)|"
    r"门(?:反锁|打不开).{0,8}(?:出不去|出不来))"
)
_GENERAL_EMERGENCY_RE = re.compile(
    r"(?:救命|晕倒|昏倒|昏迷|突发疾病|呼吸困难|大量出血|严重受伤|急救|"
    r"着火|失火|起火|火灾|冒烟|浓烟|火警|"
    r"大量漏水|爆管|跑水|漏电|触电|"
    r"打架|持刀|袭击|恶意偷拍|正在骚扰)"
)

_PURCHASE_RECOMMENDATION_RE = re.compile(r"(?:荐购|推荐购买|推荐买|增订期刊|购买新书)")
_ACTIVE_INCIDENT_OR_QUESTION_RE = re.compile(
    r"(?:怎么|如何|请问|为什么|为何|在哪|几点|什么时候|能否|能不能|可不可以|可以吗|吗[？?]?$|"
    r"[？?]$|投诉|举报|占位|占座|吵死|扣费|扣钱|罚[款钱]|报错|打不开|扫不了|退钱|丢失|遗失)"
)
_FEEDBACK_POSITIVE_OR_SUGGESTION_RE = re.compile(
    r"(?:^(?:建议|反馈|意见|意见建议|提议|吐槽|表扬|点赞)[:：\s]*|"
    r"提个?建议|提个?意见|反映一下|反馈一下|给你们提|提一点建议|提点意见|"
    r"表扬(?:一下)?(?:图书馆|工作人员|馆员|阿姨)|建议(?:图书馆|贵馆|你们|二楼|三楼|东海岸|桑浦山)?(?:增加|增设|多加|多放|改进|调整|优化|改善|取消|开通|配备))"
)

_FOLLOW_UP_MARKERS = (
    "还是不行",
    "依然不行",
    "依旧不行",
    "试过了",
    "没用",
    "解决不了",
    "还是报错",
    "还是失败",
    "还是没收到",
    "还没收到",
    "还没好",
    "刚才",
    "刚刚",
    "补充一下",
    "补充说明",
    "还有",
    "我的是",
    "学号",
    "卡号",
    "姓名",
    "手机号",
    "截图",
    "还是那样",
    "现在还是",
)
_DISTINCT_NEW_TOPIC_RE = re.compile(
    r"(?:几点(?:开馆|关门|闭馆|开门)|开馆时间|闭馆时间|饮水机在哪里|WiFi|校园网|怎么借书|图书怎么预约|怎么荐购|推荐购买)"
)


@dataclass(frozen=True, slots=True)
class FilterDecision:
    """Outcome of classifying one inbound WeChat message or event."""

    category: str  # emergency | feedback | gray_enroll | unsupported | question | invalid
    reason: str
    normalized_text: str


def _normalize_for_match(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).strip()


def _strip_edge_punctuation(text: str) -> str:
    chars: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat.startswith(("P", "S", "C", "Z")):
            continue
        chars.append(ch)
    return "".join(chars)


def _normalize_keyword(text: str) -> str:
    """Normalize a short command keyword while keeping internal word boundaries intact."""
    normalized = _normalize_for_match(text)
    start = 0
    end = len(normalized)
    while start < end and unicodedata.category(normalized[start]).startswith(("P", "S", "C", "Z")):
        start += 1
    while end > start and unicodedata.category(normalized[end - 1]).startswith(("P", "S", "C", "Z")):
        end -= 1
    return " ".join(normalized[start:end].split()).casefold()


def _extract_cjk_bigrams(text: str) -> set[str]:
    compact = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text.lower())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[idx : idx + 2] for idx in range(len(compact) - 1)}


def classify_message(
    msg_type: str,
    content: str = "",
    event: str = "",
    *,
    gray_enroll_keyword: str = DEFAULT_GRAY_ENROLL_KEYWORD,
) -> FilterDecision:
    """Classify an inbound callback payload into Filter branches."""
    normalized_type = msg_type.strip().lower()
    normalized_event = event.strip()
    if normalized_type != "text":
        reason = f"wechat_event:{normalized_event or 'unknown'}" if normalized_type == "event" else f"media:{normalized_type or 'unknown'}"
        return FilterDecision(category="unsupported", reason=reason, normalized_text="")

    trimmed_text = " ".join(content.split())
    normalized_text = _normalize_for_match(content)
    if not normalized_text:
        return FilterDecision(category="invalid", reason="empty_text", normalized_text="")

    if _URL_ONLY_RE.match(normalized_text):
        return FilterDecision(category="unsupported", reason="text_only_url", normalized_text=trimmed_text)

    without_emoji = _normalize_for_match(_WECHAT_EMOJI_RE.sub("", normalized_text))
    if not without_emoji:
        return FilterDecision(category="invalid", reason="emoji_only", normalized_text=trimmed_text)

    core_token = _strip_edge_punctuation(without_emoji).lower()
    if not core_token:
        return FilterDecision(category="invalid", reason="punctuation_only", normalized_text=trimmed_text)

    enroll_target = _normalize_keyword(gray_enroll_keyword)
    if enroll_target and _normalize_keyword(without_emoji) == enroll_target:
        return FilterDecision(category="gray_enroll", reason="gray_allowlist_keyword", normalized_text=trimmed_text)

    if core_token in _INVALID_EXACT_TOKENS:
        return FilterDecision(category="invalid", reason="noise_or_greeting", normalized_text=trimmed_text)

    if _GENERAL_EMERGENCY_RE.search(normalized_text):
        return FilterDecision(category="emergency", reason="safety_or_medical_keyword", normalized_text=trimmed_text)

    if _PHYSICAL_TRAPPED_RE.search(normalized_text) and not _LOCK_NON_EMERGENCY_CONTEXT_RE.search(normalized_text):
        return FilterDecision(category="emergency", reason="physical_trapped", normalized_text=trimmed_text)

    if (
        _FEEDBACK_POSITIVE_OR_SUGGESTION_RE.search(normalized_text)
        and not _PURCHASE_RECOMMENDATION_RE.search(normalized_text)
        and not _ACTIVE_INCIDENT_OR_QUESTION_RE.search(normalized_text)
    ):
        return FilterDecision(category="feedback", reason="reader_suggestion", normalized_text=trimmed_text)

    return FilterDecision(category="question", reason="normal_text_question", normalized_text=trimmed_text)


def default_same_case_matcher(
    merged_text: str,
    open_case: dict[str, Any],
    *,
    now_epoch: float,
    continuation_window_seconds: float = 1800.0,
) -> tuple[bool, str]:
    """Decide whether a merged message continues an open staff case or starts a new question."""
    normalized = _normalize_for_match(merged_text)
    if not normalized:
        return False, "empty_text"

    for marker in _FOLLOW_UP_MARKERS:
        if marker in normalized:
            return True, "follow_up_marker"

    case_context = f"{open_case.get('summary', '')} {open_case.get('latest_message', '')}"
    msg_grams = _extract_cjk_bigrams(normalized)
    case_grams = _extract_cjk_bigrams(case_context)
    overlap = len(msg_grams & case_grams)
    if overlap >= 2 and not _DISTINCT_NEW_TOPIC_RE.search(normalized):
        return True, "topic_overlap"

    updated_at = open_case.get("updated_at_epoch")
    within_window = isinstance(updated_at, (int, float)) and (now_epoch - float(updated_at)) <= continuation_window_seconds
    if within_window and not _DISTINCT_NEW_TOPIC_RE.search(normalized) and len(normalized) <= 40:
        return True, "active_case_continuation"

    return False, "distinct_new_topic"
