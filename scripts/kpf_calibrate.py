#!/usr/bin/env python3
"""KPF 口语作业 · AI 分 × 真人分 校准

把「AI 给的分到底准不准」变成数字：逐维度完全一致率 / ±1 档一致率、系统性偏差、
混淆矩阵、分歧清单、一行结论。

为什么要有这个：references/06-verification.md 的「真人校准」项长期是 ⬜。
没有这条流水线，AI 偏松还是偏紧只能靠感觉，改了规则也无从验证是不是变好了。

纯标准库 · 离线 · 零外部依赖：不联网、不读凭证、不碰音频与转写文件，
也不重新评判任何分数——只做纯数字比对。只读输入，除了 --out 不写任何文件。

一个 case 一个目录：

  <case>/teacher.json   必需  老师的真实给分（格式见 templates/teacher-marks.json）
  <case>/ai-分项.json    可选  AI 四项预估，机器可读（格式见 templates/ai-marks.json）
  <case>/作业记录.md     可选  没有 ai-分项.json 时的兜底：从「分项评分 / 四项预估」
                             表格里解析 AI 的分。认这几种形态：
                               | 语法与词汇 | **2** | AI 预估 | 依据 |
                               | 发音 | 79.6 / 100（≈3/5，念题部分） | 老师 | … |
                               | 话语组织 | 2.5 / 5 | AI | … |
                             解析不出来就报错，绝不静默跳过这个 case。

用法：
  kpf_calibrate.py <case目录>...
  kpf_calibrate.py --root <工作区目录>        # 扫一级子目录，含 teacher.json 的才算 case
  kpf_calibrate.py <case目录>... --min-agreement 0.9 --out 报告.txt

主指标是 ±1 档一致率（并列给出完全一致率，各自带分母）。±1 档是包含关系：完全一致的那几点
也算在 ±1 档内，两者不能相加。--ai-party 控制 AI 侧取值：
默认 any（预估表里每一行都算 AI 侧——发音行常写成「评测引擎/教师」，那也仍是机器分）；
ai-only 时只认评分方写 AI 的行，其余进「来源存疑」。

退出码：
  0 = 正常（含「样本不足 3 个，仅作冒烟」——样本少不判红）
  1 = ±1 档一致率低于 --min-agreement（精准度红灯）
  2 = 用法错误，或输入不合法到算不出报告（缺 teacher.json、级别与维度不符、
      AI 来源解析不到、档位越界、一个 case 都没给）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

LEVELS = {"KET": "A2 Key", "PET": "B1 Preliminary", "FCE": "B2 First"}
# 维度按级别给：A2 Key 只有 3 项，没有话语组织（references/02-rubric.md 第一节）
DIMENSIONS = {
    "KET": ["语法与词汇", "发音", "互动交际"],
    "PET": ["语法与词汇", "话语组织", "发音", "互动交际"],
    "FCE": ["语法与词汇", "话语组织", "发音", "互动交际"],
}
# 与 kpf_validate.py 的 DIM_ALIASES 同口径：中文名是主口径，英文原名等价识别
DIM_ALIASES = {
    "语法与词汇": ["grammar and vocabulary"],
    "话语组织": ["discourse management"],
    "发音": ["pronunciation"],
    "互动交际": ["interactive communication"],
}
LEVEL_ALIASES = {
    "ket": "KET", "key": "KET", "a2": "KET", "a2 key": "KET", "a2key": "KET",
    "pet": "PET", "b1": "PET", "b1 preliminary": "PET", "preliminary": "PET",
    "fce": "FCE", "b2": "FCE", "b2 first": "FCE", "first": "FCE",
}
BAND_MIN, BAND_MAX = 0.0, 5.0
# 少于此数量的 case 只作冒烟：数字照出，但不判红——两三条样本判红只会让人学会忽略红灯
MIN_SAMPLE = 3
# 表格落在这几个表头下才算 AI 预估表（作业记录模板写「分项评分」，实际文件里常写「四项预估」）
AI_HEAD_KEYS = ["分项评分", "四项预估", "分项预估", "分项分", "AI 预估", "AI预估", "预估分"]
AI_FILES = ["ai-分项.json", "ai分项.json", "ai-marks.json"]
MD_CANDIDATES = ["作业记录.md"]
PLACEHOLDER = re.compile(r"〔[^〕\n]*〕")
NO_EVIDENCE = ("待补", "n/a", "na", "none", "无", "—", "-", "–", "不适用")
DIM_ORDER = {d: i for i, d in enumerate(["语法与词汇", "话语组织", "发音", "互动交际"])}


class InputError(Exception):
    """输入不合法——报告根本算不出来。

    与「算出来了但一致率低」严格区分：后者才是 exit 1 的红灯，前者是 2。
    否则一次手抖写错文件名就会被当成规则退化，红灯很快没人信。
    """


def fail(msg: str) -> None:
    print(f"无法校准：{msg}", file=sys.stderr)
    sys.exit(2)


def fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def fmt_diff(d: float) -> str:
    if d == 0:
        return "0"
    return f"{d:+.1f}" if not float(d).is_integer() else f"{d:+.0f}"


def fmt_mean(d: float) -> str:
    """均值留两位小数：+1.10 与 +1.1 看起来是同一件事，但舍成一位会把 0.05 的漂移抹掉。"""
    d = round(d, 2)
    return "0.00" if d == 0 else f"{d:+.2f}"


def wpad(s: str, width: int) -> str:
    """按显示宽度补空格：中文一字占两列，用 len() 会让表头和数字列错位。"""
    w = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
    return s + " " * max(0, width - w)


# ---------- 输入解析 ----------

def norm_level(raw, where: str) -> str:
    key = re.sub(r"\s+", " ", str(raw).strip().lower().replace("for schools", "").strip())
    if key in LEVEL_ALIASES:
        return LEVEL_ALIASES[key]
    raise InputError(f"{where} 的 level「{raw}」不认：填 KET / PET / FCE"
                     f"（或 A2 Key / B1 Preliminary / B2 First）")


def norm_dim(raw) -> str | None:
    """维度名归一：去 markdown 加粗与括号补充，再认英文别名（与校验器同一份对照）。"""
    t = re.sub(r"[*`_\s]", "", str(raw)).split("：")[0].split(":")[0]
    t = re.sub(r"[（(].*?[)）]", "", t).strip()
    if t in DIMENSIONS["FCE"]:
        return t
    low = re.sub(r"\s+", " ", t.lower().replace("&", "and"))
    for dim, aliases in DIM_ALIASES.items():
        if low == dim.lower() or low in [a.replace("&", "and") for a in aliases]:
            return dim
    return None


def norm_band(raw, where: str) -> float | None:
    """档位校验：0–5、半步。null / 空串 /「待补」= 无证据。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise InputError(f"{where} 的档位不能是布尔值：{raw!r}")
    if isinstance(raw, str):
        s = raw.strip()
        if s == "" or s.lower() in NO_EVIDENCE:
            return None
        raw = s
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise InputError(f"{where} 的档位「{raw}」不是数字（0–5，可半分；无证据写 null）")
    if v < BAND_MIN or v > BAND_MAX or abs(v * 2 - round(v * 2)) > 1e-9:
        raise InputError(f"{where} 的档位 {raw} 越界：只能 0–5、步长 0.5（如 2.5）")
    return round(v, 1)


def check_dim_keys(dims: dict, level: str, where: str) -> None:
    """level 与维度必须自洽：level 写 KET 却出现话语组织，就是拿假维度当分母。"""
    expect = DIMENSIONS[level]
    unknown = [d for d in dims if d not in expect]
    if unknown:
        hint = ""
        if level == "KET":
            hint = "；A2 Key 没有「话语组织」（英文 Discourse Management 一样算错）"
        raise InputError(f"{where} 出现 {level}（{LEVELS[level]}）不该有的维度 {unknown}{hint}。"
                         f"应有维度：{'、'.join(expect)}")
    missing = [d for d in expect if d not in dims]
    if missing:
        raise InputError(f"{where} 缺维度 {missing}：{LEVELS[level]} 要写全 {'、'.join(expect)}，"
                         f"没评的写 null")


def load_teacher(case: Path) -> dict:
    path = case / "teacher.json"
    if not path.is_file():
        raise InputError(f"{case.name} 缺 teacher.json"
                         f"（老师真实给分，格式见 templates/teacher-marks.json）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise InputError(f"{path} 不是合法 JSON：{e}")
    if not isinstance(data, dict):
        raise InputError(f"{path} 顶层应是对象")
    where = f"{case.name}/teacher.json"
    level = norm_level(data.get("level", ""), where)
    dims_raw = data.get("dimensions")
    if not isinstance(dims_raw, dict):
        raise InputError(f"{where} 缺 dimensions 对象")
    dims: dict[str, float | None] = {}
    for k, v in dims_raw.items():
        dim = norm_dim(k)
        if dim is None:
            raise InputError(f"{where} 的维度「{k}」不认："
                             f"只能是 语法与词汇 / 话语组织 / 发音 / 互动交际")
        dims[dim] = norm_band(v, f"{where} {dim}")
    check_dim_keys(dims, level, where)
    notes_raw = data.get("notes")
    notes = ({norm_dim(k) or k: str(v) for k, v in notes_raw.items()}
             if isinstance(notes_raw, dict) else {})
    conf = str(data.get("confidence") or "")
    return {
        "case_id": str(data.get("case_id") or case.name),
        "level": level,
        "form": str(data.get("form") or ""),
        "homework": str(data.get("homework") or ""),
        "dims": dims,
        "notes": notes,
        "confidence": conf if conf in ("low", "medium", "high") else "",
    }


def find_one(case: Path, names: list[str], glob: str | None, label: str) -> Path | None:
    hits = [case / n for n in names if (case / n).is_file()]
    if glob:
        hits += [p for p in sorted(case.glob(glob)) if p.is_file() and p not in hits]
    if not hits:
        return None
    if len(hits) > 1:
        raise InputError(f"{case.name} 里有多个{label}：{[p.name for p in hits]}；"
                         f"留一个，否则不知道以哪份为准")
    return hits[0]


def parse_ai_json(case: Path, level: str) -> tuple[dict, str] | None:
    path = find_one(case, AI_FILES, None, " ai-分项.json")
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise InputError(f"{path} 不是合法 JSON：{e}")
    if not isinstance(data, dict):
        raise InputError(f"{path} 顶层应是对象")
    where = f"{case.name}/{path.name}"
    dims_raw = data.get("dimensions")
    if not isinstance(dims_raw, dict):
        raise InputError(f"{where} 缺 dimensions 对象（格式见 templates/ai-marks.json）")
    ai: dict[str, dict] = {}
    for k, v in dims_raw.items():
        dim = norm_dim(k)
        if dim is None:
            raise InputError(f"{where} 的维度「{k}」不认")
        if v is None or isinstance(v, (int, float, str)):
            ai[dim] = {"band": norm_band(v, f"{where} {dim}"), "evidence": [], "reason": ""}
        elif isinstance(v, dict):
            ai[dim] = {
                "band": norm_band(v.get("band"), f"{where} {dim}"),
                "evidence": [str(x) for x in (v.get("evidence") or []) if str(x).strip()],
                "reason": str(v.get("reason") or ""),
            }
        else:
            raise InputError(f"{where} 的 {dim} 应是对象（band/evidence）或裸数字")
    check_dim_keys(ai, level, where)
    note = ""
    if data.get("level") and norm_level(data["level"], where) != level:
        note = f"（{path.name} 的 level 与 teacher.json 不一致，按老师的算）"
    return ai, f"ai-分项.json{note}"


def detect_party(cells: list[str]) -> tuple[str, bool]:
    """评分方列 → (原样文本, 是否 AI)。没写评分方列时算 AI（表头本身就叫「预估」表）。"""
    for cell in cells[2:]:
        low = cell.lower()
        if any(k in low for k in ("ai", "a.i.")):
            return cell, True
        if any(k in cell for k in ("老师", "教师", "teacher")):
            return cell, False
        if any(k in cell for k in ("引擎", "评测", "engine")):
            return cell, False
    return "", True


def band_from_cell(cell: str) -> tuple[float | None, str]:
    """从单元格取档位。只认显式档位，绝不自己把「引擎原始分/100」换算成档——
    to_band() 的阈值表唯一实现在 kpf_xfyun.py，这里再复制一份就是漂移源头。"""
    t = cell.replace("**", "").replace("*", "").strip()
    if not t:
        return None, ""
    m = re.search(r"[≈≃~约]\s*([0-5](?:\.5)?)", t)
    if m:
        return float(m.group(1)), ""
    m = re.search(r"(?<![\d.])([0-5](?:\.5)?)\s*/\s*5(?![\d.])", t)
    if m:
        return float(m.group(1)), ""
    m = re.fullmatch(r"([0-5](?:\.5)?)\s*分?", t)
    if m:
        return float(m.group(1)), ""
    if re.search(r"/\s*100", t):
        return None, f"只给了引擎原始分「{t}」：请在这一行补写 ≈n/5，或改用 ai-分项.json"
    if PLACEHOLDER.search(t):
        return None, "占位符还没填"
    if t.lower() in NO_EVIDENCE:
        return None, f"表格里写的是「{t}」（无证据）"
    return None, f"读不出档位：「{t}」"


def parse_ai_markdown(case: Path, level: str) -> tuple[dict, str] | None:
    path = find_one(case, MD_CANDIDATES, "*作业记录*.md", "作业记录 markdown")
    if path is None:
        return None
    head = ""
    ai: dict[str, dict] = {}
    warns: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        m = re.match(r"^#{1,6}\s*(.+?)\s*$", line) or re.fullmatch(r"\*\*(.+?)\*\*", line)
        if m:
            head = m.group(1)
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        dim = norm_dim(cells[0])
        if dim is None:
            continue
        in_ai_table = any(k in head for k in AI_HEAD_KEYS)
        party_text, is_ai = detect_party(cells)
        if not in_ai_table and not is_ai:
            continue
        band, why, score_idx = None, "", -1
        for idx in range(1, len(cells)):
            got, why2 = band_from_cell(cells[idx])
            if got is not None:
                band, score_idx = got, idx
                break
            why = why or why2
        party_idx = cells.index(party_text) if party_text and party_text in cells else -1
        evidence = [c for j, c in enumerate(cells)
                    if j not in (0, score_idx, party_idx) and c and not PLACEHOLDER.search(c)]
        if dim in ai:
            if ai[dim]["band"] != band:
                warns.append(f"{path.name} L{lineno}：{dim} 在文档里出现多次且值不同"
                             f"（{fmt(ai[dim]['band'])} / {fmt(band)}），取第一次")
            continue
        ai[dim] = {"band": band, "evidence": evidence, "reason": why,
                   "line": lineno, "party": party_text}
    for w in warns:
        print(f"  [注意] {case.name}：{w}", file=sys.stderr)
    if not ai:
        raise InputError(f"{path} 里找不到「{'/'.join(AI_HEAD_KEYS[:3])}」表格里的 AI 分："
                         f"要么把表填上，要么给一份 ai-分项.json")
    if all(v["band"] is None for v in ai.values()):
        detail = "；".join(f"{d}：{v['reason']}" for d, v in ai.items() if v["reason"])
        raise InputError(f"{path} 的预估表里没有任何有效档位（{detail or '全是空的'}）："
                         f"没给分的记录不能拿来校准")
    return ai, "作业记录.md 兜底解析"


# ---------- 数据模型 ----------

@dataclass
class Point:
    case_id: str
    dim: str
    ai: float
    teacher: float
    teacher_note: str
    ai_evidence: list[str]
    confidence: str

    @property
    def diff(self) -> float:
        return round(self.ai - self.teacher, 1)


@dataclass
class Case:
    case_id: str
    dir: Path
    level: str
    form: str
    teacher: dict
    ai: dict
    ai_bands: dict = field(default_factory=dict)   # 按 --ai-party 过滤后的有效 AI 档位
    ai_source: str = ""
    unrated: list = field(default_factory=list)    # 老师 null：(维度, AI 档位, AI 说明, 老师理由)
    no_ai: list = field(default_factory=list)      # 老师给了、AI 没给：(维度, 老师档位, 说明)
    suspicious: list = field(default_factory=list)  # 评分方不是 AI：(维度, 档位, 评分方)

    def points(self) -> list[Point]:
        out: list[Point] = []
        for dim in DIMENSIONS[self.level]:
            t_band = self.teacher["dims"].get(dim)
            a_band = self.ai_bands.get(dim)
            if t_band is None or a_band is None:
                continue
            entry = self.ai.get(dim) or {}
            out.append(Point(case_id=self.case_id, dim=dim, ai=a_band, teacher=t_band,
                             teacher_note=self.teacher["notes"].get(dim, ""),
                             ai_evidence=list(entry.get("evidence") or []),
                             confidence=self.teacher["confidence"]))
        return out


def build_case(case_dir: Path, ai_party: str) -> Case:
    t = load_teacher(case_dir)
    parsed = parse_ai_json(case_dir, t["level"]) or parse_ai_markdown(case_dir, t["level"])
    if parsed is None:
        raise InputError(f"{case_dir.name} 既没有 {'/'.join(AI_FILES)}，也没有可解析的 "
                         f"{'/'.join(MD_CANDIDATES)}/*作业记录*.md")
    ai, source = parsed
    c = Case(case_id=t["case_id"], dir=case_dir, level=t["level"], form=t["form"],
             teacher=t, ai=ai, ai_source=source)
    for dim in DIMENSIONS[t["level"]]:
        entry = ai.get(dim) or {}
        a_band = entry.get("band")
        party = entry.get("party", "")
        if ai_party == "ai-only" and party and "ai" not in party.lower():
            c.suspicious.append((dim, a_band, party))
            a_band = None
        c.ai_bands[dim] = a_band
        t_band = t["dims"].get(dim)
        if t_band is None:
            c.unrated.append((dim, a_band, entry.get("reason", ""), t["notes"].get(dim, "")))
        elif a_band is None:
            c.no_ai.append((dim, t_band, entry.get("reason", "")))
    return c


def collect_cases(args_cases: list[Path], root: Path | None) -> list[Path]:
    dirs: list[Path] = []
    for p in args_cases:
        if not p.exists():
            raise InputError(f"路径不存在：{p}")
        if not p.is_dir():
            raise InputError(f"{p} 不是目录：case 目录里应有 teacher.json")
        dirs.append(p)
    if root is not None:
        if not root.is_dir():
            raise InputError(f"--root 不是目录：{root}")
        subdirs = sorted(d for d in root.iterdir() if d.is_dir())
        with_teacher = [d for d in subdirs if (d / "teacher.json").is_file()]
        print(f"--root {root}：扫了 {len(subdirs)} 个子目录，其中 {len(with_teacher)} 个含 "
              f"teacher.json（其余 {len(subdirs) - len(with_teacher)} 个不算 case，跳过）",
              file=sys.stderr)
        dirs += with_teacher
    seen, out = set(), []
    for d in dirs:
        key = d.resolve()
        if key not in seen:
            seen.add(key)
            out.append(d)
    if not out:
        raise InputError("一个 case 都没有：给若干 case 目录，或 --root 指一个含 case 的目录")
    return out


# ---------- 统计与报告 ----------

def per_dim_stats(points: list[Point]) -> dict[str, dict]:
    st: dict[str, dict] = {}
    for p in points:
        d = st.setdefault(p.dim, {"n": 0, "exact": 0, "within": 0, "signed": 0.0, "abs": 0.0,
                                  "over": 0, "under": 0})
        d["n"] += 1
        d["exact"] += 1 if p.diff == 0 else 0
        d["within"] += 1 if abs(p.diff) <= 1.0 + 1e-9 else 0
        d["signed"] += p.diff
        d["abs"] += abs(p.diff)
        d["over"] += 1 if p.diff > 0 else 0
        d["under"] += 1 if p.diff < 0 else 0
    for d in st.values():
        d["mean_signed"] = d["signed"] / d["n"]
        d["mean_abs"] = d["abs"] / d["n"]
    return st


def pct(a: int, b: int) -> str:
    return f"{a / b * 100:.1f}%" if b else "—"


def render_matrix(points: list[Point], title: str) -> list[str]:
    """混淆矩阵：行 = AI 给的档，列 = 老师给的档；两边都只列出现过的档位。"""
    rows = sorted({p.ai for p in points})
    cols = sorted({p.teacher for p in points})
    if not rows or not cols:
        return []
    counts = {(r, c): 0 for r in rows for c in cols}
    for p in points:
        counts[(p.ai, p.teacher)] += 1
    lw = max([len(fmt(r)) for r in rows] + [4]) + 1
    cw = max([len(fmt(c)) for c in cols] + [3]) + 2
    out = [f"【{title}】（行 = AI 给的档，列 = 老师给的档；·= 没有样本）"]
    out.append(" " * (lw + 3) + wpad("老师→", 6) + "".join(wpad(fmt(c), cw) for c in cols))
    for r in rows:
        body = "".join((str(counts[(r, c)]) if counts[(r, c)] else "·").ljust(cw) for c in cols)
        out.append(f"{fmt(r).rjust(lw)} │  {body}")
    out.append("")
    return out


def build_report(cases: list[Case], min_agreement: float, ai_party: str) -> tuple[str, bool]:
    points = [p for c in cases for p in c.points()]
    st = per_dim_stats(points)
    order = sorted(st, key=lambda d: DIM_ORDER.get(d, 99))
    used_cases = len({p.case_id for p in points})
    n = len(points)
    ex = sum(1 for p in points if p.diff == 0)
    wi = sum(1 for p in points if abs(p.diff) <= 1.0 + 1e-9)
    signed = sum(p.diff for p in points) / n if n else 0.0
    absmean = sum(abs(p.diff) for p in points) / n if n else 0.0
    rate = wi / n if n else 0.0
    smoke = used_cases < MIN_SAMPLE
    diffs = sorted([p for p in points if p.diff != 0], key=lambda p: (-abs(p.diff), p.case_id))
    big = sum(1 for p in diffs if abs(p.diff) >= 1.0 - 1e-9)

    L: list[str] = ["=" * 68, "KPF 口语 · AI 分 × 真人分 校准报告", "=" * 68]
    src_stat: dict[str, int] = {}
    for c in cases:
        src_stat[c.ai_source] = src_stat.get(c.ai_source, 0) + 1
    L.append(f"case {len(cases)} 个（进入统计 {used_cases} 个）· 可比维度点 {n} 个"
             f" · 阈值 --min-agreement {min_agreement:.0%} · AI 侧取值 {ai_party}")
    if order:
        L.append("维度点分布：" + " · ".join(f"{d} {st[d]['n']}" for d in order))
    L.append("AI 来源：" + " · ".join(f"{k} {v} 个" for k, v in sorted(src_stat.items())))
    L.append(f"老师未评（null）{sum(len(c.unrated) for c in cases)} 点 · "
             f"AI 缺值 {sum(len(c.no_ai) for c in cases)} 点 · 二者都不进统计")
    if smoke:
        L.append(f"⚠ 样本太少（进入统计的 case {used_cases} 个 < {MIN_SAMPLE}）：仅作冒烟，"
                 f"下面的数字不判红。")
    L.append("")

    L.append("一、逐维度一致率（主指标是 ±1 档，含完全一致）")
    L.append(wpad("维度", 12) + wpad("分母", 6) + wpad("完全一致", 17) + wpad("±1 档（含完全一致）", 20)
             + wpad("平均带符号差", 14) + "平均绝对差")
    for d in order:
        s = st[d]
        exact = f"{s['exact']}/{s['n']} ({pct(s['exact'], s['n'])})"
        within = f"{s['within']}/{s['n']} ({pct(s['within'], s['n'])})"
        L.append(wpad(d, 12) + wpad(str(s["n"]), 6) + wpad(exact, 17) + wpad(within, 20)
                 + wpad(fmt_mean(s["mean_signed"]), 14) + f"{s['mean_abs']:.2f}")
    L.append("-" * 68)
    tot_exact = f"{ex}/{n} ({pct(ex, n)})"
    tot_within = f"{wi}/{n} ({pct(wi, n)})"
    L.append(wpad("合计", 12) + wpad(str(n), 6) + wpad(tot_exact, 17) + wpad(tot_within, 20)
             + wpad(fmt_mean(signed), 14) + f"{absmean:.2f}")
    L.append("")

    L.append("二、系统性偏差")
    if abs(signed) < 0.25:
        direction = "基本无系统性偏差"
    elif signed > 0:
        direction = f"AI 整体偏高 {signed:.2f} 档"
    else:
        direction = f"AI 整体偏低 {abs(signed):.2f} 档"
    L.append(f"整体：平均带符号差 {fmt_mean(signed)} 档"
             f"（平均绝对差 {absmean:.2f} 档）→ {direction}")
    worst = ""
    if order:
        worst = sorted(order, key=lambda d: (-st[d]["mean_abs"], -abs(st[d]["mean_signed"]), d))[0]
        w = st[worst]
        L.append(f"最容易偏的维度：{worst}（平均 {fmt_mean(w['mean_signed'])} 档，"
                 f"平均绝对差 {w['mean_abs']:.2f} 档，n={w['n']}："
                 f"偏高 {w['over']} 点 / 持平 {w['exact']} 点 / 偏低 {w['under']} 点）")
        for d in order:
            if d == worst:
                continue
            s = st[d]
            L.append(f"  {d}：平均 {fmt_mean(s['mean_signed'])} 档，"
                     f"平均绝对差 {s['mean_abs']:.2f} 档，n={s['n']}（偏高 {s['over']} / "
                     f"持平 {s['exact']} / 偏低 {s['under']}）")
    L.append("")

    L.append("三、混淆矩阵")
    L += render_matrix(points, "合计（全部维度）")
    for d in order:
        L += render_matrix([p for p in points if p.dim == d], d)

    L.append("四、分歧清单（AI 给的不等于老师给的，按 |差| 从大到小）")
    if not diffs:
        L.append("  （无：全部维度点完全一致）")
    else:
        half = sum(1 for p in diffs if abs(p.diff) == 0.5)
        L.append(f"  共 {len(diffs)} 条：差 0.5 档 {half} 条 · 差 ≥1 档 {len(diffs) - half} 条")
        for i, p in enumerate(diffs, 1):
            conf = f"，老师置信 {p.confidence}" if p.confidence else ""
            L.append(f"  {i}. [{p.case_id}] {p.dim}：AI {fmt(p.ai)} vs 老师 {fmt(p.teacher)}"
                     f"（{fmt_diff(p.diff)} 档{conf}）")
            L.append(f"     老师理由：{p.teacher_note or '（未写 notes）'}")
            if p.ai_evidence:
                for e in p.ai_evidence:
                    L.append(f"     AI 当时引的证据：{e}")
            else:
                L.append("     AI 当时引的证据：（未记录）")
    L.append("")

    L.append("五、不进统计的部分")
    unrated = [(c.case_id, *u) for c in cases for u in c.unrated]
    if not unrated:
        L.append("老师未评维度（老师给 null）：无")
    else:
        L.append(f"老师未评维度（老师给 null，本项无证据）{len(unrated)} 点：")
        for cid, dim, a_band, a_reason, t_note in unrated:
            ai_txt = f"AI 侧 {fmt(a_band)}" if a_band is not None else "AI 侧也没给"
            tail = f"；老师：{t_note}" if t_note else ""
            L.append(f"  - [{cid}] {dim}：{ai_txt}"
                     f"{f'（{a_reason}）' if a_reason else ''}{tail}")
    no_ai = [(c.case_id, *x) for c in cases for x in c.no_ai]
    if no_ai:
        L.append(f"AI 缺值（老师给了分，AI 侧没有）{len(no_ai)} 点：")
        for cid, dim, t_band, a_reason in no_ai:
            L.append(f"  - [{cid}] {dim}：老师 {fmt(t_band)}"
                     f"{f'（{a_reason}）' if a_reason else ''}")
    susp = [(c.case_id, *s) for c in cases for s in c.suspicious]
    if susp:
        L.append(f"来源存疑（评分方不是 AI，被 --ai-party ai-only 剔除）{len(susp)} 点：")
        for cid, dim, band, party in susp:
            L.append(f"  - [{cid}] {dim}：{fmt(band)}（评分方「{party}」）")
    L.append("")

    red = False
    if smoke:
        concl = (f"结论：样本不足（进入统计的 case {used_cases} 个 < {MIN_SAMPLE}）→ 仅作冒烟："
                 f"±1 档一致率 {wi}/{n}（{pct(wi, n)}，含完全一致 {ex}/{n} = {pct(ex, n)}）"
                 f"还不能用来判红；凑满 {MIN_SAMPLE} 个 case 再回来看。")
    elif n == 0:
        concl = ("结论：没有任何可比维度点（老师全给 null？）→ 这次没算出东西，"
                 "不是绿灯也不是红灯，先补 teacher.json。")
    elif rate < min_agreement:
        concl = (f"结论：±1 档一致率 {wi}/{n}（{pct(wi, n)}）< 阈值 {min_agreement:.0%} → 红灯"
                 f"（退出码 1）：先改分歧清单里 |差| ≥ 1 档 的那 {big} 条，改完再跑一次。")
        red = True
    else:
        concl = (f"结论：±1 档一致率 {wi}/{n}（{pct(wi, n)}，含完全一致 {ex}/{n} = {pct(ex, n)}）"
                 f"≥ 阈值 {min_agreement:.0%} → 绿灯；整体 {fmt_mean(signed)} 档"
                 f"（{direction}）")
        if worst:
            concl += f"；最易偏维度 {worst}（{fmt_mean(st[worst]['mean_signed'])} 档）"
        concl += f"；|差| ≥ 1 档 {big} 条。"
    L.append(concl)
    return "\n".join(L) + "\n", red


def main() -> None:
    ap = argparse.ArgumentParser(
        description="KPF 口语 AI 分 × 真人分 校准（纯标准库、离线；只读输入，"
                    "不联网、不读凭证、不碰音频与转写）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="一个 case 一个目录，目录里：\n"
               "  teacher.json   必需  老师真实给分（templates/teacher-marks.json）\n"
               "  ai-分项.json    可选  AI 四项预估（templates/ai-marks.json），优先用它\n"
               "  作业记录.md     可选  没有 ai-分项.json 时的兜底：解析「分项评分 / 四项预估」\n"
               "                        表格里的档位。认 `**2**`、`2 / 5`、`2.5 / 5`、`≈3/5`、\n"
               "                        裸 `2.5`；只有引擎原始分 `79.6 / 100` 而没有 ≈n/5 时报错\n"
               "                        ——档位换算的唯一实现在 kpf_xfyun.py，不在这里复制一份。\n"
               "半分与 null 都支持：老师给 null 的维度不进统计，但会单独列出来。\n"
               "级别 → 应有维度：KET 3 项（无话语组织）/ PET、FCE 4 项，写错判输入错误。")
    ap.add_argument("cases", nargs="*", type=Path,
                    help="case 目录（每个里面要有 teacher.json）")
    ap.add_argument("--root", type=Path, default=None,
                    help="工作区目录：扫它的一级子目录，含 teacher.json 的才算 case")
    ap.add_argument("--min-agreement", type=float, default=0.8,
                    help="±1 档一致率低于此值判红（退出码 1）。默认 0.8")
    ap.add_argument("--ai-party", choices=["any", "ai-only"], default="any",
                    help="AI 侧取值来源：any（默认，预估表里每行都算 AI 侧）／"
                         "ai-only（只认评分方写 AI 的行，其余列「来源存疑」）")
    ap.add_argument("--out", type=Path, default=None, help="把报告同时写到这个文件")
    args = ap.parse_args()

    if not (0.0 <= args.min_agreement <= 1.0):
        ap.error(f"--min-agreement 要在 0–1 之间（现在是 {args.min_agreement}）")

    try:
        dirs = collect_cases(args.cases, args.root)
        cases = [build_case(d, args.ai_party) for d in dirs]
    except InputError as e:
        fail(str(e))

    report, red = build_report(cases, args.min_agreement, args.ai_party)
    sys.stdout.write(report)
    if args.out is not None:
        args.out.write_text(report, encoding="utf-8")
        print(f"报告已写出 {args.out}", file=sys.stderr)
    sys.exit(1 if red else 0)


if __name__ == "__main__":
    main()
