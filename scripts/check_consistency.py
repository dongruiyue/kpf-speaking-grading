#!/usr/bin/env python3
"""KPF 口语批改 skill · 规则一致性校验器（纯标准库 · 离线）

断言代码与文档不漂移，有差异就 `sys.exit(1)` 并把两边的值都打出来：

  1. `to_band()` 阈值：`scripts/kpf_xfyun.py` 的 `BANDS` ↔ `references/02-rubric.md` 5.3 的阈值表
  2. 各级别口语满分：代码/模板/文档里出现的 45 / 30 / 60 ↔ `references/02-rubric.md` 的官方分制表
  3. A2 Key 维度数：报告模板与 `references/01-task-map.md` 都必须是 3 项、且不含「话语组织」
  4. 引用完整性：`SKILL.md` 与 `references/*.md` 里出现的 scripts / references / questions 路径必须真实存在
  5. 无自我覆盖残留：不得出现「取代此前 / 效力高于 / 优先于本文件 / 已作废 / 本节的效力」这类打补丁声明
  6. 节号连续：每份 references 的一级节号必须连续（`## 一、## 二、…`），断号是文档腐化信号
  7. 防编造阈值：`scripts/kpf_validate.py` 的 `QUOTE_ERROR` / `QUOTE_PASS` ↔ `references/06-verification.md` §8.4
     锚点表（并顺带扫 checklist / 04-feedback 里写明的相似度，防止旧阈值残留在文档里）
  8. 家长版技术指标分级：`TECH_PARENT_HARD` / `TECH_PARENT_COND` ↔ §8.4「停顿/语速带量化才算指标」的描述
  9. 维度中英对照：`DIM_ALIASES` ↔ §8.4 对照表，且 A2 = 3 项在 `kpf_report.py` 三套模板里都有 A2 例外说明

用法：
  check_consistency.py            # 在本 skill 目录下跑，或从任意位置跑（按脚本位置定位根目录）
退出码：0 = 全部通过；1 = 有不一致。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = ROOT / "SKILL.md"
RUBRIC = ROOT / "references" / "02-rubric.md"
TASK_MAP = ROOT / "references" / "01-task-map.md"
FEEDBACK = ROOT / "references" / "04-feedback.md"
CHECKLIST = ROOT / "references" / "checklist.md"
VERIFY = ROOT / "references" / "06-verification.md"
XFUN = ROOT / "scripts" / "kpf_xfyun.py"
REPORT = ROOT / "scripts" / "kpf_report.py"
VALIDATE = ROOT / "scripts" / "kpf_validate.py"

DIM_NAMES = ["语法与词汇", "话语组织", "发音", "互动交际"]
EXPECTED_LEVEL_DIMS = {
    "A2": ["语法与词汇", "发音", "互动交际"],
    "B1": ["语法与词汇", "话语组织", "发音", "互动交际"],
    "B2": ["语法与词汇", "话语组织", "发音", "互动交际"],
}
EXPECTED_FULL_MARK = {"A2": 45, "B1": 30, "B2": 60}
LEVEL_TOKENS = {"A2": ["A2", "KET", "Key"], "B1": ["B1", "PET", "Preliminary"],
                "B2": ["B2", "FCE", "First"]}
BANNED_SELF_OVERRIDE = ["取代此前", "效力高于", "优先于本文件", "已作废", "本节的效力"]
CN_NUMERALS = "一二三四五六七八九十"

failures: list[str] = []
checks: list[str] = []


def ok(msg: str) -> None:
    checks.append(msg)
    print(f"  ✅ {msg}")


def bad(msg: str, detail: list[str] | None = None) -> None:
    failures.append(msg)
    print(f"  ❌ {msg}")
    for line in detail or []:
        print(f"       {line}")


def read(path: Path) -> str:
    if not path.is_file():
        bad(f"文件不存在：{path}")
        return ""
    return path.read_text(encoding="utf-8")


def dims_in(text: str) -> list[str]:
    """按出现顺序抽出已知维度名（不重复）。"""
    found = []
    for name, pos in sorted(((n, text.find(n)) for n in DIM_NAMES if n in text),
                            key=lambda x: x[1]):
        if name not in found:
            found.append(name)
    return found


def py_string_list(src: str, name: str) -> list[str]:
    """从源码里抠出 `NAME = ["a", "b"]` 的字符串列表（跨行也行）。"""
    m = re.search(rf"{name}\s*=\s*\[(.*?)\]", src, re.S)
    return re.findall(r'"([^"]*)"', m.group(1)) if m else []


def py_number(src: str, name: str) -> float | None:
    m = re.search(rf"^{name}\s*=\s*([\d.]+)", src, re.M)
    return float(m.group(1)) if m else None


def py_dict_of_lists(src: str, name: str) -> dict[str, list[str]]:
    m = re.search(rf"{name}\s*=\s*\{{(.*?)\n\}}", src, re.S)
    if not m:
        return {}
    return {cn: re.findall(r'"([^"]*)"', alts)
            for cn, alts in re.findall(r'"([^"]+)"\s*:\s*\[(.*?)\]', m.group(1), re.S)}


def py_dimensions(src: str) -> dict[str, list[str]]:
    m = re.search(r"^DIMENSIONS\s*=\s*\{(.*?)\n\}", src, re.S | re.M)
    if not m:
        return {}
    return {lv: re.findall(r'"([^"]*)"', dims)
            for lv, dims in re.findall(r'"(KET|PET|FCE)"\s*:\s*\[(.*?)\]', m.group(1), re.S)}


def anchor_rows() -> dict[str, str]:
    """解析 references/06-verification.md §8.4 的数值锚点表 → {标签: 值}。"""
    text = read(VERIFY)
    m = re.search(r"###\s*8\.4(.*?)(?=\n###|\n##|\Z)", text, re.S)
    if not m:
        bad("references/06-verification.md 里找不到 8.4 节（校验器数值锚点表）")
        return {}
    rows: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().strip("*` ") for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] and not set(cells[0]) <= set("-: "):
            rows[cells[0]] = cells[1]
    if len(rows) < 3:
        bad("references/06-verification.md §8.4 的锚点表解析不出内容", [f"解析出 {rows}"])
    return rows


def anchor(rows: dict[str, str], needle: str) -> str | None:
    for label, value in rows.items():
        if needle in label:
            return value
    return None


def code_bands() -> list[tuple[int, int]]:
    src = read(XFUN)
    m = re.search(r"BANDS\s*=\s*\[(.*?)\]", src, re.S)
    if not m:
        bad("scripts/kpf_xfyun.py 里找不到 BANDS 定义")
        return []
    return [(int(a), int(b)) for a, b in re.findall(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", m.group(1))]


def rubric_band_table() -> list[tuple[str, int, int]] | None:
    """解析 02-rubric.md 5.3 的阈值表 → [(形式, 边界值, 档位)]。"""
    text = read(RUBRIC)
    m = re.search(r"###\s*5\.3(.*?)(?=\n###|\n##|\Z)", text, re.S)
    if not m:
        bad("references/02-rubric.md 里找不到 5.3 节（阈值表）")
        return None
    rows = []
    for line in m.group(1).splitlines():
        cells = [c.strip().strip("*` ") for c in line.strip().strip("|").split("|")]
        if len(cells) != 2 or not cells[1].isdigit():
            continue
        rng, band = cells[0], int(cells[1])
        mm = re.match(r"^≥\s*(\d+)$", rng)
        if mm:
            rows.append(("ge", int(mm.group(1)), band))
            continue
        mm = re.match(r"^(\d+)\s*[–\-~—]\s*(\d+)$", rng)
        if mm:
            rows.append(("ge", int(mm.group(1)), band))
            continue
        mm = re.match(r"^<\s*(\d+)$", rng)
        if mm:
            rows.append(("lt", int(mm.group(1)), band))
            continue
    if not rows:
        bad("references/02-rubric.md 5.3 的阈值表解析不出任何一行")
        return None
    return rows


def level_number_pairs(line: str, pat_num: re.Pattern[str]):
    """把一行里的「级别标记」与它**后面紧跟**的数字配对（避免"A2 Key 45 分／B1 … 30 分"互相串味）。

    产出 (级别, 数字, 该级别所在的片段)。
    """
    marks = []
    for level, toks in LEVEL_TOKENS.items():
        for tok in toks:
            start = 0
            while True:
                pos = line.find(tok, start)
                if pos == -1:
                    break
                marks.append((pos, level))
                start = pos + len(tok)
    marks.sort()
    for idx, (pos, level) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(line)
        window = line[pos:end]
        for mm in pat_num.finditer(window):
            num = int(mm.group(1) or mm.group(2))
            yield level, num, window


def check_bands() -> None:
    bands = code_bands()
    rows = rubric_band_table()
    if not bands or rows is None:
        return
    doc = []
    for kind, value, band in rows:
        doc.append((0 if kind == "lt" else value, band))
    if bands == doc:
        ok(f"to_band() 阈值一致：代码 BANDS={bands} ↔ 02-rubric 5.3={[r[:2] for r in rows]}")
        return
    bad("to_band() 阈值漂移：代码与 02-rubric.md 5.3 的阈值表不一致",
        [f"scripts/kpf_xfyun.py BANDS = {bands}",
         f"references/02-rubric.md 5.3 表 = {[(k, v, b) for k, v, b in rows]}",
         "两处必须同时改，改一处就是漂移"])


def check_full_marks() -> None:
    """各级别口语满分：02-rubric 官方分制表 ↔ 其他文件里出现的 level↔满分 配对。"""
    rubric = read(RUBRIC)
    m = re.search(r"###\s*5\.1(.*?)(?=\n###|\Z)", rubric, re.S)
    if not m:
        bad("references/02-rubric.md 里找不到 5.1 节（官方分制表）")
        return
    found = {}
    table = m.group(1)
    for line in table.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        level = next((lv for lv, toks in LEVEL_TOKENS.items()
                      if any(t in cells[0] for t in toks)), None)
        if not level:
            continue
        nums = [int(c) for c in cells if c.isdigit() and 20 <= int(c) <= 100]
        if nums:
            found[level] = nums[-1]
    if found != EXPECTED_FULL_MARK:
        bad("02-rubric.md 5.1 官方分制表与预期不符",
            [f"期望 {EXPECTED_FULL_MARK}", f"表中解析出 {found}"])
    else:
        ok(f"02-rubric 官方分制表一致：A2 Key 45 / B1 Preliminary 30 / B2 First 60")

    # 其他文件里的 level ↔ 满分 配对不得矛盾（只看"紧跟在该级别后面的那个数字"）
    files = [SKILL_MD, TASK_MAP, RUBRIC, ROOT / "references" / "03-scoring-rules.md",
             ROOT / "references" / "04-feedback.md", ROOT / "references" / "05-output-and-vault.md",
             ROOT / "references" / "06-verification.md", ROOT / "references" / "checklist.md",
             XFUN, REPORT, ROOT / "scripts" / "kpf_pronounce.py"]
    pat_num = re.compile(r"(\d{2})\s*分|/\s*(\d{2})(?![\d.])")
    mismatches = []
    for path in files:
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for level, num, clause in level_number_pairs(line, pat_num):
                if num in EXPECTED_FULL_MARK.values() and num != EXPECTED_FULL_MARK[level]:
                    mismatches.append(
                        f"{path.relative_to(ROOT)}:{i} 把 {level} 与 {num} 写在一起"
                        f"（该级别应为 {EXPECTED_FULL_MARK[level]}）：{clause.strip()[:60]}")
    if mismatches:
        bad("满分 45 / 30 / 60 在不同文件里对不上级别", mismatches)
    else:
        ok("各级别满分（45 / 30 / 60）在代码与文档里没有矛盾配对")


def check_a2_dimensions() -> None:
    task_map = read(TASK_MAP)
    # ① 01-task-map 的维度矩阵：A2 列不能给「话语组织」打勾
    matrix_ok = False
    for line in task_map.splitlines():
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        if cells[:1] == ["话语组织"] and len(cells) >= 5:
            a2 = cells[2]
            if "❌" in a2 or "无" in a2 or "没有" in a2:
                matrix_ok = True
            else:
                bad("01-task-map.md 的维度矩阵里 A2 列给「话语组织」打了勾",
                    [f"该行 A2 列写的是：{a2}",
                     "A2 Key 只有 3 个维度：语法与词汇、发音、互动交际"])
            break
    else:
        bad("01-task-map.md 里找不到「话语组织」的维度矩阵行")
    if matrix_ok:
        ok("01-task-map.md 维度矩阵：A2 列明确没有「话语组织」")

    # ② 从矩阵推出每级维度数，必须与校验器/报告口径一致
    per_level: dict[str, list[str]] = {"A2": [], "B1": [], "B2": []}
    for line in task_map.splitlines():
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] not in DIM_NAMES:
            continue
        for idx, level in zip((2, 3, 4), ("A2", "B1", "B2")):
            if "✅" in cells[idx]:
                per_level[level].append(cells[0])
    if per_level == EXPECTED_LEVEL_DIMS:
        details = "；".join(f"{lv} {len(d)} 项（{'、'.join(d)}）" for lv, d in per_level.items())
        ok(f"01-task-map.md 逐级维度数正确：{details}")
    else:
        bad("01-task-map.md 的逐级维度与预期不符",
            [f"期望 {EXPECTED_LEVEL_DIMS}", f"解析出 {per_level}"])

    # ③ 02-rubric 的级别维度表
    rubric = read(RUBRIC)
    for line in rubric.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip().strip("*") for c in line.strip().strip("|").split("|")]
        if cells[:1] and ("A2 Key" in cells[0] or "A2（KET）" in cells[0]) and len(cells) >= 2:
            dims = dims_in(cells[1])
            if dims == EXPECTED_LEVEL_DIMS["A2"]:
                ok("02-rubric.md 的 A2 维度行正确：语法与词汇、发音、互动交际（3 项，无话语组织）")
            else:
                bad("02-rubric.md 的 A2 维度行不对（必须正好 3 项、不含话语组织）",
                    [f"该行写的是：{cells[1]}"])
            break

    # ④ 报告模板（kpf_report.py）：话语组织必须带 A2 例外说明
    src = read(REPORT)
    templates = re.split(r"^TPL_", src, flags=re.M)[1:]
    names = []
    for chunk in templates:
        name = chunk.split("=", 1)[0].strip()
        if name not in ("RECORD", "PARENT", "STUDENT"):
            continue
        names.append(name)
        missing = [d for d in ("语法与词汇", "发音", "互动交际") if d not in chunk]
        if missing:
            bad(f"kpf_report.py 的 {name} 模板缺维度行：{'、'.join(missing)}")
        for i, line in enumerate(chunk.splitlines(), 1):
            if re.match(r"^\s*(?:\|\s*|#+\s*|-+\s*)?\*{0,2}话语组织", line) and \
                    not any(k in line for k in ("无此项", "删掉", "整行不写")):
                bad(f"kpf_report.py 的 {name} 模板里「话语组织」维度项没写 A2 例外说明",
                    [f"该行：{line.strip()[:80]}",
                     "A2 Key 没有话语组织，模板必须写明「A2 Key 无此项，整行删掉」"])
    if sorted(names) != ["PARENT", "RECORD", "STUDENT"]:
        bad(f"kpf_report.py 模板名与预期不符：{names}")
    else:
        ok("kpf_report.py 三个报告模板都存在，核心维度行齐备")


def check_reference_integrity() -> None:
    pat = re.compile(r"(?:scripts|references|questions)/[A-Za-z0-9_.\-]+")
    targets = [SKILL_MD] + sorted((ROOT / "references").glob("*.md"))
    missing = []
    seen = 0
    for path in targets:
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in pat.finditer(line):
                ref = m.group(0).rstrip(".,;:、）)")
                seen += 1
                if not (ROOT / ref).exists():
                    missing.append(f"{path.relative_to(ROOT)}:{i} 引用了不存在的路径 `{ref}`")
    if missing:
        bad("引用完整性：有路径指向不存在的文件", missing)
    else:
        ok(f"引用完整性：SKILL.md + references/*.md 里 {seen} 处 scripts/references/questions 路径全部存在")


def check_self_override_residue() -> None:
    hits = []
    for path in [SKILL_MD] + sorted((ROOT / "references").glob("*.md")):
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for phrase in BANNED_SELF_OVERRIDE:
                if phrase in line:
                    hits.append(f"{path.relative_to(ROOT)}:{i} 出现「{phrase}」：{line.strip()[:70]}")
    if hits:
        bad("出现「打补丁式」自我覆盖声明（作废段落必须物理删除，不得靠新章节声明效力）", hits)
    else:
        ok("无自我覆盖残留：没有「取代此前 / 效力高于 / 优先于本文件 / 已作废 / 本节的效力」")


def check_section_numbering() -> None:
    pat = re.compile(rf"^##\s*([{CN_NUMERALS}]+)、", re.M)
    problems = []
    for path in sorted((ROOT / "references").glob("*.md")):
        nums = pat.findall(path.read_text(encoding="utf-8"))
        if not nums:
            continue
        expected = [CN_NUMERALS[i] for i in range(len(nums))]
        if nums != expected:
            problems.append(f"{path.relative_to(ROOT)} 的一级节号是 {'、'.join(nums)}，"
                            f"应为 {'、'.join(expected)}（断号=文档腐化的信号）")
    if problems:
        bad("一级节号不连续", problems)
    else:
        ok("references 一级节号全部连续（## 一、## 二、…）")


def check_quote_thresholds() -> None:
    """防编造阈值：kpf_validate.py 的常量 ↔ 06-verification §8.4 锚点 + 文档里的相似度写法。"""
    src = read(VALIDATE)
    rows = anchor_rows()
    code_error = py_number(src, "QUOTE_ERROR")
    code_pass = py_number(src, "QUOTE_PASS")
    if code_error is None or code_pass is None:
        bad("scripts/kpf_validate.py 里找不到 QUOTE_ERROR / QUOTE_PASS 常量")
        return
    doc_error = anchor(rows, "QUOTE_ERROR")
    doc_pass = anchor(rows, "QUOTE_PASS")
    if doc_error is None or doc_pass is None:
        bad("references/06-verification.md §8.4 缺 QUOTE_ERROR / QUOTE_PASS 锚点行",
            [f"锚点表标签：{list(rows)}"])
        return
    problems = []
    if doc_error != f"{code_error:g}":
        problems.append(f"QUOTE_ERROR：scripts/kpf_validate.py = {code_error:g} ↔ "
                        f"06-verification §8.4 = {doc_error}")
    if doc_pass != f"{code_pass:g}":
        problems.append(f"QUOTE_PASS：scripts/kpf_validate.py = {code_pass:g} ↔ "
                        f"06-verification §8.4 = {doc_pass}")
    if not code_error < code_pass:
        problems.append(f"代码里 QUOTE_ERROR({code_error:g}) 必须小于 QUOTE_PASS({code_pass:g})")
    # 其他文件里写明的相似度阈值（形如「相似度 < 0.85」）也必须落在这两个边界上，
    # 防止旧阈值（0.72 / 0.90）残留。带比较符才算阈值说明，避免误伤"相似度 0.68"这种实测值。
    for path in (CHECKLIST, FEEDBACK, VERIFY):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(r"相似度[^0-9\n]{0,10}?[<≤≥>]\s*([01]?\.\d+)", line)
            if m and float(m.group(1)) not in (code_error, code_pass):
                problems.append(f"{path.relative_to(ROOT)}:{i} 写的相似度阈值 {m.group(1)} "
                                f"既不是 QUOTE_ERROR({code_error:g}) 也不是 QUOTE_PASS({code_pass:g})")
    if problems:
        bad("防编造阈值漂移：代码与文档写明的阈值对不上", problems)
    else:
        ok(f"防编造阈值一致：QUOTE_ERROR={code_error:g} / QUOTE_PASS={code_pass:g}"
           f"（代码 ↔ 06-verification §8.4 ↔ checklist/04-feedback）")


def check_tech_tiers() -> None:
    """家长版技术指标分级：无条件那一级 vs「停顿 / 语速」带量化才算。"""
    src = read(VALIDATE)
    rows = anchor_rows()
    hard = py_string_list(src, "TECH_PARENT_HARD")
    cond = py_string_list(src, "TECH_PARENT_COND")
    banned = py_string_list(src, "BANNED_PARENT")
    if not hard or not cond:
        bad("scripts/kpf_validate.py 里找不到 TECH_PARENT_HARD / TECH_PARENT_COND")
        return
    doc_hard = anchor(rows, "无条件")
    doc_cond = anchor(rows, "带量化")
    if doc_hard is None or doc_cond is None:
        bad("references/06-verification.md §8.4 缺「技术指标（无条件 error）/（带量化才算）」锚点行",
            [f"锚点表标签：{list(rows)}"])
        return
    split = lambda s: [x.strip() for x in s.split("、") if x.strip()]  # noqa: E731
    problems = []
    if split(doc_hard) != hard:
        problems.append(f"无条件那级：代码 {hard} ↔ 文档 {split(doc_hard)}")
    if split(doc_cond) != cond:
        problems.append(f"带量化那级：代码 {cond} ↔ 文档 {split(doc_cond)}")
    for word in ("停顿", "语速"):
        if word not in cond:
            problems.append(f"「{word}」应在 TECH_PARENT_COND（带量化才算技术指标）里")
    for word in ("数据", "样本", "测量"):
        if word not in banned:
            problems.append(f"「{word}」必须仍在 BANNED_PARENT（硬 error，不随停顿/语速放宽）")
        if word in hard or word in cond:
            problems.append(f"「{word}」不该出现在技术指标分级里（它属于禁用词）")
    if "带量化" not in (read(FEEDBACK) + read(CHECKLIST)):
        problems.append("04-feedback.md / checklist.md 里没有写明「停顿 / 语速 带量化才算技术指标」")
    doc_pat = anchor(rows, "带数字才算")
    if doc_pat is None:
        problems.append("references/06-verification.md §8.4 缺「技术指标（带数字才算）」锚点行")
    elif "个词" not in doc_pat:
        problems.append(f"锚点行「带数字才算」要写明 个词，实际是 {doc_pat}")
    if "个词" in hard:
        problems.append("「个词」还留在 TECH_PARENT_HARD —— 会把「三个普通的词」误判成技术指标，"
                        r"应改到 TECH_PARENT_PAT 的 `\d+ 个词`")
    if "TECH_PARENT_PAT" not in src:
        problems.append("scripts/kpf_validate.py 里找不到 TECH_PARENT_PAT（个词的带数字形态）")
    if problems:
        bad("家长版技术指标分级漂移：代码与文档对不上", problems)
    else:
        ok(f"家长版技术指标分级一致：无条件 {len(hard)} 个；带数字才算 ['个词']；"
           f"带量化才算 {cond}；数据/样本/测量仍在禁用词里")


def check_dim_names_bilingual() -> None:
    """维度中英对照：DIM_ALIASES ↔ §8.4 对照表；A2 = 3 项在三套报告模板里都有例外说明。"""
    src = read(VALIDATE)
    rows = anchor_rows()
    aliases = py_dict_of_lists(src, "DIM_ALIASES")
    dims = py_dimensions(src)
    if not aliases or not dims:
        bad("scripts/kpf_validate.py 里找不到 DIM_ALIASES / DIMENSIONS")
        return
    doc = anchor(rows, "维度中英对照")
    if doc is None:
        bad("references/06-verification.md §8.4 缺「维度中英对照」锚点行",
            [f"锚点表标签：{list(rows)}"])
        return
    problems = []
    for entry in [e.strip() for e in doc.split("；") if e.strip()]:
        if "=" not in entry:
            problems.append(f"对照表条目解析不出等号：{entry}")
            continue
        cn, en = (part.strip() for part in entry.split("=", 1))
        expected = aliases.get(cn)
        if expected is None:
            problems.append(f"对照表里的「{cn}」不在 DIM_ALIASES 里（现有关键字：{list(aliases)}）")
            continue
        got = [x.strip() for x in en.split("/") if x.strip()]
        if got != expected:
            problems.append(f"「{cn}」英文名：代码 {expected} ↔ 文档 {got}")
    for cn in aliases:
        if cn not in doc:
            problems.append(f"DIM_ALIASES 里的「{cn}」在 §8.4 对照表里没有对应行")

    # A2 = 3 项：代码 / 预期 / 文档锚点三处一致
    a2 = dims.get("KET", [])
    doc_a2 = anchor(rows, "A2 Key 维度数") or ""
    m = re.search(r"(\d+)", doc_a2)
    if len(a2) != 3:
        problems.append(f"代码 DIMENSIONS['KET'] 有 {len(a2)} 项，应为 3 项：{a2}")
    if a2 != EXPECTED_LEVEL_DIMS["A2"]:
        problems.append(f"代码 DIMENSIONS['KET'] = {a2} ↔ 预期 {EXPECTED_LEVEL_DIMS['A2']}")
    if not m or int(m.group(1)) != len(a2):
        problems.append(f"A2 Key 维度数：代码 {len(a2)} 项 ↔ §8.4 锚点写的是「{doc_a2}」")
    if "无话语组织" not in doc_a2:
        problems.append("§8.4 的 A2 维度数锚点没写明「无话语组织」")

    # 三套报告模板都要有 A2 例外说明（否则模板会把话语组织行带给 A2 学生）
    src_report = read(REPORT)
    for chunk in re.split(r"^TPL_", src_report, flags=re.M)[1:]:
        name = chunk.split("=", 1)[0].strip()
        if name not in ("RECORD", "PARENT", "STUDENT"):
            continue
        hit = [line for line in chunk.splitlines()
               if re.match(r"^\s*(?:\|\s*|#+\s*|-+\s*)?\*{0,2}话语组织", line)]
        if not hit:
            problems.append(f"kpf_report.py 的 {name} 模板里没有「话语组织」维度行")
        elif not all(any(k in line for k in ("无此项", "删掉", "整行不写")) for line in hit):
            problems.append(f"kpf_report.py 的 {name} 模板里「话语组织」行没写 A2 例外说明："
                            f"{hit[0].strip()[:70]}")
    if problems:
        bad("维度中英对照 / A2 例外说明漂移", problems)
    else:
        ok(f"维度中英对照一致：{len(aliases)} 组（A2 Key {len(a2)} 项、无话语组织，"
           f"kpf_report.py 三套模板都有 A2 例外说明）")


def main() -> None:
    print(f"KPF 规则一致性校验 · {ROOT}")
    print("\n[1] to_band() 阈值")
    check_bands()
    print("\n[2] 各级别口语满分")
    check_full_marks()
    print("\n[3] A2 Key 维度数")
    check_a2_dimensions()
    print("\n[4] 引用完整性")
    check_reference_integrity()
    print("\n[5] 自我覆盖残留")
    check_self_override_residue()
    print("\n[6] references 节号连续")
    check_section_numbering()
    print("\n[7] 防编造阈值（QUOTE_ERROR / QUOTE_PASS）")
    check_quote_thresholds()
    print("\n[8] 家长版技术指标分级（停顿 / 语速带量化才算指标）")
    check_tech_tiers()
    print("\n[9] 维度中英对照与 A2 例外说明")
    check_dim_names_bilingual()

    print()
    if failures:
        print(f"一致性校验未通过：{len(failures)} 项不一致 / {len(checks)} 项通过")
        sys.exit(1)
    print(f"一致性校验通过：{len(checks)} 项断言全部成立")


if __name__ == "__main__":
    main()
