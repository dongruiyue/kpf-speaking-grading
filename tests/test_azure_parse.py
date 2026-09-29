#!/usr/bin/env python3
"""离线单测：Azure 发音评估的**响应解析**与**时长/切段守卫**（不联网、不需要 key、秒级）

官方文档（2026-09-29 核对原文）
https://learn.microsoft.com/en-us/azure/ai-services/speech-service/rest-speech-to-text-short

  - 示例响应（"Here's a typical response for recognition with pronunciation assessment"）：
    AccuracyScore / FluencyScore / ProsodyScore / CompletenessScore / PronScore **直接挂在
    NBest[0] 上**，逐词的 AccuracyScore / ErrorType 直接挂在 `NBest[0].Words[]` 的元素上，
    **没有 PronunciationAssessment 这一层**。
  - 时长限制（"Before you use the Speech to text REST API for short audio…"）：
    "Requests that use the REST API for short audio and transmit audio directly can contain no
     more than 60 seconds of audio. **For pronunciation assessment, the audio duration should be
     no more than 30 seconds.**"

为什么只测这两块：这条链从没用真实 key 端到端跑过，**用 mock 假装 HTTP 成功证明不了链路真的通**；
能离线判定的是"官方文档给的响应必须解析得出来"与"超限的音频绝不发出去"这两件纯逻辑。

跑法：python3 tests/test_azure_parse.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from kpf_azure import (  # noqa: E402
    AZURE_MAX_SECONDS,
    aggregate_metrics,
    build_coverage,
    parse_azure_response,
    plan_spans,
    screen_spans,
    skip_note,
)
from kpf_pronounce import render as render_pronounce  # 渲染层也要看得到覆盖情况  # noqa: E402

# 官方文档示例响应原文（"Good morning."，含逐词 Prosody Feedback 块）。
# 只改动了缩进以适应 Python 字面量，键名与数值一字未动。
OFFICIAL_FLAT = {
    "RecognitionStatus": "Success",
    "Offset": 700000,
    "Duration": 8400000,
    "DisplayText": "Good morning.",
    "SNR": 38.76819,
    "NBest": [
        {
            "Confidence": 0.98503506,
            "Lexical": "good morning",
            "ITN": "good morning",
            "MaskedITN": "good morning",
            "Display": "Good morning.",
            "AccuracyScore": 100.0,
            "FluencyScore": 100.0,
            "ProsodyScore": 87.8,
            "CompletenessScore": 100.0,
            "PronScore": 95.1,
            "Words": [
                {
                    "Word": "good",
                    "Offset": 700000,
                    "Duration": 2600000,
                    "Confidence": 0.0,
                    "AccuracyScore": 100.0,
                    "ErrorType": "None",
                    "Feedback": {
                        "Prosody": {
                            "Break": {"ErrorTypes": ["None"], "BreakLength": 0},
                            "Intonation": {
                                "ErrorTypes": [],
                                "Monotone": {
                                    "Confidence": 0.0,
                                    "WordPitchSlopeConfidence": 0.0,
                                    "SyllablePitchDeltaConfidence": 0.91385907,
                                },
                            },
                        }
                    },
                },
                {
                    "Word": "morning",
                    "Offset": 3400000,
                    "Duration": 5700000,
                    "Confidence": 0.0,
                    "AccuracyScore": 100.0,
                    "ErrorType": "None",
                    "Feedback": {
                        "Prosody": {
                            "Break": {
                                "ErrorTypes": ["None"],
                                "UnexpectedBreak": {"Confidence": 3.5294118e-08},
                                "MissingBreak": {"Confidence": 1.0},
                                "BreakLength": 0,
                            },
                            "Intonation": {
                                "ErrorTypes": [],
                                "Monotone": {
                                    "Confidence": 0.0,
                                    "WordPitchSlopeConfidence": 0.0,
                                    "SyllablePitchDeltaConfidence": 0.91385907,
                                },
                            },
                        }
                    },
                },
            ],
        }
    ],
}


def flat_response(words, accuracy=100.0, fluency=100.0, prosody=87.8,
                  completeness=100.0, pron=95.1, include=("Display",)) -> dict:
    """造一份**扁平形态**的响应（只带要测的字段），字段名与官方示例一致。"""
    best = {"AccuracyScore": accuracy, "FluencyScore": fluency, "ProsodyScore": prosody,
            "CompletenessScore": completeness, "PronScore": pron, "Words": words}
    if "Display" in include:
        best["Display"] = "Good morning."
    return {"RecognitionStatus": "Success", "DisplayText": "Good morning.", "NBest": [best]}


def nested_response(words, accuracy=100.0, fluency=100.0, prosody=87.8,
                    completeness=100.0, pron=95.1) -> dict:
    """同一批分数的**嵌套 PronunciationAssessment 形态**（SDK / 旧示例的写法）。"""
    return {
        "RecognitionStatus": "Success",
        "DisplayText": "Good morning.",
        "NBest": [{
            "Confidence": 0.985,
            "Display": "Good morning.",
            "PronunciationAssessment": {
                "AccuracyScore": accuracy, "FluencyScore": fluency, "ProsodyScore": prosody,
                "CompletenessScore": completeness, "PronScore": pron, "ErrorType": "None",
            },
            "Words": list(words),
        }],
    }


def nested_words():
    return [
        {"Word": "good", "PronunciationAssessment": {"AccuracyScore": 100.0, "ErrorType": "None"}},
        {"Word": "morning", "PronunciationAssessment": {"AccuracyScore": 100.0, "ErrorType": "None"}},
    ]


class TestParseOfficialSample(unittest.TestCase):
    """官方示例响应（扁平形态）必须解析得出来——这是 Astra 指出的第 1 个缺陷。"""

    def test_flat_scores(self):
        res = parse_azure_response(OFFICIAL_FLAT)
        self.assertEqual(res["accuracy"], 100.0)
        self.assertEqual(res["fluency"], 100.0)
        self.assertEqual(res["prosody"], 87.8)
        self.assertEqual(res["completeness"], 100.0)
        self.assertEqual(res["pron_score"], 95.1)
        self.assertEqual(res["raw_text"], "Good morning.")

    def test_flat_band_uses_shared_to_band(self):
        res = parse_azure_response(OFFICIAL_FLAT)
        # 95.1 → ≥90 档 = 5；映射只有 to_band() 一份实现，本模块不另写一份
        self.assertEqual(res["score_0_5"], 5)
        self.assertIn("语音评测引擎", res["band_source"])

    def test_flat_words(self):
        res = parse_azure_response(OFFICIAL_FLAT)
        self.assertEqual([w["w"] for w in res["weakest_words"]], ["good", "morning"])
        self.assertEqual({w["error_type"] for w in res["weakest_words"]}, {"None"})


class TestParseNestedForm(unittest.TestCase):
    """嵌套 PronunciationAssessment 形态也要认（SDK 写法）——两种形态都得支持。"""

    def test_nested_equals_flat(self):
        flat = parse_azure_response(OFFICIAL_FLAT)
        nested = parse_azure_response(nested_response(nested_words()))
        for key in ("accuracy", "fluency", "prosody", "completeness",
                    "pron_score", "score_0_5", "band_source", "weakest_words"):
            self.assertEqual(nested[key], flat[key], key)

    def test_nested_word_scores(self):
        res = parse_azure_response(nested_response(nested_words()))
        self.assertEqual(res["weakest_words"][0], {"w": "good", "accuracy": 100.0, "error_type": "None"})


class TestParseErrors(unittest.TestCase):
    """两种形态都没有时必须明确报错，且错误里要带上**实际收到的键**（便于对着真实响应改）。"""

    def test_no_nbest_lists_top_keys(self):
        data = {"RecognitionStatus": "NoMatch", "Offset": 0, "Duration": 0, "DisplayText": ""}
        with self.assertRaises(RuntimeError) as ctx:
            parse_azure_response(data)
        msg = str(ctx.exception)
        self.assertIn("NBest", msg)
        for key in ("RecognitionStatus", "DisplayText", "Duration", "Offset"):
            self.assertIn(key, msg, f"错误信息里应打出实际顶层键 {key}")

    def test_nbest_without_any_score_lists_keys(self):
        data = {"RecognitionStatus": "Success", "NBest": [{"Confidence": 0.9, "Display": "hi", "Words": []}]}
        with self.assertRaises(RuntimeError) as ctx:
            parse_azure_response(data)
        msg = str(ctx.exception)
        self.assertIn("NBest[0]", msg)
        self.assertIn("Confidence", msg)
        self.assertIn("Display", msg)

    def test_not_a_dict(self):
        with self.assertRaises(RuntimeError):
            parse_azure_response(["unexpected"])


class TestParseWords(unittest.TestCase):
    """逐词解析：排序、ErrorType、缺分不算 0。"""

    def test_weakest_words_sorted_and_error_type_kept(self):
        words = [
            {"Word": "morning", "AccuracyScore": 82.0, "ErrorType": "Mispronunciation"},
            {"Word": "good", "AccuracyScore": 55.5, "ErrorType": "Omission"},
            {"Word": "the", "AccuracyScore": 100.0, "ErrorType": "None"},
            {"Word": "a", "ErrorType": "Insertion"},  # 没有分：不该进最弱词表
        ]
        res = parse_azure_response(flat_response(words))
        self.assertEqual([(w["w"], w["accuracy"], w["error_type"]) for w in res["weakest_words"]],
                         [("good", 55.5, "Omission"), ("morning", 82.0, "Mispronunciation"),
                          ("the", 100.0, "None")])

    def test_weakest_words_capped_at_12(self):
        words = [{"Word": f"w{i}", "AccuracyScore": float(i), "ErrorType": "None"} for i in range(20)]
        res = parse_azure_response(flat_response(words))
        self.assertEqual(len(res["weakest_words"]), 12)
        self.assertEqual(res["weakest_words"][0]["w"], "w0")

    def test_missing_pronscore_is_not_zero(self):
        # 有 Accuracy 但没 PronScore：不算"解析失败"，但发音分必须是"未取得"而不是 0
        res = parse_azure_response(flat_response([], pron=None, include=()))
        self.assertIsNone(res["pron_score"])
        self.assertIsNone(res["score_0_5"])
        self.assertEqual(res["accuracy"], 100.0)


def two_question_words(second_start: float = 20.0, tail_end: float = 26.0) -> list:
    """最小可用转写夹具：Q1 题干 4 词 + 答案 4 词 + Q2 题干 4 词 + 答案 3 词。

    为什么题干只放 4 词：对齐算法会在相似度 ≥0.9 时提前收（01-task-map.md 5.1 的边界假阳性），
    而"题干前多一个答案词"的窗口相似度正好是 2n/(2n+1)——n≥5 时 ≥0.9 会提前收、n=4 时 0.89 不会。
    这里要测的是 `plan_spans()` 的接线（段边界=题干起点），不是对齐算法本身，所以让边界落在真起点上。
    """
    return [
        {"w": "Where", "start": 0.0, "end": 0.4},
        {"w": "do", "start": 0.4, "end": 0.6},
        {"w": "you", "start": 0.6, "end": 0.8},
        {"w": "live?", "start": 0.8, "end": 1.3},
        {"w": "I", "start": 2.0, "end": 2.3},
        {"w": "live", "start": 2.3, "end": 2.9},
        {"w": "in", "start": 2.9, "end": 3.1},
        {"w": "Shenzhen.", "start": 3.1, "end": 3.9},
        {"w": "Do", "start": second_start, "end": second_start + 0.3},
        {"w": "you", "start": second_start + 0.3, "end": second_start + 0.5},
        {"w": "like", "start": second_start + 0.5, "end": second_start + 0.9},
        {"w": "sport?", "start": second_start + 0.9, "end": second_start + 1.5},
        {"w": "Yes", "start": tail_end - 1.0, "end": tail_end - 0.6},
        {"w": "I", "start": tail_end - 0.6, "end": tail_end - 0.4},
        {"w": "do.", "start": tail_end - 0.4, "end": tail_end},
    ]


QUESTIONS = ["Where do you live?", "Do you like sport?"]


class TestPlanSpans(unittest.TestCase):
    """切段计划：按题切（复用题目对齐）、无题目切窗口、没有时间戳就明确失败。"""

    def test_by_questions(self):
        spans, warnings = plan_spans({"meta": {"duration": 26.0}, "words": two_question_words()},
                                     QUESTIONS)
        self.assertEqual([s["label"] for s in spans], ["Q1", "Q2"])
        self.assertEqual([(s["start"], s["end"], s["seconds"]) for s in spans],
                         [(0.0, 20.0, 20.0), (20.0, 26.0, 6.0)])
        self.assertFalse(any(s["too_long"] for s in spans))
        self.assertEqual(warnings, [])   # 开头没有被漏掉的音频

    def test_by_questions_reports_uncovered_head(self):
        shifted = [dict(t, start=t["start"] + 3, end=t["end"] + 3) for t in two_question_words()]
        words = [{"w": "Hello", "start": 0.0, "end": 1.0}] + shifted
        spans, warnings = plan_spans({"words": words}, QUESTIONS)
        self.assertEqual(spans[0]["start"], 3.0)
        self.assertTrue(any("未纳入评测" in w for w in warnings))

    def test_no_questions_windows(self):
        payload = {"meta": {"duration": 61.2},
                   "segments": [{"start": 0.0, "end": 20.0, "text": "a"},
                                {"start": 20.0, "end": 61.2, "text": "b"}]}
        spans, warnings = plan_spans(payload)
        self.assertEqual([s["seconds"] for s in spans], [20.0, 41.2])
        self.assertEqual(spans[0]["too_long"], False)
        self.assertEqual(spans[1]["too_long"], True)   # 41.2 > 30：守卫会在发送前剔除
        self.assertTrue(any("--questions" in w for w in warnings))

    def test_windows_from_word_timestamps(self):
        payload = {"words": [{"w": "a", "start": 0.0, "end": 5.0},
                             {"w": "b", "start": 5.0, "end": 35.0},
                             {"w": "c", "start": 35.0, "end": 40.0}]}
        spans, _w = plan_spans(payload)
        self.assertEqual([s["seconds"] for s in spans], [5.0, 30.0, 5.0])
        self.assertFalse(any(s["too_long"] for s in spans))

    def test_no_timestamps_fails_with_actions(self):
        for payload in ({"meta": {"duration": 140.4}, "words": [{"w": "hello"}]}, {"words": []}, {}):
            with self.assertRaises(RuntimeError) as ctx:
                plan_spans(payload)
            msg = str(ctx.exception)
            self.assertIn("30", msg)
            self.assertIn("xfyun", msg)
            self.assertIn("--media", msg)

    def test_no_timestamps_message_mentions_duration(self):
        with self.assertRaises(RuntimeError) as ctx:
            plan_spans({"meta": {"duration": 140.4}, "words": [{"w": "hello"}]})
        self.assertIn("140", str(ctx.exception))

    def test_questions_without_words_fails(self):
        with self.assertRaises(RuntimeError):
            plan_spans({"words": []}, QUESTIONS)


class TestDurationGuard(unittest.TestCase):
    """超限的段绝不发出去：被跳过、并出现在 coverage 里。"""

    def test_overlong_segment_skipped_and_recorded(self):
        payload = {"meta": {"duration": 61.2},
                   "segments": [{"start": 0.0, "end": 20.0, "text": "a"},
                                {"start": 20.0, "end": 61.2, "text": "b"}]}
        spans, _w = plan_spans(payload)
        sendable, skipped = screen_spans(spans)

        self.assertEqual([s["index"] for s in sendable], [1])          # 41.2 秒那段不发
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["index"], 2)
        self.assertEqual(skipped[0]["seconds"], 41.2)
        self.assertIn("超过接口上限", skipped[0]["why"])

        cov = build_coverage(spans, skipped)
        self.assertEqual(cov["segments_total"], 2)
        self.assertEqual(cov["segments_scored"], 1)
        self.assertEqual(cov["skipped"][0]["index"], 2)

    def test_per_question_segment_over_limit_is_skipped(self):
        # 真实样本形态：整页 5 题，其中某题连题带答超过 30 秒 → 该题跳过并记入 coverage
        spans, _w = plan_spans({"meta": {"duration": 51.0},
                                "words": two_question_words(second_start=45.0, tail_end=51.0)},
                               QUESTIONS)
        self.assertEqual([s["seconds"] for s in spans], [45.0, 6.0])
        sendable, skipped = screen_spans(spans)
        self.assertEqual([s["label"] for s in sendable], ["Q2"])
        self.assertEqual([s["label"] for s in skipped], ["Q1"])
        self.assertIn("30", skipped[0]["why"])

    def test_every_sendable_span_within_limit(self):
        # 140.4 秒的整页作业：切片后**没有任何一段**超过上限
        payload = {"meta": {"duration": 140.4},
                   "segments": [{"start": i * 28.0, "end": (i + 1) * 28.0, "text": "x"} for i in range(5)]}
        spans, _w = plan_spans(payload)
        sendable, skipped = screen_spans(spans)
        self.assertTrue(sendable)
        for span in sendable:
            self.assertLessEqual(span["seconds"], AZURE_MAX_SECONDS)
        self.assertEqual(build_coverage(spans, skipped)["segments_scored"], len(sendable))

    def test_failed_segment_uses_same_note_shape(self):
        # 调用失败（网络/接口报错）也用同一种记录形状进 coverage，教师能看到缺了哪段
        span = plan_spans({"segments": [{"start": 0.0, "end": 10.0, "text": "x"}]})[0][0]
        note = skip_note(span, "调用失败：Azure 返回 401")
        cov = build_coverage([span], [note])
        self.assertEqual(cov["segments_scored"], 0)
        self.assertEqual(cov["skipped"][0]["why"], "调用失败：Azure 返回 401")


class TestAggregate(unittest.TestCase):
    """多段聚合：平均 → 统一 to_band()。"""

    def test_average_and_band(self):
        a = parse_azure_response(flat_response([], accuracy=100.0, pron=95.1))
        b = parse_azure_response(flat_response([], accuracy=80.0, pron=84.9))
        merged = aggregate_metrics([a, b])
        self.assertEqual(merged["pron_score"], 90.0)      # (95.1 + 84.9) / 2
        self.assertEqual(merged["score_0_5"], 5)          # 边界 90 → 5，走的是 to_band()
        self.assertEqual(merged["accuracy"], 90.0)

    def test_weakest_words_merged_keeping_lowest(self):
        a = parse_azure_response(flat_response([{"Word": "think", "AccuracyScore": 70.0,
                                                 "ErrorType": "Mispronunciation"}]))
        b = parse_azure_response(flat_response([{"Word": "think", "AccuracyScore": 40.0,
                                                 "ErrorType": "Mispronunciation"},
                                                {"Word": "three", "AccuracyScore": 60.0,
                                                 "ErrorType": "Mispronunciation"}]))
        merged = aggregate_metrics([a, b])
        self.assertEqual([(w["w"], w["accuracy"]) for w in merged["weakest_words"]],
                         [("think", 40.0), ("three", 60.0)])
        self.assertEqual(merged["raw_text"], "Good morning. Good morning.")

    def test_no_rows_fails(self):
        with self.assertRaises(RuntimeError):
            aggregate_metrics([])


class TestRenderShowsCoverage(unittest.TestCase):
    """覆盖情况必须写进 `发音.md`——教师要知道"这个分覆盖了哪几段"。"""

    PAYLOAD = {"meta": {"file": "fixture://anonymous/demo-lesson", "duration": 160.4}}

    def _result(self, skipped, scored=6, total=7):
        result = aggregate_metrics([parse_azure_response(OFFICIAL_FLAT)])
        result.update({
            "provider": "azure",
            "coverage": {"segments_total": total, "segments_scored": scored, "skipped": skipped},
            "segments": [{"index": 1, "label": "Q1", "start": 7.2, "end": 28.7, "seconds": 21.5,
                          "pron_score": 95.1, "accuracy": 100.0}],
            "align_warnings": [],
            "errors": [],
            "note": f"按段提交（共 {total} 段，每段不超过 30 秒）",
        })
        return result

    def test_skipped_segment_is_visible(self):
        md = render_pronounce(self.PAYLOAD, self._result(
            [{"index": 3, "label": "Q3", "seconds": 40.0, "why": "超过接口上限 30 秒（本段 40.0 秒）"}]))
        self.assertIn("覆盖情况", md)
        self.assertIn("全篇切成 **7 段**", md)
        self.assertIn("已取得分数 **6 段**", md)
        self.assertIn("**Q3（第 3 段，40.0 秒）未覆盖**", md)
        self.assertIn("不代表整篇作业的全部内容", md)
        self.assertIn("| Q1 | 00:07.2–00:28.7 | 21.5s | 95.1 | 100.0 |", md)
        self.assertIn("5 / 5", md)          # 95.1 → 5/5，映射仍走 to_band()

    def test_full_coverage_has_no_warning(self):
        md = render_pronounce(self.PAYLOAD, self._result([], scored=7))
        self.assertIn("已取得分数 **7 段**", md)
        self.assertNotIn("不代表整篇作业的全部内容", md)
        self.assertNotIn("未覆盖**", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
