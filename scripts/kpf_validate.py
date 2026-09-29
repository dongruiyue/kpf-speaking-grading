#!/usr/bin/env python3
"""KPF 口语作业报告 · 输出合规校验器

纯标准库 · 离线可跑 · 零外部依赖：不联网、不加载模型、不读凭证、不安装任何包。
只做静态文本检查（读文件 → 报错），不修改任何文件。

约定（与 `guizang-ppt-skill/scripts/validate-swiss-deck.mjs` 一致）：
  - **errors 决定退出码**：有 error → `sys.exit(1)`；warnings 只打印，不影响退出码。
  - **stderr 打明细**（每条带行号），**stdout 只打一行摘要**。
  - 任何检查都不能"因为环境缺东西就静默跳过"：转写稿解析不了就直接报错并非零退出。

用法：
  kpf_validate.py <报告文件> --kind 家长|学生|作业记录 --form 单篇|完整模拟 \
      --level KET|PET|FCE [--transcript <转写.json>] \
      [--draft] [--quote-threshold <float>] [--quote-warn-threshold <float>]

  --draft                  作业记录草稿形态：占位符残留由 error 降为 warning，其余检查不变；
                           只对 --kind 作业记录 有效（家长版/学生版传了 → 退出码 2）。
                           不传也行：作业记录的 frontmatter 写了「状态: 草稿」即自动进入草稿形态。
  --quote-threshold        防编造 error 边界（默认 0.85）：相似度低于它判 error。
  --quote-warn-threshold   防编造通过边界（默认 0.92）：相似度 ≥ 它通过，介于两者之间给 warning；
                           必须大于 --quote-threshold，否则退出码 2。

退出码：0 = 通过（可能带 warning）；1 = 有 error，不可交付；2 = 用法/路径错误。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

LEVELS = {"KET": "A2 Key", "PET": "B1 Preliminary", "FCE": "B2 First"}
# 维度按级别给：A2 Key 只有 3 项，没有话语组织（references/02-rubric.md 第一节）
DIMENSIONS = {
    "KET": ["语法与词汇", "发音", "互动交际"],
    "PET": ["语法与词汇", "话语组织", "发音", "互动交际"],
    "FCE": ["语法与词汇", "话语组织", "发音", "互动交际"],
}
FULL_MARK = {"KET": 45, "PET": 30, "FCE": 60}

# 维度名的英文等价写法（官方原名）。中文名仍是主口径，英文名只作等价识别，
# 不参与任何数学校验（分制、权重、满分都还是按中文维度的那几条规则走）。
# 匹配时大小写不敏感，`and` 与 `&` 等价。
DIM_ALIASES = {
    "语法与词汇": ["Grammar and Vocabulary", "Grammar & Vocabulary"],
    "话语组织": ["Discourse Management"],
    "发音": ["Pronunciation"],
    "互动交际": ["Interactive Communication"],
}

DISCLAIMER_FLAT = "这是单次录音的表现不作为考试总分预估"
DISCLAIMER_RAW = "这是单次录音的表现，不作为考试总分预估"

# 家长版禁用词（不得让家长看出有工具链参与）。「数据 / 样本 / 测量」是能看出有工具链
# 参与的词，教师不会自然地这么写，所以保持无条件 error，不随下面那组放宽。
BANNED_PARENT = ["人工智能", "引擎", "评测", "讯飞", "Whisper", "识别", "转写",
                 "数据", "检测", "测量", "样本", "算法", "模型"]
AI_TOKEN = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])")
# 家长版禁止的技术指标，分两级：
#   ① TECH_PARENT_HARD —— 词本身就带量化含义（词/分、个词、字数…），出现即 error；
#   ② TECH_PARENT_COND —— 「停顿 / 语速」是教师会自然说出口的话（「几乎没有停顿」「语速偏快」），
#      只有同一行里带量化线索时才算技术指标 → error；不带则只 warning（见 quantified()）。
TECH_PARENT_HARD = ["词/分", "词／分", "词每分", "词/分钟", "词／分钟", "字数",
                    "平均每题"]
TECH_PARENT_COND = ["停顿", "语速"]
# 带数字才算的技术指标（正则型）：`18.8 个词` 是词数统计，`三个普通的词` 是正常描述。
# 只按字面拦「个词」会把后者误判成技术指标，所以要求前面带阿拉伯数字。
TECH_PARENT_PAT = [(re.compile(r"\d+\s*个词"), "个词（词数统计）")]
# 量化线索：数字必须与「停顿 / 语速」**紧挨着**才算技术指标。只按"同一行有数字"判会误伤
# 正常句子——「5 道题全程没有沉默，几乎没有停顿」里的 5 是题数，与停顿无关。
# 认这三种形态（`[^0-9，。；！？,;\n]{0,8}` = 词与数字之间只允许短虚词，越不过标点）：
#   停顿 12 次 / 停顿了 3 次 / 停顿次数 3 / 语速 122 / 平均语速 120
#   12 次停顿 / 3 处停顿
#   停顿占比 15% / 停顿 15%
QUANT_PAT = re.compile(
    r"(?:停顿|语速)[^0-9，。；！？,;\n]{0,8}\d"
    r"|\d+\s*(?:次|处|个|秒|%|％|分钟|词)?\s*(?:的)?\s*(?:停顿|语速)"
    r"|(?:停顿|语速)[^，。；！？,;\n]{0,6}[%％]"
)
# 行首序号（`2.` / `3、` / `（4）`）不是量化线索：家长版的问题清单本来就是编号列表。
LIST_MARKER_PAT = re.compile(r"^\s*(?:[-*•]\s*)?[(（]?\d{1,2}[.、)）]\s*")
# 单篇作业不得出现：总分 / 得分率 / 合格判定
TOTAL_TOKENS_COMMON = ["得分率", "加权总分", "未达", "合格水平", "过没过", "量表分"]
# 家长版再加"总分"：报告正文里除了免责句，不该出现这个词（内部档案里说明规则可以出现，见下）。
# 唯一豁免就是免责句本身——它含"不作为考试总分预估"这个固定说法。
TOTAL_TOKENS_PARENT = TOTAL_TOKENS_COMMON + ["总分"]
TOTAL_EXEMPT_PAT = re.compile(r"不作为考试总分预估")
# 作业记录草稿形态：老师习惯先落库后补分，frontmatter 写明状态即降级（或命令行 --draft）
DRAFT_STATUS_PAT = re.compile(r"^状态\s*[:：]\s*(?:草稿|draft)\s*$", re.I | re.M)
# 防编造（报告引用的学生原句 ↔ 转写稿）相似度阈值，实测定档：
#   逐字照抄 1.00 ｜ 只差标点/分词 0.99 ｜ 少一个词 0.93 ｜ 换掉一个词 0.82 ｜ 完全编造 0.68
#   ratio < QUOTE_ERROR            → error（疑似编造，必须回音频核对）
#   QUOTE_ERROR ≤ ratio < QUOTE_PASS → warning（引用与原文有出入，请核对）
#   ratio ≥ QUOTE_PASS             → 通过（"少一个词"0.93 必须落在这一档，不许误伤）
# 可用 --quote-threshold / --quote-warn-threshold 覆盖，两个常量见 references/06-verification.md §8.4。
QUOTE_ERROR = 0.85
QUOTE_PASS = 0.92
DENOM_PAT = re.compile(r"/\s*(?:30|45|60)(?![\d])")
RAW100_PAT = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d+)?\s*/\s*100(?![\d])")
SCORE5_PAT = re.compile(r"(-?\d+(?:\.\d+)?)\s*/\s*5(?![\d.])")
PLACEHOLDER_BRACKET = re.compile(r"〔[^〕\n]{0,80}〕")
PLACEHOLDER_BRACE = re.compile(r"\{[^{}\n]{1,80}\}")
NEG_CHARS = "不无没未禁免勿别非"
NO_INTERLOCUTOR = ["自问自答", "独白", "独自录音", "独自", "没有对手方", "无对手方",
                   "单说话人", "一个人说"]
IMAGE_HINT = ["描述照片", "两图", "看图", "图片类", "in the picture", "in this photo",
              "the photo shows", "photo", "picture"]
IMAGE_CLAIM = ["描述完整性", "描述是否完整", "描述准确性", "内容切题", "说错了", "描述错误"]

STUDENT_SECTIONS = [
    ("你这次做到的三件事", ["你这次做到的三件事", "做到的三件事", "做到的三点"]),
    ("本次预估分", ["本次预估分", "预估分", "本次分数"]),
    ("逐句修改", ["逐句修改", "逐句改", "逐句修改表"]),
    ("升级表达", ["升级表达", "可以直接背", "升级说法"]),
    ("下次课当场验收", ["下次课当场验收", "当场验收", "下次课验收"]),
]
RECORD_SECTIONS = [
    ("作业内容", ["作业内容", "作业说明", "任务内容"]),
    ("提交记录", ["提交记录", "提交信息", "提交情况"]),
    ("批改与反馈", ["批改与反馈", "批改记录", "批改与评价"]),
    ("分项评分", ["分项评分", "四项预估", "分项预估", "分项分"]),
    ("转化", ["转化", "后续动作", "待办"]),
]
RECORD_SOFT_SECTIONS = ["家长反馈", "硬指标", "教师观察", "抽听点位"]

EN_RUN = re.compile(r"[A-Za-z][A-Za-z'\u2019\-]*(?:[ ,.!?;:]+[A-Za-z][A-Za-z'\u2019\-]*)+")
CORRECTION_MARKERS = ["应改为", "应该是", "应该改成", "应为", "改后", "改成"]
ENGLISH_SENTENCE_SPLIT = re.compile(r"[.!?]+")


class Report:
    """一份报告文本 + 错误/警告收集器。行号从 1 开始；0 表示全文级问题。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.text = path.read_text(encoding="utf-8")
        self.lines = self.text.splitlines()
        self.errors: list[tuple[int, str]] = []
        self.warnings: list[tuple[int, str]] = []
        # frontmatter 是机器字段（能力点、状态…），不当正文看：那里的"话语组织：答句太短"
        # 是能力点名称，不是维度分数行。检测时跳过开头的空行与 HTML 注释。
        self.frontmatter_end = 0
        self.frontmatter = ""
        idx = 0
        while idx < len(self.lines):
            probe = self.lines[idx].strip()
            if probe == "" or probe.startswith("<!--"):
                idx += 1
                continue
            break
        if idx < len(self.lines) and self.lines[idx].strip() == "---":
            for j in range(idx + 1, len(self.lines)):
                if self.lines[j].strip() == "---":
                    self.frontmatter_end = j + 1
                    self.frontmatter = "\n".join(self.lines[idx + 1:j])
                    break

    def body(self) -> list[tuple[int, str]]:
        """正文行（行号, 内容），frontmatter 已剔除。"""
        return [(i, line) for i, line in enumerate(self.lines, 1) if i > self.frontmatter_end]

    def is_draft(self) -> bool:
        """frontmatter 里写了「状态: 草稿 / 状态：草稿 / 状态: draft」。"""
        return DRAFT_STATUS_PAT.search(self.frontmatter) is not None

    def error(self, line: int, msg: str) -> None:
        self.errors.append((line, msg))

    def warn(self, line: int, msg: str) -> None:
        self.warnings.append((line, msg))

    def lineno(self, needle: str) -> int:
        for i, line in enumerate(self.lines, 1):
            if needle in line:
                return i
        return 0

    def flat(self) -> str:
        """去掉空白与标点，用于"一句话必须原样出现"的比对。"""
        return re.sub(r"[\s，,。.；;：:、]+", "", self.text)


def negated(text: str, start: int, span: int = 14) -> bool:
    """判断命中位置前面一小段里有没有否定词（"不给加权总分"是规则说明，不算违规）。"""
    return any(c in NEG_CHARS for c in text[max(0, start - span):start])


def quantified(line: str, pos: int) -> bool:
    """该词所在行是否带量化线索 —— 决定「停顿 / 语速」算不算技术指标。

    「几乎没有停顿」「语速偏快」没有量化，是自然的教师话术；「停顿 12 次」「语速 122 词/分钟」
    才是技术指标。判定看**数字与这个词是否紧挨**（QUANT_PAT），不看"整行有没有数字"——
    后者会把「5 道题全程没有沉默，几乎没有停顿」这种句子误判成技术指标。
    """
    marker = LIST_MARKER_PAT.match(line)
    return bool(QUANT_PAT.search(line[marker.end():] if marker else line))


def dim_alternation(dim: str) -> str:
    """维度名 → 正则片段：中文官方译名 + 英文原名等价（大小写由调用方的 re.I 处理）。"""
    parts = [re.escape(dim)]
    for alias in DIM_ALIASES.get(dim, []):
        parts.append(re.escape(alias).replace("and", "(?:and|&)"))
    return "(?:" + "|".join(parts) + ")"


def dim_mentioned(line: str, dim: str) -> bool:
    """行文里提到该维度（中文或英文名），不要求它是维度行。"""
    return re.search(dim_alternation(dim), line, re.I) is not None


def dim_line_score(line: str, dim: str) -> tuple[float | None, str | None]:
    """从维度行里取分数：返回 (分数, 状态词)。分数为 None 表示没写数字分。"""
    m = SCORE5_PAT.search(line)
    if m:
        try:
            return float(m.group(1)), None
        except ValueError:
            return None, None
    for state in ("待补", "待填", "待判断", "N/A", "n/a", "无证据", "未取得"):
        if state in line:
            return None, state
    # 中文写法：发音：5 分
    m2 = re.search(r"(\d+(?:\.\d+)?)\s*分", line)
    if m2:
        return float(m2.group(1)), None
    return None, None


def is_dim_line(line: str, dim: str) -> bool:
    """这一行是不是该维度的分数行（表格首列，或行首「维度名：」）。中英文名都认。"""
    s = line.strip().lstrip("-*•> ").strip()
    pat = dim_alternation(dim)
    if s.startswith("|"):
        cells = [c.strip() for c in s.strip("|").split("|")]
        return bool(cells) and re.fullmatch(pat, cells[0].strip("*` "), re.I) is not None
    return re.match(rf"^{pat}\s*(?:[：:（(]|$)", s, re.I) is not None


def check_dimension_lines(rep: Report, kind: str, level: str) -> None:
    dims = DIMENSIONS[level]
    for dim in dims:
        hit = [(i, ln) for i, ln in rep.body() if is_dim_line(ln, dim)]
        if not hit:
            rep.error(0, f"缺少维度行「{dim}」（{LEVELS[level]} 必须有这几项：{'、'.join(dims)}）")
            continue
        line_no, line = hit[0]
        score, state = dim_line_score(line, dim)
        if score is None and state is None:
            if kind == "家长":
                rep.error(line_no, f"维度行「{dim}」既没有 X / 5 分数、也没有「待补 / N/A」标注：{line.strip()[:60]}")
            elif not PLACEHOLDER_BRACKET.search(line):
                rep.warn(line_no, f"维度行「{dim}」未写分数（内部档案若缺分需写明原因）：{line.strip()[:60]}")
            continue
        if score is None:
            continue
        if score < 0 or score > 5 or (score * 2) % 1 != 0:
            rep.error(line_no, f"维度分「{dim} = {score:g}」不在 0–5 区间内，或不是整数/半步（可半分，如 3.5）")
            continue
        if kind in ("学生", "作业记录"):
            continue
        # 家长版：不许给独白/自问自答录音的互动交际打分，也不许用分数糊弄
        if dim == "互动交际" and any(k in rep.text for k in NO_INTERLOCUTOR):
            rep.error(line_no, "互动交际给了分数，但文中写明本次是独自录音/无常人对手方——"
                               "独白与自问自答必须写「待补」并说明原因（references/03-scoring-rules.md 第五节）")


def check_placeholders(rep: Report, draft: bool = False) -> int:
    """空占位符残留 = 还没写完。三种成品都查。

    作业记录草稿形态（frontmatter 写了「状态: 草稿」或命令行 --draft）下降为 warning：
    老师习惯先落库后补分。返回占位符处数，供 stdout 摘要显式标注这是草稿。
    """
    report = rep.warn if draft else rep.error
    count = 0
    for i, line in enumerate(rep.lines, 1):
        bracketed = False
        for ph in PLACEHOLDER_BRACKET.finditer(line):
            bracketed = True
            count += 1
            report(i, f"残留占位符「{ph.group(0)[:24]}」：交付前必须填完")
        for ph in PLACEHOLDER_BRACE.finditer(line):
            bracketed = True
            count += 1
            report(i, f"残留模板花括号「{ph.group(0)[:24]}」：交付前必须填完")
        if "待填" in line and not bracketed:
            count += 1
            report(i, "残留占位符「待填」：交付前必须填完（缺测的维度写「待补」）")
    return count


def check_parent_redlines(rep: Report) -> None:
    for i, line in enumerate(rep.lines, 1):
        for word in BANNED_PARENT:
            if word in line:
                rep.error(i, f"家长版出现禁用词「{word}」：不能暴露工具链参与，改成教师口吻")
        m = AI_TOKEN.search(line)
        if m:
            rep.error(i, "家长版出现禁用词「AI」：不能暴露工具链参与，改成教师口吻")
        for tech in TECH_PARENT_HARD:
            if tech in line:
                rep.error(i, f"家长版出现技术指标「{tech}」：家长版只放官方口径的 X / 5 分项分")
        for pat, label in TECH_PARENT_PAT:
            m = pat.search(line)
            if m:
                rep.error(i, f"家长版出现技术指标「{label}」：家长版只放官方口径的 "
                             f"X / 5 分项分")
        for tech in TECH_PARENT_COND:
            pos = 0
            while True:
                pos = line.find(tech, pos)
                if pos == -1:
                    break
                if quantified(line, pos):
                    rep.error(i, f"家长版出现带量化的技术指标「{tech}」：家长版只放官方口径的 "
                                 f"X / 5 分项分（把数字/次数删掉）")
                else:
                    rep.warn(i, f"家长版出现「{tech}」：若是非量化描述（如「几乎没有停顿」"
                                f"「语速偏快」）可忽略；一旦带数字/次数必须删除")
                pos += len(tech)
        m = RAW100_PAT.search(line)
        if m:
            rep.error(i, f"家长版出现引擎原始分「{m.group(0)}」：只放官方口径的分项分 X / 5")
        if line.count("|") >= 2 or line.count("｜") >= 2:
            rep.error(i, "家长版出现 markdown 表格（含 | 分隔行）：微信里会散，必须纯文字逐行写")


def check_parent_structure(rep: Report) -> None:
    if "本次评分" not in rep.text:
        rep.error(0, "缺少「一、本次评分」段：家长版六段结构的第一段必须有（references/04-feedback.md 2.2）")
    flat = rep.flat()
    if DISCLAIMER_FLAT not in flat:
        rep.error(rep.lineno("本次评分"),
                  f"缺少免责句，必须一字不改：「{DISCLAIMER_RAW}」")
    if "本次未涉及的部分" not in rep.text:
        rep.warn(0, "未写「六、本次未涉及的部分」段（家长版固定六段结构）")
    for section in ("做得好的地方", "存在的问题", "需要改进的方向", "回家"):
        if section not in rep.text:
            rep.warn(0, f"未找到家长版的「{section}」段（固定六段结构）")


def check_total_score_forbidden(rep: Report, kind: str, form: str, level: str) -> None:
    """单篇作业不得出现总分 / 得分率 / 合格判定；完整模拟才允许。

    「总分」在单篇家长版里一律 error，**唯一豁免是免责句本身**——那句必须一字不改地写
    「这是单次录音的表现，不作为考试总分预估。」，其中的"总分"不算违规。
    """
    if form == "完整模拟":
        return
    tokens = TOTAL_TOKENS_PARENT if kind == "家长" else TOTAL_TOKENS_COMMON
    for i, line in enumerate(rep.lines, 1):
        exempt = [m.span() for m in TOTAL_EXEMPT_PAT.finditer(line)]
        for token in tokens:
            pos = 0
            while True:
                pos = line.find(token, pos)
                if pos == -1:
                    break
                in_exempt = any(start <= pos < end for start, end in exempt)
                if not in_exempt and not negated(line, pos):
                    rep.error(i, f"单篇作业不得出现「{token}」"
                                 f"（只给分项分 X/5 + 免责句；总分/得分率/过没过只在完整模拟里给）")
                pos += len(token)
        for m in DENOM_PAT.finditer(line):
            if not negated(line, m.start()):
                rep.error(i, f"单篇作业不得出现满分分母「{m.group(0)}」"
                             f"（{LEVELS[level]} 口语原始满分 {FULL_MARK[level]}，"
                             f"只有覆盖全部 Part 的完整模拟才用它算总分）")


def check_a2_dimensions(rep: Report, level: str) -> None:
    if level != "KET":
        return
    for i, line in rep.body():
        if is_dim_line(line, "话语组织"):
            rep.error(i, "A2 Key 没有「话语组织」这一维度（英文名 Discourse Management 同），"
                         "报告里不得出现这一行（硬造一项就是错的）")
        elif dim_mentioned(line, "话语组织"):
            rep.warn(i, "A2 Key 报告里出现「话语组织」（Discourse Management）字样：若这不是在说明"
                        "「该级别无此项」，请删掉")


def check_workflow_rules(rep: Report, kind: str, level: str) -> None:
    if kind != "作业记录":
        return
    # 内部档案要保留引擎原始分 + 映射后的 X/5（references/02-rubric.md 5.3）
    has_raw = RAW100_PAT.search(rep.text) is not None or "引擎原始分" in rep.text
    for dim in ("发音",):
        hit = [(i, ln) for i, ln in rep.body() if is_dim_line(ln, dim)]
        for line_no, line in hit:
            score, _ = dim_line_score(line, dim)
            if score is not None and not has_raw:
                rep.warn(line_no, "内部档案给了发音 X/5，但全文没有引擎原始分（如 N/100）——"
                                  "规则是原始分与映射分都要留")
                break
    if "念题与标准题目差异" in rep.text or "多读" in rep.text:
        rep.warn(0, "文中出现念题差异字样：不得单独据此下结论（对齐窗口有 ±2–4 词边界误差），"
                    "必须回音频复核（references/01-task-map.md 5.1）")


def check_structure(rep: Report, kind: str) -> None:
    if kind == "学生":
        for label, synonyms in STUDENT_SECTIONS:
            if not any(s in rep.text for s in synonyms):
                rep.error(0, f"学生版缺结构段「{label}」（references/04-feedback.md 第三节）")
    elif kind == "作业记录":
        for label, synonyms in RECORD_SECTIONS:
            if not any(s in rep.text for s in synonyms):
                rep.error(0, f"作业记录缺结构段「{label}」（模板见 scripts/kpf_report.py）")
        for section in RECORD_SOFT_SECTIONS:
            if section not in rep.text:
                rep.warn(0, f"作业记录缺「{section}」段：落库件按模板应齐备")


def check_image_task(rep: Report) -> None:
    if any(k in rep.text for k in IMAGE_HINT) and any(k in rep.text for k in IMAGE_CLAIM):
        rep.warn(0, "文中涉及图片类题型与「描述完整性/切题」类结论：确认教师已提供图片；"
                    "未提供图片时不得评描述完整性、内容切题、有无描述错误"
                    "（references/01-task-map.md 6.4）")


# ---------------------------------------------------------------- 防编造检查

def norm_phrase(s: str) -> str:
    s = s.replace("\u2019", "'").lower()
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_transcript_words(path: Path) -> list[str]:
    """从转写 JSON 里取词序列。取不到就报错（绝不静默跳过校验）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都必须显式失败
        fail(f"转写稿无法解析为 JSON：{path}（{exc}）；防编造检查无法执行")
    words: list[str] = []
    texts: list[str] = []
    if isinstance(data, dict):
        raw_words = data.get("words")
        if isinstance(raw_words, list):
            for item in raw_words:
                if isinstance(item, dict):
                    tok = item.get("w") or item.get("word")
                    if tok:
                        words.append(str(tok))
                elif isinstance(item, str):
                    words.append(item)
        if isinstance(data.get("text"), str):
            texts.append(data["text"])
        segs = data.get("segments")
        if isinstance(segs, list):
            for seg in segs:
                if isinstance(seg, dict) and isinstance(seg.get("text"), str):
                    texts.append(seg["text"])
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                tok = item.get("w") or item.get("word")
                if tok:
                    words.append(str(tok))
                elif isinstance(item.get("text"), str):
                    texts.append(item["text"])
            elif isinstance(item, str):
                words.append(item)
    if not words and texts:
        words = re.findall(r"[A-Za-z0-9'\u2019\-]+", " ".join(texts))
    # 有的外部转写工具导出的是 `transcriptResult` 字段里再套一层 JSON 字符串的结构，
    # 词在 `ps[].words[].text`（type16 归档格式）。2026-09 以前那几批录音只有这种格式，不认它防编造就整批跑不了。
    if not words and isinstance(data, dict) and isinstance(data.get("transcriptResult"), str):
        try:
            inner = json.loads(data["transcriptResult"])
        except Exception:  # noqa: BLE001 - 解不开就走到下面的统一报错
            inner = None
        if isinstance(inner, dict):
            for para in inner.get("ps") or []:
                if not isinstance(para, dict):
                    continue
                for item in para.get("words") or []:
                    if isinstance(item, dict):
                        tok = item.get("w") or item.get("text")
                        if tok:
                            words.append(str(tok))
                    elif isinstance(item, str):
                        words.append(item)
    if not words:
        fail(f"转写稿里没有可用的词序列（{path}）：需要 words[].w、text，"
             f"或外部转写稿 type16 的 transcriptResult.ps[].words[].text；防编造检查无法执行")
    cleaned = [norm_phrase(w) for w in words]
    return [w for w in cleaned if w]


def best_ratio(cand: str, twords: list[str], tflat: str) -> float:
    if cand and cand in tflat:
        return 1.0
    cw = cand.split()
    n = len(cw)
    if n == 0:
        return 0.0
    best = 0.0
    for i in range(max(1, len(twords) - n + 1)):
        window = " ".join(twords[i:i + n])
        r = difflib.SequenceMatcher(None, cand, window).ratio()
        if r > best:
            best = r
            if best >= 0.999:
                break
    return best


def extract_student_quotes(rep: Report) -> list[tuple[int, str, str]]:
    """抽出报告里"属于学生的原句"：

    ① `他说的 X 应为 Y` 里的 X；
    ② 表格里的原句列——表头写了「原句 / 你说的」的表格，按那一列取（`| # | 你说的 | 改后 | 为什么 |`
       与 `| 原句 | 应为 | 类型 |` 两种排法都能认）。
    """
    quotes: list[tuple[int, str, str]] = []
    original_col: int | None = None
    for i, line in enumerate(rep.lines, 1):
        cut = None
        for marker in CORRECTION_MARKERS:
            pos = line.find(marker)
            if pos != -1 and (cut is None or pos < cut):
                cut = pos
        if cut is not None:
            prefix = line[:cut]
            runs = [r for r in EN_RUN.findall(prefix) if len(r.split()) >= 2]
            if runs:
                quotes.append((i, max(runs, key=len), "原文引用"))
        if line.count("|") >= 2:
            cells = [c.strip().strip("*` ") for c in line.strip().strip("|").split("|")]
            head = next((idx for idx, c in enumerate(cells) if c in ("原句", "你说的", "你说的句子")), None)
            if head is not None:                      # 表头：记住原句在第几列
                original_col = head
                continue
            if original_col is not None and len(cells) > original_col:
                cell = cells[original_col]
            elif len(cells) >= 3 and (re.fullmatch(r"#|\d+", cells[0]) or cells[0] == "原句"):
                cell = cells[1]
            else:
                continue
            runs = [r for r in EN_RUN.findall(cell) if len(r.split()) >= 2]
            if runs:
                quotes.append((i, max(runs, key=len), "逐句修改表"))
        else:
            original_col = None                        # 离开表格就重置列记忆
    seen: set[str] = set()
    out: list[tuple[int, str, str]] = []
    for line_no, raw, src in quotes:
        key = norm_phrase(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((line_no, raw, src))
    return out


def check_fabrication(rep: Report, transcript: Path,
                      quote_error: float = QUOTE_ERROR, quote_pass: float = QUOTE_PASS) -> None:
    twords = load_transcript_words(transcript)
    tflat = " ".join(twords)
    quotes = extract_student_quotes(rep)
    if not quotes:
        rep.warn(0, "报告里没有找到可核验的学生原句引用（形如「他说的 X 应为 Y」或逐句修改表）："
                    "防编造检查这一项没有覆盖面")
        return
    for line_no, raw, src in quotes:
        ratio = best_ratio(norm_phrase(raw), twords, tflat)
        if ratio < quote_error:
            rep.error(line_no, f"引用的学生原句在转写稿中找不到（{src}，最相近片段相似度 "
                               f"{ratio:.2f} < 阈值 {quote_error:g}）：「{raw}」——"
                               f"疑似编造，必须回音频/转写核对后再交付")
        elif ratio < quote_pass:
            rep.warn(line_no, f"引用与转写原文有出入（{src}，相似度 {ratio:.2f}，"
                              f"低于通过线 {quote_pass:g}）：「{raw}」"
                              f"——转写原文要照抄，包括 he go / didn't been 这类不规范形式；"
                              f"如果确实改过措辞，请改回照抄")


def check_english_quote_budget(rep: Report) -> None:
    """家长版：整篇英文原话合计不超过 6 句。

    只数「做得好的地方」到「需要改进的方向」这两段里的英文（学生原话就住在这里），
    并且把 `应为…`／`改后…` 之后的那半句剔掉——改后的表述不算"学生原话"。
    """
    lines = rep.lines
    start = next((i for i, l in enumerate(lines) if "做得好的地方" in l), None)
    end = next((i for i, l in enumerate(lines) if "需要改进的方向" in l), None)
    if start is None or end is None or end <= start:
        # 结构不规范时退化成全文（跳过标题块），宁可多报一句，也不静默不查
        body_start = next((i for i, l in enumerate(lines) if l.strip() == ""), 0)
        window = lines[body_start:]
    else:
        window = lines[start:end]
    body_lines = []
    for line in window:
        cut = None
        for marker in CORRECTION_MARKERS:
            pos = line.find(marker)
            if pos != -1 and (cut is None or pos < cut):
                cut = pos
        body_lines.append(line[:cut] if cut is not None else line)
    body = "\n".join(body_lines)
    sentences = 0
    for run in EN_RUN.findall(body):
        for piece in ENGLISH_SENTENCE_SPLIT.split(run):
            if len(piece.split()) >= 2:
                sentences += 1
    if sentences > 6:
        rep.warn(0, f"家长版英文原话约 {sentences} 句，超过 6 句上限："
                    f"原话与 4–6 条问题一一对应即可，别每条都堆原句")


def check_complete_mock(rep: Report, form: str, level: str) -> None:
    """完整模拟才给总分；给了这个 form 却没有总分，多半是 form 填错了。"""
    if form != "完整模拟":
        return
    if "总分" not in rep.text and "得分率" not in rep.text:
        rep.warn(0, f"--form 完整模拟，但全文没有总分/得分率：{LEVELS[level]} 口语原始满分 "
                    f"{FULL_MARK[level]}（权重见 references/02-rubric.md 5.2）。"
                    f"若其实是单篇作业，--form 应填「单篇」")


def fail(msg: str) -> None:
    print(f"校验无法执行：{msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="KPF 口语作业报告输出合规校验（纯标准库、离线）")
    ap.add_argument("report", type=Path, help="报告文件（家长版 / 学生版 / 作业记录）")
    ap.add_argument("--kind", choices=["家长", "学生", "作业记录"], required=True)
    ap.add_argument("--form", choices=["单篇", "完整模拟"], required=True)
    ap.add_argument("--level", choices=["KET", "PET", "FCE"], required=True)
    ap.add_argument("--transcript", type=Path, default=None,
                    help="转写 JSON；给了就启用防编造检查（报告引用的学生原句必须在转写里找得到）")
    ap.add_argument("--draft", action="store_true",
                    help="作业记录草稿形态：占位符残留由 error 降为 warning，其余检查不变；"
                         "只对 --kind 作业记录 有效")
    ap.add_argument("--quote-threshold", type=float, default=QUOTE_ERROR,
                    help=f"防编造 error 边界：相似度低于此值判 error（默认 {QUOTE_ERROR}）")
    ap.add_argument("--quote-warn-threshold", type=float, default=None,
                    help=f"防编造通过边界：相似度 ≥ 此值判通过，介于两边界之间给 warning"
                         f"（默认 {QUOTE_PASS}；只给 --quote-threshold 时自动取 "
                         f"max({QUOTE_PASS}, --quote-threshold)）")
    args = ap.parse_args()

    if args.draft and args.kind != "作业记录":
        ap.error("--draft 只适用于 --kind 作业记录：家长版与学生版没有草稿形态，"
                 "交付前必须填完（占位符残留仍是 error）")
    quote_pass = args.quote_warn_threshold
    if quote_pass is None:
        quote_pass = max(QUOTE_PASS, args.quote_threshold)
    elif quote_pass <= args.quote_threshold:
        ap.error(f"--quote-warn-threshold 必须大于 --quote-threshold"
                 f"（现为 {quote_pass:g} ≤ {args.quote_threshold:g}）")

    if not args.report.is_file():
        fail(f"报告文件不存在：{args.report}")
    if args.transcript is not None and not args.transcript.is_file():
        fail(f"转写稿不存在：{args.transcript}")

    rep = Report(args.report)
    # 草稿形态：命令行给 --draft，或作业记录的 frontmatter 自己写了「状态: 草稿」
    draft_mode = args.kind == "作业记录" and (args.draft or rep.is_draft())
    placeholder_count = check_placeholders(rep, draft=draft_mode)

    if args.kind == "家长":
        check_parent_redlines(rep)
        check_parent_structure(rep)
        check_dimension_lines(rep, args.kind, args.level)
        check_total_score_forbidden(rep, args.kind, args.form, args.level)
        check_a2_dimensions(rep, args.level)
        check_english_quote_budget(rep)
    else:
        if args.kind == "学生":
            for i, line in enumerate(rep.lines, 1):
                for word in ("讯飞", "Whisper"):
                    if word in line:
                        rep.warn(i, f"学生版出现工具名「{word}」：学生版可给技术指标，但不必暴露引擎品牌")
        check_structure(rep, args.kind)
        check_dimension_lines(rep, args.kind, args.level)
        check_total_score_forbidden(rep, args.kind, args.form, args.level)
        check_a2_dimensions(rep, args.level)
        check_workflow_rules(rep, args.kind, args.level)

    check_image_task(rep)
    check_complete_mock(rep, args.form, args.level)

    if args.transcript is not None:
        check_fabrication(rep, args.transcript, args.quote_threshold, quote_pass)

    for line_no, msg in rep.warnings:
        where = f"L{line_no}" if line_no else "全文"
        print(f"{where} [warning] {msg}", file=sys.stderr)

    if rep.errors:
        print(f"校验未通过：{args.report}（{args.kind}/{args.form}/{LEVELS[args.level]}）",
              file=sys.stderr)
        for line_no, msg in rep.errors:
            where = f"L{line_no}" if line_no else "全文"
            print(f"  {where} [error] {msg}", file=sys.stderr)
        print(f"FAIL {args.kind}/{args.form}/{LEVELS[args.level]} · {args.report} · "
              f"{len(rep.errors)} error / {len(rep.warnings)} warning")
        sys.exit(1)

    summary = (f"PASS {args.kind}/{args.form}/{LEVELS[args.level]} · {args.report} · "
               f"0 error / {len(rep.warnings)} warning")
    if draft_mode:
        summary += (f"（草稿形态：占位符 {placeholder_count} 处未填，交付前必须补全；"
                    f"转正式后这些是 error）")
    print(summary)


if __name__ == "__main__":
    main()
