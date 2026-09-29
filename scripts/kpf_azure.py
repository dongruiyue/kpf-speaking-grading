#!/usr/bin/env python3
"""KPF 口语作业 · Azure 发音评估的**纯逻辑**：响应解析 + 时长/切段守卫

本模块不 import requests / av / numpy，不发请求、不解音频——那是调用方
（`kpf_pronounce.provider_azure()`）的事。这样 `tests/test_azure_parse.py` 能在系统
python3 上离线跑：**响应解析**与**哪些段允许发出去**这两块逻辑不需要 key、不需要网络。

两条硬事实，都来自官方文档
https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-speech-to-text-short
（2026-09-29 核对原文，两处都是外部审查 Astra 指出的）：

  1. **时长上限**（"Before you use the Speech to text REST API for short audio…" 一节原文）：
     "Requests that use the REST API for short audio and transmit audio directly can contain
      no more than 60 seconds of audio. **For pronunciation assessment, the audio duration
      should be no more than 30 seconds.**"
     所以客户端 requests 的 timeout 治不了这个上限——超限的段必须**不发**。
  2. **分数位置**（"Here's a typical response for recognition with pronunciation assessment"
     示例）：AccuracyScore / FluencyScore / ProsodyScore / CompletenessScore / PronScore
     直接挂在 `NBest[0]` 上，逐词的 AccuracyScore / ErrorType 直接挂在 `NBest[0].Words[]`
     的元素上，**没有 `PronunciationAssessment` 这一层**。
     SDK 与部分旧示例用的是嵌套 `PronunciationAssessment` 形态，所以两种都认：
     先取扁平字段，再回退嵌套。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kpf_xfyun import avg_of, to_band  # 0–5 映射与平均值的唯一实现，本模块不另写一份  # noqa: E402

# 接口对发音评估音频的硬上限（秒）。官方原文：pronunciation assessment 的音频不超过 30 秒。
AZURE_MAX_SECONDS = 30.0
# 单个请求的客户端超时（秒）：单段 ≤30 秒，留 4 倍余量足够覆盖上传 + 识别。
# **它不是时长上限**，调长只会让坏请求挂更久；音频能不能发由 AZURE_MAX_SECONDS 管。
AZURE_TIMEOUT = 120
BAND_SOURCE = "语音评测引擎（AI 预估，非官方考官分）"

_SCORE_FIELDS = (
    ("AccuracyScore", "accuracy"),
    ("FluencyScore", "fluency"),
    ("ProsodyScore", "prosody"),
    ("CompletenessScore", "completeness"),
    ("PronScore", "pron_score"),
)


# ---------------------------------------------------------------- 响应解析

def _pick(flat: dict, nested: dict, key: str):
    """先取扁平字段（官方示例的形态），没有再回退 PronunciationAssessment（SDK 形态）。"""
    val = flat.get(key)
    return nested.get(key) if val is None else val


def parse_azure_response(data: dict) -> dict:
    """Azure 短音频 REST 的响应 JSON → 统一结构（与 provider 的返回字段一致，单段一份）。

    两种形态都支持（都从官方/SDK 示例抄来的）：`NBest[0]` 上的扁平字段，以及
    `NBest[0].PronunciationAssessment` 里的同名字段。两种都取不到分数就报错，并把
    **实际收到的顶层键与 NBest[0] 的键**打出来——这条链还没用真实 key 端到端跑过，
    报错里带上真实键名，下次对着改比重读文档快。
    """
    if not isinstance(data, dict):
        raise RuntimeError(f"Azure 返回的不是 JSON 对象，而是 {type(data).__name__}，无法解析。")

    nbest = data.get("NBest")
    best = nbest[0] if isinstance(nbest, list) and nbest and isinstance(nbest[0], dict) else None
    top_keys = ", ".join(sorted(str(k) for k in data)) or "（空对象）"
    if best is None:
        raise RuntimeError(
            f"Azure 响应里没有 NBest（顶层 RecognitionStatus={data.get('RecognitionStatus')!r}）。"
            f"实际顶层键：{top_keys}"
        )

    nested = best.get("PronunciationAssessment")
    nested = nested if isinstance(nested, dict) else {}
    scores = {out: _pick(best, nested, key) for key, out in _SCORE_FIELDS}
    if all(v is None for v in scores.values()):
        raise RuntimeError(
            "Azure 响应里既没有 NBest[0] 的扁平分数（AccuracyScore/FluencyScore/ProsodyScore/"
            "CompletenessScore/PronScore），也没有 NBest[0].PronunciationAssessment。"
            f"实际顶层键：{top_keys}；NBest[0] 的键：{', '.join(sorted(str(k) for k in best)) or '（空）'}"
        )

    words = []
    for w in best.get("Words") or []:
        if not isinstance(w, dict):
            continue
        wa = w.get("PronunciationAssessment")
        wa = wa if isinstance(wa, dict) else {}
        words.append({
            "w": w.get("Word"),
            "accuracy": _pick(w, wa, "AccuracyScore"),
            "error_type": _pick(w, wa, "ErrorType"),
        })
    weakest = sorted((w for w in words if w["accuracy"] is not None),
                     key=lambda x: x["accuracy"])[:12]

    return {
        "accuracy": scores["accuracy"],
        "fluency": scores["fluency"],
        "prosody": scores["prosody"],
        "completeness": scores["completeness"],
        "pron_score": scores["pron_score"],
        "score_0_5": to_band(scores["pron_score"]),
        "band_source": BAND_SOURCE,
        "weakest_words": weakest,
        "raw_text": best.get("Display") or data.get("DisplayText"),
    }


def aggregate_metrics(rows: list[dict]) -> dict:
    """多段结果 → 一份结果：各维度取**已评段**的平均，PronScore 平均后再 `to_band()`。

    平均值用 `kpf_xfyun.avg_of`（本 skill 唯一一份实现）；某维度一个有效值都没有时返回 None，
    调用方必须显示"未取得"，不得当 0 分。最弱词跨段合并，同一个词取最低分。
    """
    if not rows:
        raise RuntimeError("没有任何已评段，聚合不出分数。")
    pron = avg_of(rows, "pron_score")
    words: dict = {}
    for row in rows:
        for w in row.get("weakest_words") or []:
            if w.get("accuracy") is None:
                continue
            cur = words.get(w["w"])
            if cur is None or w["accuracy"] < cur["accuracy"]:
                words[w["w"]] = w
    return {
        "accuracy": avg_of(rows, "accuracy"),
        "fluency": avg_of(rows, "fluency"),
        "prosody": avg_of(rows, "prosody"),
        "completeness": avg_of(rows, "completeness"),
        "pron_score": pron,
        "score_0_5": to_band(pron),
        "band_source": BAND_SOURCE,
        "weakest_words": sorted(words.values(), key=lambda x: x["accuracy"])[:12],
        "raw_text": " ".join(r["raw_text"] for r in rows if r.get("raw_text")),
    }


# ---------------------------------------------------------------- 切段计划（纯时间戳，不碰音频）

def _has_time(obj) -> bool:
    return isinstance(obj, dict) and obj.get("start") is not None and obj.get("end") is not None


def _window(label: str, index: int, start: float, end: float) -> dict:
    """一段待评测区间。seconds 按**取整后**的起止算——切片用的就是这两个值，报的数必须一致。"""
    lo, hi = round(float(start), 2), round(float(end), 2)
    seconds = round(max(0.0, hi - lo), 2)
    return {
        "index": index,
        "label": label,
        "start": lo,
        "end": hi,
        "seconds": seconds,
        "too_long": seconds > AZURE_MAX_SECONDS,
    }


def _no_timestamp_hint(payload: dict) -> str:
    dur = (payload.get("meta") or {}).get("duration")
    head = f"这段音频 {float(dur):.0f} 秒" if isinstance(dur, (int, float)) else "这段音频"
    return (
        f"转写里没有可用的词级/分段级时间戳，无法按段切分。{head}，而该接口对发音评估的音频上限是 "
        f"{AZURE_MAX_SECONDS:.0f} 秒，整段上传必然超限——所以**不会**退回整段上传。可执行的替代方案："
        "① `--provider xfyun`（评念题部分，已验证）；"
        "② 换会写词级时间戳的引擎重跑转写（`kpf_asr.py --engine local`）；"
        "③ 先按题把音频切成 ≤30 秒的片段，再用 `--media` 逐个跑。"
    )


def _spans_by_questions(words: list[dict], questions: list[str]) -> tuple[list[dict], list[str]]:
    """一题一段：题干起点 → 下一题题干起点（未定位的题不占段），最后一段到词末尾。

    用 `kpf_analyze.split_by_questions()`——与讯飞链路同一份题目对齐，不另写一套。
    段里**含答题**（Azure 是 unscripted，可评自由说的部分）；末边界取"下一题起点"而不是
    "本题答案末边界"，因为后者在题目未定位时不可靠（见 01-task-map.md 5.1）。
    """
    from kpf_analyze import split_by_questions

    spans_raw, warnings = split_by_questions(words, questions)
    starts = []
    for qi, (qspan, _aspan, _diff) in enumerate(spans_raw, 1):
        if not qspan:
            warnings.append(f"第 {qi} 题没在转写里定位到：这一段不在发音分的覆盖范围内，请人工核对。")
            continue
        starts.append((qi, float(qspan[0]["start"])))
    if not starts:
        raise RuntimeError(
            "标准题目一条都没对齐上转写内容，无法按题切段。请核对题目文件与转写是否同一次作业"
            "（对齐提示见上）。"
        )

    end_all = max(float(t["end"]) for t in words)
    spans = []
    for k, (qi, start) in enumerate(starts):
        end = starts[k + 1][1] if k + 1 < len(starts) else end_all
        spans.append(_window(f"Q{qi}", len(spans) + 1, start, end))
    head = min(float(t["start"]) for t in words)
    if head < spans[0]["start"] - 0.5:
        warnings.append(
            f"开头 {spans[0]['start'] - head:.1f} 秒在 Q1 之前（开场白），未纳入评测。"
        )
    return spans, warnings


def _spans_by_windows(payload: dict) -> tuple[list[dict], list[str]]:
    """没给标准题目时：把转写的 segments（其次词级时间戳）贪心合并成 ≤30 秒的窗口。

    窗口边界与题目无关，只是为了让"整段 >30 秒"这件事变成"多段都不超限"；
    要按题看分就给 `--questions`。
    """
    units, source = [], ""
    for seg in payload.get("segments") or []:
        if _has_time(seg):
            units.append((float(seg["start"]), float(seg["end"])))
    if units:
        source = "转写分段"
    else:
        for w in payload.get("words") or []:
            if _has_time(w):
                units.append((float(w["start"]), float(w["end"])))
        if units:
            source = "词级时间戳"
    if not units:
        raise RuntimeError(_no_timestamp_hint(payload))

    spans, cur_start, cur_end = [], None, None
    for start, end in units:
        if cur_start is None:
            cur_start, cur_end = start, end
        elif end - cur_start <= AZURE_MAX_SECONDS:
            cur_end = end
        else:
            spans.append(_window(f"段{len(spans) + 1}", len(spans) + 1, cur_start, cur_end))
            cur_start, cur_end = start, end
    spans.append(_window(f"段{len(spans) + 1}", len(spans) + 1, cur_start, cur_end))
    return spans, [
        f"没给 --questions：按{source}切成 {len(spans)} 个 ≤{AZURE_MAX_SECONDS:.0f} 秒的窗口，"
        "边界与题目边界无关。要按题看分请加 --questions questions/<页号>.txt。"
    ]


def plan_spans(payload: dict, questions: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """把一次作业切成待评测的音频区间（每段一个请求）。返回 (段列表, 对齐/切分提示)。

    纯函数：只读转写 JSON 里的时间戳，不解音频、不联网、不需要 key。
    拿不到任何时间戳时**明确 raise**，绝不退回整段上传。
    """
    words = [w for w in payload.get("words") or [] if _has_time(w)]
    if questions:
        # 按题对齐要用**全部**词元（对齐不依赖时间戳），但"能不能切段"看的是有没有时间戳
        if not words:
            raise RuntimeError(_no_timestamp_hint(payload))
        return _spans_by_questions(payload["words"], questions)
    return _spans_by_windows(payload)


# ---------------------------------------------------------------- 发送前的守卫与覆盖情况

def too_long_why(span: dict) -> str:
    return (f"超过接口上限 {AZURE_MAX_SECONDS:.0f} 秒（本段 {span['seconds']:.1f} 秒）"
            "——官方文档：发音评估的音频不超过 30 秒")


def skip_note(span: dict, why: str) -> dict:
    """未覆盖段的记录（超限与被调用失败共用一种形状，教师一眼看出缺了哪段）。"""
    return {"index": span["index"], "label": span["label"], "seconds": span["seconds"], "why": why}


def screen_spans(spans: list[dict]) -> tuple[list[dict], list[dict]]:
    """时长守卫：返回 (允许发送的段, 直接跳过的段)。

    超限的段**绝不发出去**——接口对发音评估的音频上限是 30 秒，客户端 timeout 改多大都不改变
    这一点。跳过的段会原样进 coverage.skipped。
    """
    sendable = [s for s in spans if not s["too_long"]]
    skipped = [skip_note(s, too_long_why(s)) for s in spans if s["too_long"]]
    return sendable, skipped


def build_coverage(spans: list[dict], skipped: list[dict]) -> dict:
    """覆盖情况：教师必须知道这个分是"哪几段"的平均，以及哪几段没评。"""
    return {
        "segments_total": len(spans),
        "segments_scored": len(spans) - len(skipped),
        "skipped": skipped,
    }
