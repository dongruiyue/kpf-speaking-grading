#!/usr/bin/env python3
"""KPF 口语作业 · 切问答 + 算指标 + 出批改底稿

输入：kpf_asr.py 产出的统一 schema JSON
输出：固定格式的批改底稿 markdown（指标数字由脚本算，口径跨批次一致）

用法：
  kpf_analyze.py <转写.json> [--questions 题目.txt] [--out 底稿.md]
  kpf_analyze.py <转写.json> --questions q.txt --student 学生甲 --class FCE-A --level FCE

两条切分路径：
  1. --questions 给出标准题目（推荐）：逐题匹配，顺带产出「学生念题 vs 标准题目」的差异
  2. 不给题目：按问号 + 句首词启发式自动切分，输出后必须人工核对边界
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

PAUSE_MIN = 0.4          # 计入停顿的间隔（秒）
PAUSE_LONG = 1.0         # 长停顿阈值
LOW_P = 0.5              # 词级置信度低阈值
FILLERS = {"um", "uh", "er", "erm", "ah", "eh", "hmm", "mmm", "uhm"}
FUNCTION_WORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with",
    "is", "am", "are", "was", "were", "be", "been", "do", "does", "did", "i", "you",
    "he", "she", "it", "we", "they", "my", "your", "his", "her", "its", "our", "their",
    "that", "this", "these", "those", "as", "so", "if", "not", "no", "yes", "well",
}
QUESTION_STARTERS = {
    "what", "what's", "where", "when", "which", "who", "whose", "why", "how", "how's",
    "do", "does", "did", "have", "has", "had", "is", "are", "was", "were", "can",
    "could", "would", "will", "shall", "should", "may", "might", "must",
}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9']", "", str(text).lower())


def mmss(t: float) -> str:
    """秒 → `mm:ss.s` 时间戳（本 skill 唯一实现，其他脚本从这里 import）。

    先按 0.1 秒进位再拆分钟，否则 59.97 会输出 `00:60.0`、119.99 输出 `01:60.0`
    这种无效时间戳（教师要靠它直接跳位）。
    """
    total = round(float(t), 1)
    minutes, seconds = divmod(total, 60)
    return f"{int(minutes):02d}:{seconds:04.1f}"


def span_metrics(tokens: list[dict]) -> dict:
    if not tokens:
        return {}
    start, end = tokens[0]["start"], tokens[-1]["end"]
    dur = end - start
    pauses, prev_end = [], None
    for tok in tokens:
        if prev_end is not None and tok["start"] - prev_end >= PAUSE_MIN:
            pauses.append(round(tok["start"] - prev_end, 2))
        prev_end = tok["end"]
    words = [t["w"] for t in tokens]
    fillers = [w for w in words if norm(w) in FILLERS]
    return {
        "n": len(tokens),
        "start": round(start, 2),
        "end": round(end, 2),
        "dur": round(dur, 2),
        "wpm": round(len(tokens) / dur * 60) if dur > 0 else 0,
        "pauses": pauses,
        "long_pauses": [p for p in pauses if p >= PAUSE_LONG],
        "max_pause": max(pauses) if pauses else 0.0,
        "fillers": fillers,
    }


WINDOW_RATIO = 0.55  # 题目对齐的最低相似度


def _match_question(tokens: list[dict], targets: list[str], start: int,
                    slack: int = 3, strong: float = 0.9) -> tuple[int | None, int | None, float]:
    """把标准题目对齐到转写里的一段，返回 (题干起点, 题干终点, 相似度)。

    四个必需的设计（每一个都被真实数据打回过一次）：
    1. **不依赖 ASR 的标点**——Whisper 常不给念出来的问题加问号（实测 5 题里 3 题没有），
       所以题干终点由"最佳匹配窗口的右端"决定，而不是去找 `?`；
    2. **窗口长度受限**（题目词数 ±slack）——否则起点可以前移、把答案吞进题干；
    3. **滑动窗口 + 相似度**——学生念题常改词（`Which` 念成 `We each`），逐字匹配会整段失配；
    4. **提前收的阈值必须高（0.9）**——阈值放宽到 0.75 时，前面一个"勉强像"的位置会抢掉
       后面"更像"的真位置（实测把答案段切少了 4 个词）。够像才提前收，否则取全局最佳。
    """
    qlen = len(targets)
    best: tuple[int | None, int | None, float] = (None, None, 0.0)
    for i in range(start, max(start + 1, len(tokens) - 2)):
        for j in range(i + 2, min(i + qlen + slack, len(tokens))):
            window = [norm(t["w"]) for t in tokens[i:j + 1] if norm(t["w"])]
            if len(window) < 3:
                continue
            ratio = difflib.SequenceMatcher(a=targets, b=window).ratio()
            if ratio > best[2]:
                best = (i, j, ratio)
        if best[2] >= strong and best[0] == i:
            break
    return best


def split_by_questions(tokens: list[dict], questions: list[str]) -> tuple[list[dict], list[str]]:
    """按标准题目定位每题的边界。返回 [(问题span, 答案span, 念题差异)] 与对齐警告。"""
    qtok = [[norm(w) for w in q.split() if norm(w)] for q in questions]
    warnings: list[str] = []
    marks: list[tuple[int, int, float] | None] = []
    cursor = 0

    for idx, (question, targets) in enumerate(zip(questions, qtok)):
        found, q_end, ratio = _match_question(tokens, targets, cursor)
        if found is None or ratio < WINDOW_RATIO:
            warnings.append(
                f"第 {idx + 1} 题对齐度偏低（{ratio:.0%}），已跳过；该题答案边界改由相邻题目推断，请人工核对："
                f"{question[:40]}…"
            )
            marks.append(None)
            continue
        marks.append((found, q_end, ratio))
        cursor = q_end + 1

    spans = []
    for idx, question in enumerate(questions):
        mark = marks[idx]
        if mark is None:
            spans.append((None, None, "未定位"))
            continue
        found, q_end, ratio = mark
        # 答案末边界 = 下一道「定位成功」题目的起点；后续全失败才落到文件末尾，并明确警告
        nxt = next((m[0] for m in marks[idx + 1:] if m is not None), None)
        if nxt is None:
            nxt = len(tokens)
            if any(m is None for m in marks[idx + 1:]):
                warnings.append(f"第 {idx + 1} 题之后有题目未定位，该题答案末边界不可靠，请人工核对。")
        spoken = [tokens[j]["w"] for j in range(found, q_end + 1)]
        diff = f"{diff_reading(question, spoken)}（题目对齐度 {ratio:.0%}）"
        spans.append((tokens[found:q_end + 1], tokens[q_end + 1:nxt], diff))
    return spans, warnings


def diff_reading(standard: str, spoken: list[str]) -> str:
    """比对标准题目与学生念出的题干。

    **只在"匹配窗口内部"计算差异**：窗口首尾若出现"纯插入"（相邻答案的尾部串进窗口），
    先剔除再比对——否则上一题答案末尾的词会被误报成"多读"（实测 Q1–Q5 的
    `多读 you. Music. / it's not boring. …` 全是这种边界噪声，见 01-task-map 5.1）。
    窗口**外部**的 token 从不参与比对。
    """
    raw_a, raw_b = standard.split(), list(spoken)
    a = [norm(x) for x in raw_a]
    b = [norm(x) for x in raw_b]
    ops = difflib.SequenceMatcher(a=a, b=b).get_opcodes()

    matched = [k for k, op in enumerate(ops) if op[0] != "insert"]
    trimmed_lead = trimmed_tail = 0
    if matched:
        lo, hi = matched[0], matched[-1]
        trimmed_lead = sum(j2 - j1 for tag, _i1, _i2, j1, j2 in ops[:lo] if tag == "insert")
        trimmed_tail = sum(j2 - j1 for tag, _i1, _i2, j1, j2 in ops[hi + 1:] if tag == "insert")
        ops = ops[lo:hi + 1]

    notes = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        if tag == "delete":
            notes.append(f"漏读 `{' '.join(raw_a[i1:i2])}`")
        elif tag == "insert":
            notes.append(f"多读 `{' '.join(raw_b[j1:j2])}`")
        else:
            notes.append(f"`{' '.join(raw_a[i1:i2])}` → 念成 `{' '.join(raw_b[j1:j2])}`")

    text = "；".join(notes) if notes else "逐字一致"
    if trimmed_lead or trimmed_tail:
        text += f"；交界处另有 {trimmed_lead + trimmed_tail} 词不在题干内，已忽略（疑属相邻答案）"
    return text


def split_auto(tokens: list[dict]) -> tuple[list[dict], list[str]]:
    """无题目时的启发式切分：问号收尾为题干，句首疑问词/助动词起下一题。"""
    q_marks = [i for i, t in enumerate(tokens) if t["w"].rstrip().endswith("?")]
    if not q_marks:
        return [], ["转写里没有问号，无法自动切分。请用 --questions 提供标准题目。"]

    starts = []
    prev = 0
    for q in q_marks:
        if starts and q < starts[-1]:
            continue
        cand = None
        for i in range(prev + 1, q + 1):
            tok = tokens[i]["w"].strip()
            if norm(tok) in QUESTION_STARTERS and (tok[:1].isupper() or norm(tok) in QUESTION_STARTERS):
                cand = i
                break
        if cand is not None:
            starts.append(cand)
            prev = q
        else:
            prev = q

    spans = []
    for k, s in enumerate(starts):
        q_end = next((q for q in q_marks if q >= s), None)
        if q_end is None:
            continue
        a_start = q_end + 1
        # 紧跟其后的孤立疑问尾（Why? / Why or why not?）并入题干
        while a_start < len(tokens) and tokens[a_start]["w"].rstrip().endswith("?"):
            q_end = a_start
            a_start += 1
        a_end = (starts[k + 1] - 1) if k + 1 < len(starts) else len(tokens) - 1
        spans.append((tokens[s:q_end + 1], tokens[a_start:a_end + 1], ""))
    return spans, ["自动切分结果，请人工核对每条边界是否落在题目/答案交界处。"]


def low_confidence_points(tokens: list[dict]) -> list[dict]:
    scored = [t for t in tokens if t.get("p") is not None]
    if not scored:
        return []
    points = []
    for t in scored:
        if norm(t["w"]) in FUNCTION_WORDS or not norm(t["w"]):
            continue
        if t["p"] < LOW_P:
            points.append(t)
    return sorted(points, key=lambda x: x["p"])


def render(payload: dict, spans: list, warnings: list[str], args) -> str:
    meta = payload["meta"]
    all_tokens = payload["words"]
    overall = span_metrics(all_tokens)
    answers = [s[1] for s in spans if s[1]]
    total_ans_words = sum(len(a) for a in answers)
    total_ans_dur = sum(span_metrics(a)["dur"] for a in answers) if answers else 0

    L = []
    L.append("---")
    L.append("type: 批改底稿")
    L.append(f"学生: {args.student or '〔待填〕'}")
    L.append(f"班级: {args.klass or '〔待填〕'}")
    L.append(f"级别: {args.level or '〔待填〕'}")
    L.append(f"来源转写: {Path(meta.get('file', '')).name}（引擎 {meta.get('engine')}）")
    L.append("---")
    L.append("")
    L.append(f"# 批改底稿 · {args.student or '〔姓名〕'} · {args.klass or '〔班级〕'}")
    L.append("")
    L.append("> 本文件由 `kpf_analyze.py` 生成：**所有数字口径固定**，可直接引用。")
    L.append("> 评分与详评由 AI 填，发音分与互动交际由教师/评测引擎填（见 references/02-rubric.md、03-scoring-rules.md）。")
    L.append("")
    if warnings:
        L.append("**切分提示**")
        for w in warnings:
            L.append(f"- {w}")
        L.append("")

    L.append("## 一、整体指标")
    L.append("")
    L.append("| 项目 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 转写引擎 / 模型 | {meta.get('engine')} / {meta.get('model')} |")
    L.append(f"| 音频时长 | {meta.get('duration', 0):.1f} 秒 |")
    L.append(f"| 词标记总数（含念题） | {len(all_tokens)} |")
    L.append(f"| 整体语速（含念题） | {overall.get('wpm', 0)} 词/分 |")
    L.append(f"| 题目数 | {len([s for s in spans if s[0]])} |")
    L.append(f"| 答题词数合计 | {total_ans_words} |")
    L.append(f"| 答题语速合计 | {round(total_ans_words / total_ans_dur * 60) if total_ans_dur else 0} 词/分 |")
    L.append(f"| 每题平均词数 | {round(total_ans_words / len(answers), 1) if answers else 0} |")
    L.append("")

    L.append("## 二、逐题硬指标")
    L.append("")
    L.append("| 题 | 答词数 | 答段秒 | 语速(词/分) | 停顿≥0.4s | 其中≥1s | 最长停顿 | 填充词 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i, (qspan, aspan, _d) in enumerate(spans, 1):
        if not aspan:
            continue
        m = span_metrics(aspan)
        fill = "/".join(m["fillers"]) if m["fillers"] else "—"
        L.append(f"| Q{i} | {m['n']} | {m['dur']} | {m['wpm']} | {len(m['pauses'])} | "
                 f"{len(m['long_pauses'])} | {m['max_pause']:.2f}s | {fill} |")
    L.append("")
    L.append(f"> 停顿 = 词间间隔 ≥{PAUSE_MIN}s；长停顿 = ≥{PAUSE_LONG}s。低语速本身不直接扣分，看它是否伴随内容讲不完或逻辑断裂。")
    L.append("")

    L.append("## 三、逐题原文（问题 / 回答）")
    L.append("")
    L.append("> 念题差异只作线索：对齐窗口可能把相邻答案的尾部算进题干，逐题词数有 ±2–4 词的边界误差，"
             "**不得据此单独下结论**（见 references/01-task-map.md 第 5.1 节）。")
    L.append("")
    for i, (qspan, aspan, diff) in enumerate(spans, 1):
        L.append(f"### Q{i}")
        L.append("")
        if qspan:
            L.append(f"- **问**（学生念题）：{' '.join(t['w'] for t in qspan)}")
        if diff:
            L.append(f"- **念题与标准题目差异**：{diff}")
        if aspan:
            m = span_metrics(aspan)
            L.append(f"- **答**（{m['n']} 词 / {m['dur']} 秒 / {m['wpm']} 词每分 / "
                     f"停顿 {len(m['pauses'])} 次，最长 {m['max_pause']:.2f}s）："
                     f"{' '.join(t['w'] for t in aspan)}")
            if m["pauses"]:
                L.append(f"- 停顿位置（秒）：{m['pauses']}")
        L.append("")

    points = low_confidence_points(all_tokens)
    L.append("## 四、疑似发音点位（待语音评测引擎或教师确认）")
    L.append("")
    if not payload["words"][0].get("p") and all(t.get("p") is None for t in payload["words"]):
        L.append("> 本引擎不返回词级置信度（groq / xftj），请用 `--engine local` 跑一份，或跑 `kpf_asr.py crosscheck` 做双引擎比对。")
    if points:
        L.append("| 时间 | 转写词 | 词级置信度 | 邻近上文 |")
        L.append("|---|---|---|---|")
        for t in points[:40]:
            idx = all_tokens.index(t)
            ctx = " ".join(x["w"] for x in all_tokens[max(0, idx - 4):idx])
            L.append(f"| {mmss(t['start'])} | {t['w']} | {t['p']} | …{ctx} |")
        L.append("")
        L.append(f"> 低置信词共 {len(points)} 个（已排除虚词）。**置信度只用于排听点优先级，不能折算成分数。**")
    else:
        L.append("无低置信词（或引擎不提供置信度）。")
    L.append("")
    L.append("**必须在点位表里补充的项**（脚本无法自动判断）：")
    L.append("- [ ] 两引擎交叉验证（`kpf_asr.py crosscheck A.json B.json`）")
    L.append("- [ ] 个别音：/θ/ /ð/、/v/ vs /w/、词尾 -s、-ed 结尾、多音节重音")
    L.append("- [ ] 语调：句尾是否降调、从句结尾是否收住")
    L.append("")

    L.append("## 五、待填（AI 与教师分工）")
    L.append("")
    L.append("| 维度 | 评分方 | 预估 | 证据（必须填 2–3 条） |")
    L.append("|---|---|---|---|")
    L.append("| 语法与词汇 | AI | 〔待填〕 | 〔待填〕 |")
    L.append("| 话语组织 | AI | 〔待填〕 | 〔待填〕 |")
    L.append("| 发音 | 评测引擎 / 教师 | 〔待填〕 | 〔待填〕 |")
    L.append("| 互动交际 | 教师 | 〔按形态填 N/A 或分数〕 | 〔待填〕 |")
    L.append("")
    L.append("> 提醒：A2 Key 无「话语组织」维度，选中该行删掉；独白/自问自答形态下，互动交际一律填 `N/A` 并写明「无对手方，无证据」。")
    L.append("> 单篇作业只给分项分（0–5，可半分）并带免责句；不给加权总分、不给得分率、不判过没过；不换算剑桥量表分。")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="KPF 口语作业分析与批改底稿")
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--questions", type=Path, help="标准题目 txt，一行一题")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--student", default="")
    ap.add_argument("--class", dest="klass", default="")
    ap.add_argument("--level", default="")
    args = ap.parse_args()

    payload = json.loads(args.transcript.read_text(encoding="utf-8"))
    tokens = payload["words"]
    if not tokens:
        sys.exit("转写文件里没有词元，无法分析。")

    if args.questions:
        questions = [ln.strip() for ln in args.questions.read_text(encoding="utf-8").splitlines() if ln.strip()]
        spans, warnings = split_by_questions(tokens, questions)
    else:
        spans, warnings = split_auto(tokens)

    report = render(payload, spans, warnings, args)
    out = args.out or args.transcript.with_name(args.transcript.stem + "-底稿.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    n_q = len([s for s in spans if s[0]])
    print(f"已写出 {out}")
    print(f"切出 {n_q} 题；" + ("；".join(warnings) if warnings else "无提示"))


if __name__ == "__main__":
    main()
