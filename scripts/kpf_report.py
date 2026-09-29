#!/usr/bin/env python3
"""KPF 口语作业 · 生成三份成品骨架

设计意图：**机器能填的部分全部由脚本填**（指标句、点位表、frontmatter、检查清单），
需要人话的部分留占位符给 AI/教师填。这样跨批次的"数据句"措辞与口径完全一致。

用法：
  kpf_report.py <底稿.md> --kind 作业记录 --out 作业记录.md
  kpf_report.py <底稿.md> --kind 家长
  kpf_report.py <底稿.md> --kind 学生
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def parse_draft(text: str) -> dict:
    """从底稿里抽出机器填好的部分，避免重复计算、保证口径一致。"""
    data = {"overall": {}, "per_q": [], "low_conf": []}

    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if fm:
        for line in fm.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                data[k.strip()] = v.strip()

    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 7 and re.fullmatch(r"Q\d+", cells[0]):
            data["per_q"].append({
                "q": cells[0], "n": cells[1], "dur": cells[2], "wpm": cells[3],
                "pauses": cells[4], "long": cells[5], "max": cells[6],
            })
        elif len(cells) == 2 and "：" not in cells[0] and cells[1].isdigit():
            data["overall"][cells[0]] = cells[1]

    for line in text.splitlines():
        m = re.match(r"\|\s*(\d{2}:\d{2}\.\d)", line)
        if m:
            data["low_conf"].append([c.strip() for c in line.strip().strip("|").split("|")])
    return data


def metric_sentences(d: dict) -> str:
    qs = d["per_q"]
    if not qs:
        return "〔未能从底稿解析出逐题指标，请人工补写〕"
    n = [int(x["n"]) for x in qs]
    wpm = [int(x["wpm"]) for x in qs]
    pauses = [int(x["pauses"]) for x in qs]
    longs = [int(x["long"]) for x in qs]
    longest = max(float(x["max"].rstrip("s")) for x in qs)
    avg = sum(n) / len(n)
    return (
        f"共 {len(qs)} 道题，平均每题 {avg:.1f} 个词"
        f"（最少 {min(n)} 词、最多 {max(n)} 词），答题语速平均 {sum(wpm) // len(wpm)} 词/分钟；"
        f"全程停顿 {sum(pauses)} 次，其中超过一秒的 {sum(longs)} 次，最长 {longest:.2f} 秒。"
    )


TPL_RECORD = """---
type: 作业
学生: {student}
班级: {klass}
作业: {homework}
布置日期: 待补
截止日期: {date}
提交日期: {date}
状态: 已完成
能力点:
  - 〔待填：2–4 项，与错误卡的能力点对齐〕
来源课堂:
---

# 作业 · {homework} · {date}

> 状态流转：待提交 → 待批改 → 已完成（完成时转化为错误卡或进步证据）

## 作业内容

{homework_note}

- 原始文件：`{media}`
- 转写稿：[[{transcript_link}|转写原文（已归档）]]
- 批改分工：**语法与词汇、话语组织由 AI 评**；**发音由评测引擎/教师评**；**互动交际由教师评**

## 提交记录

- 提交时间：{date}
- 提交形式：{submission_note}

## 批改与反馈

### 分项评分（{level} 口径，0–5 分，可半分）

| 维度 | 预估 | 评分方 | 依据摘要 |
|---|---|---|---|
| 语法与词汇 | 〔待填〕 | AI | 〔待填 2–3 条证据〕 |
| 话语组织 | 〔待填；A2 Key 无此项，删掉本行〕 | AI | 〔待填 2–3 条证据〕 |
| 发音 | 〔见发音文件〕 | 评测引擎/教师 | — |
| 互动交际 | {interaction} | 教师 | {interaction_reason} |

> 注：单篇作业只给分项分（0–5，可半分）——**不给加权总分、不给得分率、不判过没过，不换算剑桥量表分**；
> 只有覆盖全部 Part、四项齐备的完整模拟才给总分（见 references/02-rubric.md 第六节）。

### 硬指标（回答段单独统计）

{draft_tables}

### 语法与词汇 · 详评

〔待填：错误清单表（原句 → 应为 → 类型）；加分项要引真实原话〕

### 话语组织 · 详评（A2 Key 无此项，整段删掉）

〔待填：切题 / 展开长度 / 衔接多样性 / 流利度；加分项〕

### 教师观察（{date}）

〔待填：教师听音后的判断 + AI 用转写数据做的交叉核对〕

### 发音与朗读 · 抽听点位表（留待老师，带时间）

{pronounce_section}

### 互动交际 · 待老师确认

{interaction_note}

## 家长反馈（待发出）

> 发出后请把本行改成「已发出（日期）」。

〔用 `--kind 家长` 生成的草稿粘到这里〕

## 转化

- [ ] 个人错误卡：〔链接〕
- [ ] 共性错误卡（同一错误已出现在 ≥2 名学生身上时建）
- [ ] 老师补发音与互动交际**评分**
- [ ] 下次作业复查：〔列 3–4 条具体的复查项〕

## 交付前自检

按 `references/checklist.md` 逐项自检（全 skill 唯一一份清单），并跑：

```bash
python3 scripts/kpf_validate.py <本文件> --kind 作业记录 --form 单篇 --level <KET|PET|FCE>
```

校验不通过必须重做，不得交付。
"""

TPL_PARENT = """{student} {level}口语作业反馈
（{date} · {homework}）

一、本次评分（参照 {level} 口语评分标准，各维度 0–5 分）

语法与词汇：〔待填〕 / 5
话语组织：〔待填〕 / 5      ← A2 Key 无此项，整行删掉
发音：〔填分数；未取得则写「待补」〕 / 5
互动交际：{interaction}
这是单次录音的表现，不作为考试总分预估。

二、做得好的地方
1. 〔具体，引原话或现象〕
2. 〔同上〕

三、存在的问题
1. 〔问题名〕：他说的 〔原句〕 应为 〔改后〕。〔一句说明〕
2. 〔同上，共 4–6 条〕

四、需要改进的方向
1. 〔对应问题 1 的可执行动作〕
2. 〔对应问题 2 的可执行动作〕

五、接下来重点 / 回家怎么配合
〔一件事 + 具体量；说明不用发给老师〕

六、本次未涉及的部分
{parent_not_covered}
"""

TPL_STUDENT = """{student} · 本次口语作业反馈
（{date} · {homework}）

## 一、你这次做到的三件事

1. 〔具体，带数字或原话〕
2. 〔同上〕
3. 〔同上〕

## 二、本次预估分（AI 预估，不是官方分）

| 维度 | 预估 | 说明 |
|---|---|---|
| 语法与词汇 | 〔待填〕 / 5 | |
| 话语组织 | 〔待填〕 / 5 | A2 Key 无此项，整行删掉 |
| 发音 | 〔评测结果；未取得填"待补"〕 / 5 | 只覆盖念题部分 |
| 互动交际 | 〔按形态填 N/A 或分数〕 | 〔按形态说明；独白写"没有对手方，这一项评不了"〕 |

题目指标：{metrics}

## 三、逐句修改（只列最值得改的 5–8 句）

| # | 你说的 | 改后 | 为什么 |
|---|---|---|---|
| 1 | 〔原句〕 | 〔改后〕 | 〔错在哪〕 |
| 2 | | | |

## 四、可以直接背的三句升级表达

1. 〔替换中式表达的地道说法〕
2. 〔同上〕
3. 〔同上〕

## 五、下次课当场验收的一件事

〔明确、可完成、做对了当场给肯定〕
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="生成作业记录 / 家长版 / 学生版骨架")
    ap.add_argument("draft", type=Path)
    ap.add_argument("--kind", choices=["作业记录", "家长", "学生"], required=True)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--homework", default="〔作业名，如 FCE Trainer Test 2 Speaking Part 1（P102）〕")
    ap.add_argument("--date", default="〔YYYY-MM-DD〕")
    ap.add_argument("--media", default="〔原始音视频路径〕")
    ap.add_argument("--transcript-link", default="〔归档后的转写原文 wikilink〕")
    ap.add_argument("--form", choices=["自问自答", "独白", "对话"], default=None,
                    help="作业形态；给了才会把互动交际填成确定性措辞，否则留成〔待判断〕占位符")
    args = ap.parse_args()

    text = args.draft.read_text(encoding="utf-8")
    d = parse_draft(text)
    student = d.get("学生", "〔姓名〕")
    klass = d.get("班级", "〔班级〕")
    level = d.get("级别", "〔级别〕")
    metrics = metric_sentences(d)

    # 互动交际的措辞必须跟着作业形态走：含对手方的对话录音硬写成"自问自答"就是假话
    if args.form in ("自问自答", "独白"):
        interaction = "待补（本次是独自录音，测不到和别人的对话能力）"
        interaction_reason = "无对手方，本项无证据"
        interaction_note = "无对手方（单说话人），本项无证据。若要评需另排一次两两对话或含对手方的作业。"
        parent_not_covered = "本次是独自录音，测不到和别人的对话能力，课堂上的对话环节我会另外观察。"
    elif args.form == "对话":
        interaction = "〔教师评分〕"
        interaction_reason = "含对手方，需教师判断"
        interaction_note = "含对手方，互动交际由教师评分（AI 不下结论）。"
        parent_not_covered = "本次含对话环节，互动交际部分我在课堂上另外观察。"
    else:
        interaction = "〔待判断：按形态填 N/A/待补 或教师分数〕"
        interaction_reason = "〔按形态：独白或自问自答＝无对手方、本项无证据；含对手方＝教师评分〕"
        interaction_note = ("〔按形态填写：独白/自问自答写「无对手方，本项无证据」；"
                           "含同伴或考官的对话录音需教师评分。〕")
        parent_not_covered = "〔按形态填写，如「本次是独自录音，测不到和别人的对话能力，课堂上的对话环节我会另外观察。」〕"

    ctx = dict(
        student=student, klass=klass, level=level, date=args.date,
        homework=args.homework, metrics=metrics,
        homework_note="〔一句作业说明：来自哪本书哪一页、哪些题、什么形态（自问自答/对话/独白）〕",
        media=args.media, transcript_link=args.transcript_link,
        submission_note="〔文件数量、时长、格式；若为视频且画面不可用要写明〕",
        draft_tables="〔把底稿的「整体指标」与「逐题硬指标」两张表粘到这里〕",
        pronounce_section="〔把 `<转写>-发音.md`（或 `<转写>-发音-ise.md`）的内容粘到这里〕",
        interaction=interaction,
        interaction_reason=interaction_reason,
        interaction_note=interaction_note,
        parent_not_covered=parent_not_covered,
    )

    tpl = {"作业记录": TPL_RECORD, "家长": TPL_PARENT, "学生": TPL_STUDENT}[args.kind]
    out_text = tpl.format(**ctx)

    out = args.out or args.draft.with_name(f"{student}-{args.kind}.md")
    out.write_text(out_text, encoding="utf-8")
    print(f"已写出 {out}（{args.kind}）")
    if args.kind == "家长":
        print(
            "家长版定稿提醒（不写进文件，避免踩红线）：\n"
            "  - A2 Key 没有「话语组织」这一项，把那一行删掉（英文写法 Discourse Management 一样算错）。\n"
            "  - 互动交际只在含同伴/考官的对话录音里填分数；独白/自问自答写「待补」。\n"
            "  - 整篇只放每个维度 X / 5：不要写工具的原始分（如 93.6/100），"
            "不要写带量化的技术指标（语速 X 词/分、停顿 X 次、词数、字数）。\n"
            "    「几乎没有停顿」「语速偏快」这类非量化描述可以留；带数字或次数就必须删。\n"
            "  - 单篇家长版正文除免责句外不得出现「总分」二字。\n"
            "  - 通读一遍：不能让人看出有工具链参与。",
            file=sys.stderr,
        )
    if args.kind == "作业记录" and d["per_q"]:
        print("已自动填入逐题指标句；其余〔待填〕由 AI/教师补。")


if __name__ == "__main__":
    main()
