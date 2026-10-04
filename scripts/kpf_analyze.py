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
# 题目对齐的"说不清"判据（见 _match_question 第 4 条）：命中任一条就标边界待确认。
# 两个数都有出处，不是拍的：
#   AMBIG_GAP：两处候选的**分差**；合成用例里出现过的分差是 0.09–0.147。
#   AMBIG_ABS：某一处候选**自身够像**。0.75 这个数来自两处实测 ——
#     ① 项目自己踩过的坑：阈值放宽到 0.75 时，前面一个"勉强像"的位置会抢掉后面更像的
#        真位置（实测把答案段切少了 4 个词），也就是说 0.75 这一档确实会被误当成念题；
#     ② 两份真实作业 9 道题里，与最佳窗口**不重叠**的第二名最高只有 0.625，
#        0.75 留了 0.125 的余量，不会把真实数据误标。
# 只用 AMBIG_GAP 会漏：外审 2026-10-04 第二轮的合成用例里，念题 0.8、答案里的复述 1.0，
# 分差 0.2 超出余量，于是**静默**选了复述、开头的回答从该题答段消失。
AMBIG_GAP = 0.15
AMBIG_ABS = 0.75


def _trim_insertions(tokens: list[dict], targets: list[str], i: int, j: int,
                     min_keep: int = 1) -> tuple[int, int, float]:
    """剪掉窗口首尾的**纯插入** token，返回剪后的 (起点, 终点, 相似度)。

    只剪 `insert`、不剪 `replace`：学生把念题开头的词念错（`Which` 念成 `We each`）时
    那是替换不是插入，必须留在题干里；而首尾多出来的"纯插入"多半是相邻答案的尾巴
    串进了窗口（`diff_reading` 早就在展示层做同一件事，这里是对**边界**做）。
    剪不动（剪完不足 `min_keep` 词、或本来就没多）时原样返回。
    """
    pairs = [(k, norm(tokens[k]["w"])) for k in range(i, j + 1) if norm(tokens[k]["w"])]
    if not pairs:
        return i, j, 0.0
    win = [w for _, w in pairs]
    sm = difflib.SequenceMatcher(a=targets, b=win)
    ratio = sm.ratio()
    ops = sm.get_opcodes()
    lo, hi = 0, len(win)
    if ops and ops[0][0] == "insert":
        lo = ops[0][4]
    if ops and ops[-1][0] == "insert":
        hi = ops[-1][3]
    if hi - lo < max(1, min_keep) or (lo == 0 and hi == len(win)):
        return i, j, ratio
    trimmed = win[lo:hi]
    return (pairs[lo][0], pairs[hi - 1][0],
            difflib.SequenceMatcher(a=targets, b=trimmed).ratio())


def _match_question(tokens: list[dict], targets: list[str], start: int,
                    slack: int = 3,
                    ) -> tuple[int | None, int | None, float, tuple[int, float] | None]:
    """把标准题目对齐到转写里的一段，返回 (题干起点, 题干终点, 相似度, 歧义提示)。

    七条设计（前五条每一个都被真实数据打回过一次）：
    1. **不依赖 ASR 的标点**——Whisper 常不给念出来的问题加问号（实测 5 题里 3 题没有），
       所以题干终点由"最佳匹配窗口的右端"决定，而不是去找 `?`；
    2. **窗口长度受限**（题目词数 ±slack）——否则起点可以前移、把答案吞进题干；
    3. **滑动窗口 + 相似度**——学生念题常改词（`Which` 念成 `We each`），逐字匹配会整段失配；
    4. **选窗取全局最相似的那处；前后若有"位置不同、相似度接近"的候选，就标待确认，
       不自动裁决**——念题在整段录音里只发生一次，但音频里可能出现两处都像的位置：
       上一题答案里有一句相似的话，或者学生在本题的答案里把题目复述一遍。做法：
       - **只选全局最佳，不按余量降门槛往前找。** 外审 2026-10-04 给过一个反例：
         全局最佳只有 0.9 时，"全局最佳 − 0.15"这种余量会把门槛压到 0.75，正好落回
         "前面一个勉强像的位置抢掉后面真位置"那个老坑 —— 实测选中的是上一题答案里的
         一句话（0.783），真正念题的那处（0.909）被跳过。
       - **是否"说不清"看两条，命中任一条就标待确认**：① 分差接近（`分差 ≤ AMBIG_GAP`）；
         ② 另一处候选**自身够像**（`相似度 ≥ AMBIG_ABS`）。**只比"分差"是不够的** ——
         念题 0.8、答案里的复述 1.0 时分差 0.2，超出一个 0.15 的余量，于是会静默选中
         复述、开头的回答从该题答段消失（外审 2026-10-04 第二轮用合成转写复现）。
         两个常数的出处写在文件头 `AMBIG_GAP` / `AMBIG_ABS` 的注释里。
       - 命中之后：把另一处候选的位置一并返回；调用方据此把该题标成 `边界待确认` ——
         **这一题的精确答词数与语速先不输出**（`split_by_questions` / `render` 负责落实），
         底稿里把两处候选都打出来，由教师听音频定边界。这比"猜一个再小声警告"诚实
         （见 references/01-task-map.md 5.1）。
    5. **首尾的"纯插入"要剪掉**（见 `_trim_insertions`）——窗口两端多出来的词多半是相邻
       答案的尾巴串了进来（`diff_reading` 早就在展示层这么做了）。只剪 `insert`，不剪
       `replace`：学生把念题的第一个词念错（`Which` 念成 `We each`）时那是替换不是插入，
       必须留在题干里。剪完的相似度按剪后的窗口重算。
       外审 2026-10-04 的合成转写（`Why because I like music What music do you like
       I like jazz`）就是这一类：选中窗口只多吞了前一个词，剪掉之后边界才落对。
    6. **窗口下界只对短题干放宽**——原来窗口最少 3 个词（`j` 从 `i + 2` 起），短题干够不到
       等长窗口：`Why?`（1 词）拿 3 词窗口去比，相似度最高也只有 0.50，低于 WINDOW_RATIO，
       于是它永远判「未定位」，题干连同整题答案一起从底稿里消失。现在下界是
       `i + min(2, max(1, qlen) - 1)`（1 词题从 i 起、2 词题从 i+1 起），窗口下限词数
       `min(3, qlen)`。
       **注意 `min(2, …)` 这个夹子不能省**：下界是"至少 3 词"写死在 `i + 2` 上的，跟 qlen 无关。
       写成 `i + max(1, qlen) - 1` 会让 4 词题的最小窗口从 3 词变成 4 词、5 词题变成 5 词——
       长题目的对齐结果整片漂移（实测：679 词的官方校准转写里 6 道题有 4 道换了位置）。
       夹到 2 之后，qlen ≥ 3 时两条式子的取值与改动前逐字相同，长题目一个字节不变。
    """
    qlen = len(targets)
    min_words = min(3, qlen)                        # qlen ≥ 3 时恒为 3，与改动前一致
    j_from = min(2, max(1, qlen) - 1)               # qlen ≥ 3 时恒为 2，与改动前一致
    best_at: list[tuple[int, int, float]] = []      # 每个 i 的最佳窗口（i 升序）
    overall: tuple[int | None, int | None, float] = (None, None, 0.0)
    for i in range(start, max(start + 1, len(tokens) - 2)):
        cur: tuple[int | None, int | None, float] = (None, None, 0.0)
        cur_key = (0.0, 0)                          # (相似度, −|窗口词数 − 题目词数|)
        for j in range(max(i, i + j_from), min(i + qlen + slack, len(tokens))):
            window = [norm(t["w"]) for t in tokens[i:j + 1] if norm(t["w"])]
            if len(window) < min_words:
                continue
            ratio = difflib.SequenceMatcher(a=targets, b=window).ratio()
            key = (ratio, -abs(len(window) - qlen))
            if key > cur_key:
                cur_key = key
                cur = (i, j, ratio)
        if cur[0] is None:
            continue
        best_at.append(cur)
        if cur[2] > overall[2]:
            overall = cur

    chosen = overall
    if chosen[0] is None:
        return None, None, 0.0, None

    found, q_end, ratio = _trim_insertions(tokens, targets, chosen[0], chosen[1], min_words)

    ambiguous = None
    for c in best_at:
        if c[0] == chosen[0]:
            continue
        overlaps = not (c[1] < chosen[0] or c[0] > chosen[1])
        if overlaps or (c[2] < overall[2] - AMBIG_GAP and c[2] < AMBIG_ABS):
            continue
        ambiguous = (c[0], c[2])        # 取最早的那条（best_at 按 i 升序）
        break
    return found, q_end, ratio, ambiguous


def _infer_unlocated(tokens: list[dict], marks: list, idx: int, owner: dict[int, int],
                     question: str, ratio: float) -> tuple[list[dict] | None, str, str]:
    """给一道「未定位」的题定答案区间：返回 (答案span 或 None, 底稿说明, 切分警告)。

    为什么必须推断：对齐失败时题干和答案会一起从 spans 里消失，那一段词就只在「全篇指标」
    里有、在「答题合计」里没有——两套数字对不上，教师也无从知道被漏掉的是哪一题。

    为什么只有一部分未定位的题能拿到 span（一段词只能算一次，两题都算就是重复计数）：
    - **前面没有已定位题目**（一开头就没人定位上）：区域 `[0, 下一道已定位题的题干起点)` 谁都
      没认领，归这一段里的**第一题**，计入答题合计；同段后续题目与这个区间重叠，不重复计入。
    - **前面有已定位题目**：那道题的答案区间按「答案跟在题干后面」的老规则一直延伸到下一道
      已定位题的起点，区域整个落在它里面——本题的回答已经随它进了答题合计，再算一次就重复。
      所以这里只报区间、不加数（旧版正是在这个位置写了句谎话：「改由相邻题目推断」，其实没推）。
    - **前后都没有已定位题目**（全篇都没定位上）：不凭空造数，答案保持为空，只写一行说明。
    三种情况都返回一段人话说明，底稿逐题写出来——任何一段词都不静默丢弃。
    """
    prev = next((i for i in range(idx - 1, -1, -1) if marks[i] is not None), None)
    nxt = next((i for i in range(idx + 1, len(marks)) if marks[i] is not None), None)
    if prev is None and nxt is None:
        note = "题目未定位，前后都没有已定位题目，无法推断答案区间；本题答案记为空，不计入答题合计。"
        return None, note, (f"第 {idx + 1} 题未能在转写里定位（对齐度 {ratio:.0%}）：题干不进底稿；"
                            f"前后都没有已定位题目，无法推断答案区间，本题答案记为空、不计入答题合计，"
                            f"请人工核对：{question[:40]}…")
    lower = marks[prev][1] + 1 if prev is not None else 0
    upper = marks[nxt][0] if nxt is not None else len(tokens)
    seg = tokens[lower:upper]
    if not seg:
        note = "题目未定位，相邻已定位题目之间没有可推断的词（推断区间为空）；本题答案记为空，不计入答题合计。"
        return None, note, (f"第 {idx + 1} 题未能在转写里定位（对齐度 {ratio:.0%}）：题干不进底稿；"
                            f"相邻题目之间推断不出任何词，本题答案记为空、不计入答题合计，"
                            f"请人工核对：{question[:40]}…")
    span_desc = f"{mmss(seg[0]['start'])}–{mmss(seg[-1]['end'])}，{len(seg)} 词"
    if prev is not None:
        note = (f"题目未定位，其回答落在第 {prev + 1} 题的答段区间内（{span_desc}），无法单独切出；"
                f"这些词已随第 {prev + 1} 题计入答题合计，本题不重复计入。")
        return None, note, (f"第 {idx + 1} 题未能在转写里定位（对齐度 {ratio:.0%}）：题干不进底稿；"
                            f"其回答落在第 {prev + 1} 题的答段区间 {span_desc} 内，"
                            f"已随第 {prev + 1} 题计入答题合计、本题不重复计入，"
                            f"请人工核对：{question[:40]}…")
    claimed = owner.get(idx, idx)   # 开头这一段的第一题认领区间；缺失时（不该发生）就自己认领
    if claimed == idx:
        note = "题目未定位，答案为相邻题目之间的推断区间（可能含未识别的题干词）。"
        return list(seg), note, (f"第 {idx + 1} 题未能在转写里定位（对齐度 {ratio:.0%}）：题干不进底稿；"
                                 f"其答案改由相邻已定位题目之间的推断区间 {span_desc} 承担"
                                 f"（可能含未识别的题干词），已计入答题合计，请人工核对：{question[:40]}…")
    note = (f"题目未定位，与第 {claimed + 1} 题的推断区间（{span_desc}）重叠、无法分摊；"
            f"已随第 {claimed + 1} 题计入答题合计，本题不重复计入。")
    return None, note, (f"第 {idx + 1} 题未能在转写里定位（对齐度 {ratio:.0%}）：题干不进底稿；"
                        f"本题与第 {claimed + 1} 题的推断区间 {span_desc} 重叠、无法分摊，"
                        f"已随第 {claimed + 1} 题一并计入答题合计，请人工核对：{question[:40]}…")


def split_by_questions(tokens: list[dict], questions: list[str]) -> tuple[list[dict], list[str]]:
    """按标准题目定位每题的边界。返回 [(问题span, 答案span, 念题差异, 待确认说明)] 与对齐警告。

    未定位的题目答案 span 见 `_infer_unlocated`：要么是「相邻已定位题目之间的推断区间」
    （第三项说明里写明），要么为空（说明里写明为什么空）——旧版这里是 `(None, None, "未定位")`，
    整题（题干 + 答案）无声消失，且警告还宣称"已改由相邻题目推断"，与代码实际行为不符。

    第四项 `pending`：非空 = 这题（或它的答案末边界）**说不清**，`render` 会把该题的精确
    答词数与语速换成「待确认」。两种来源：
      ① 题干在音频里有两处位置不同、相似度接近的候选（见 `_match_question` 第 4 条）；
      ② 下一题的题干边界待确认 → 本题答案的末边界跟着不确定。
    """
    qtok = [[norm(w) for w in q.split() if norm(w)] for q in questions]
    warnings: list[str] = []
    marks: list[tuple[int, int, float] | None] = []
    ratios: list[float] = []   # 每题的最佳对齐度（未定位的也要留着，警告里要报）
    pending: dict[int, str] = {}
    cursor = 0

    for idx, (question, targets) in enumerate(zip(questions, qtok)):
        found, q_end, ratio, ambiguous = _match_question(tokens, targets, cursor)
        ratios.append(ratio)
        if found is None or ratio < WINDOW_RATIO:
            marks.append(None)
            continue
        marks.append((found, q_end, ratio))
        cursor = q_end + 1
        if ambiguous is not None:
            # 两处都像。**不替教师裁决**：本题答词数/语速先不输出，等听音频定边界。
            alt_i, alt_ratio = ambiguous
            pending[idx] = (f"题干在音频里有**两处**位置不同、相似度接近的候选"
                            f"（已选的这处 {ratio:.0%}、另一处在转写第 {alt_i + 1} 词起 "
                            f"{alt_ratio:.0%}），本题的精确答词数与语速**先不输出**")
            warnings.append(
                f"第 {idx + 1} 题边界待确认：题干在音频里有两处候选（相似度 {ratio:.0%} 与 "
                f"{alt_ratio:.0%}、位置分别从第 {found + 1} 词与第 {alt_i + 1} 词起）。"
                f"已按全局最相似的那处切分，但**本题的答词数与语速不计入统计**，"
                f"请听音频确认边界：{question[:40]}…")
        if len(targets) < 3:
            # 放开了短题目的窗口下限，代价是「1–2 词的题目」本身就属于弱证据：答案里出现
            # 同形词（`and you` / `why`）也能拿到 100% 对齐。这是放宽窗口后新引入的误判面，
            # 所以在底稿里明说，让教师核对边界，而不是装作对齐结果和长题目一样可靠。
            warnings.append(
                f"第 {idx + 1} 题只有 {len(targets)} 词，属于弱定位证据（答案里出现同形词也会命中），"
                f"边界请人工核对：{question[:40]}…"
            )

    # 开头那一段「谁都没认领」的连续未定位题：区间只能记一次，归这一段的第一题。
    owner: dict[int, int] = {}
    head_run: int | None = None
    for idx in range(len(marks)):
        if marks[idx] is not None:
            head_run = None
            continue
        if any(m is not None for m in marks[:idx]):
            continue           # 前面有已定位题 → 那题已经覆盖了这个区域（见 _infer_unlocated）
        if head_run is None:
            head_run = idx
        owner[idx] = head_run

    spans = []
    for idx, question in enumerate(questions):
        mark = marks[idx]
        if mark is None:
            aspan, note, warn = _infer_unlocated(tokens, marks, idx, owner, question, ratios[idx])
            spans.append((None, aspan, note, ""))
            warnings.append(warn)
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
        spans.append((tokens[found:q_end + 1], tokens[q_end + 1:nxt], diff, pending.get(idx, "")))

    # 末边界传播：**本题答案的末边界 = 它之后第一道"已定位题"的题干起点**（未定位题拿到的
    # 推断区间用的也是这个上界）。所以那道题一旦待确认，本题的边界跟着不确定 → 同样不输出
    # 精确数字、也不计入合计。**必须右到左做**，否则漏掉链式传播（Q3 待确认 → Q2 待确认 →
    # Q1 也待确认：Q1 的末边界取的是 Q2 的题干起点，而 Q2 的起点本身还没定）。
    # 外审 2026-10-04 第二轮就是在这里漏的：旧实现跳过 `marks[idx] is None` 的题，于是
    # "开头那道未定位题的推断区间"照样输出 `Q1† | 7` 并计入答题词数合计。
    for idx in range(len(spans) - 1, -1, -1):
        if pending.get(idx) or spans[idx][1] is None:
            continue           # 已经待确认，或本来就没有数字可扣
        nxt_located = next((j for j in range(idx + 1, len(spans)) if marks[j] is not None), None)
        if nxt_located is None or not pending.get(nxt_located):
            continue
        pending[idx] = (f"本题答案的末边界取的是第 {nxt_located + 1} 题的题干起点，"
                        f"而那一题的边界待确认 → 本题的边界随之不确定，"
                        f"精确答词数与语速**先不输出**")
        spans[idx] = (spans[idx][0], spans[idx][1], spans[idx][2], pending[idx])
        warnings.append(f"第 {idx + 1} 题的答案末边界取决于第 {nxt_located + 1} 题的题干起点，"
                        f"而后者待确认 → 本题的答词数与语速也不计入统计。")
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
        spans.append((tokens[s:q_end + 1], tokens[a_start:a_end + 1], "", ""))
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
    # 边界待确认的题**不进任何自动统计**：宁可少一个数，也不给一个错的数（见 split_by_questions）
    answers = [s[1] for s in spans if s[1] and not s[3]]
    n_pending = len([s for s in spans if s[3]])
    total_ans_words = sum(len(a) for a in answers)
    total_ans_dur = sum(span_metrics(a)["dur"] for a in answers) if answers else 0
    tail = f"（另有 {n_pending} 题边界待确认，未计入）" if n_pending else ""

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
    # 单独给一行"未定位题数"：不加它，上面那行只在数"找得到题干的题数"，
    # 于是"下面有三行 Q、这里写题目数 2"看上去像 bug（2026-09-30 定）。两个数都是定位口径，
    # 与"答题词数合计"不同——后者含开场段的推断区间。
    L.append(f"| 未定位题数 | {len([s for s in spans if s[0] is None])} |")
    # 待确认题数单独一行：它和"未定位"不是一回事 —— 题干定位上了，但边界说不清，所以
    # 那一题的精确数字不输出、也不进下面的合计（宁可少一个数，也不给一个错的数）
    L.append(f"| 边界待确认题数 | {n_pending} |")
    L.append(f"| 答题词数合计 | {total_ans_words}{tail} |")
    L.append(f"| 答题语速合计 | {round(total_ans_words / total_ans_dur * 60) if total_ans_dur else 0} 词/分{tail} |")
    L.append(f"| 每题平均词数 | {round(total_ans_words / len(answers), 1) if answers else 0}{tail} |")
    L.append("")

    L.append("## 二、逐题硬指标")
    L.append("")
    L.append("| 题 | 答词数 | 答段秒 | 语速(词/分) | 停顿≥0.4s | 其中≥1s | 最长停顿 | 填充词 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for i, (qspan, aspan, _d, note) in enumerate(spans, 1):
        if note:
            # 边界待确认：这一题的精确答词数/语速/停顿**一个都不输出**，只标明原因。
            # 光是"给个数再小声警告"不算诚实 —— 数字会被引用，警告不会。
            L.append(f"| Q{i}‡ | 待确认 | — | 待确认 | — | — | — | 边界待确认 |")
            continue
        if not aspan:
            if qspan is None:
                # 未定位的题也必须占一行：整行消失会让教师以为"这题不存在"（旧版就是这么静默丢的）
                L.append(f"| Q{i} | — | — | — | — | — | — | 未定位 |")
            continue
        m = span_metrics(aspan)
        fill = "/".join(m["fillers"]) if m["fillers"] else "—"
        mark = "†" if qspan is None else ""     # 题干没定位上 → 这个答段是推断出来的
        L.append(f"| Q{i}{mark} | {m['n']} | {m['dur']} | {m['wpm']} | {len(m['pauses'])} | "
                 f"{len(m['long_pauses'])} | {m['max_pause']:.2f}s | {fill} |")
    L.append("")
    L.append(f"> 停顿 = 词间间隔 ≥{PAUSE_MIN}s；长停顿 = ≥{PAUSE_LONG}s。低语速本身不直接扣分，看它是否伴随内容讲不完或逻辑断裂。")
    if any(s[0] is None and s[1] for s in spans):
        L.append("> † 该题未定位：答段是相邻已定位题目之间的**推断区间**，可能含未识别的题干词，"
                 "词数/语速只能当粗略参考，不计入任何自动判分。")
    if n_pending:
        L.append("> ‡ 该题**边界待确认**：题干在音频里有位置不同、相似度接近的候选，"
                 "或它的答案末边界取决于下一题的题干起点。**这一题的精确答词数与语速不予输出**、"
                 "也不计入上面的合计，请听音频定边界后再补（references/01-task-map.md 5.1）。")
    L.append("")

    L.append("## 三、逐题原文（问题 / 回答）")
    L.append("")
    L.append("> 念题差异只作线索：对齐窗口可能把相邻答案的尾部算进题干，逐题词数有 ±2–4 词的边界误差，"
             "**不得据此单独下结论**（见 references/01-task-map.md 第 5.1 节）。")
    L.append("")
    for i, (qspan, aspan, diff, note) in enumerate(spans, 1):
        L.append(f"### Q{i}")
        L.append("")
        if qspan:
            L.append(f"- **问**（学生念题）：{' '.join(t['w'] for t in qspan)}")
            if diff:
                L.append(f"- **念题与标准题目差异**：{diff}")
        elif diff:
            # 未定位：第三项装的是「这段答案是推断的还是没推出来」的说明，逐题写一行，绝不静默
            L.append(f"- **未定位**：{diff}")
        if note:
            L.append(f"- **边界待确认**：{note}")
            L.append(f"- **这一题的答段（供人工核对，数字不予输出）**："
                     f"{' '.join(t['w'] for t in aspan) if aspan else '（空）'}")
        elif aspan:
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
        L.append("> 本引擎不返回词级置信度（groq），请用 `--engine local` 跑一份，或跑 `kpf_asr.py crosscheck` 做双引擎比对。")
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
