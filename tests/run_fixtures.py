#!/usr/bin/env python3
"""KPF 口语批改 skill · 夹具回归跑批

按 `tests/expected.json` 逐个把夹具喂给 `scripts/kpf_validate.py`，比对**退出码 / error 数 /
warning 数**；任一不符就打印差异并 `sys.exit(1)`，全绿打一张表并 `sys.exit(0)`。

纯标准库 · 离线 · 不需要任何 API key：夹具是静态文本，校验器只用标准库，
所以这个跑批能在没网、没凭证的机器上秒级跑完（CI 上也一样）。

用法：
  run_fixtures.py                          # 跑 tests/fixtures/ + tests/expected.json
  run_fixtures.py --fixtures-dir <目录>     # 换成别的夹具目录（作者私有的真实夹具也能用同一个 runner）
  run_fixtures.py --expected <文件>         # 换一份期望表

`--fixtures-dir` 的设计目标：作者的私有夹具目录只要放一份自己的 expected.json，
就能用同一个 runner 跑，不必把真实姓名/原句搬进公开仓库。

expected.json 的字段（每个用例一项，键是夹具文件名）：
  kind / form / level   直接透传给校验器的三个必填参数
  transcript            true = 同级目录下的 `_transcript.json`；也可写成字符串路径（相对夹具目录）
  draft                 true = 追加 `--draft`（作业记录的草稿形态）
  exit / errors / warnings   期望值：0/1 退出码 + 校验器 stdout 摘要里的两个计数

退出码：0 = 全绿；1 = 有差异（或夹具文件缺失）；2 = 用法/文件错误。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
VALIDATE = ROOT / "scripts" / "kpf_validate.py"
DEFAULT_FIXTURES = HERE / "fixtures"
DEFAULT_EXPECTED = HERE / "expected.json"
DEFAULT_TRANSCRIPT = "_transcript.json"
SUMMARY_PAT = re.compile(r"(\d+)\s+error\s*/\s*(\d+)\s+warning")
REQUIRED = ("kind", "form", "level", "exit", "errors", "warnings")


def dw(text: str) -> int:
    """终端显示宽度（CJK 算 2 列），只为把表对齐。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - dw(text))


def fail(msg: str, code: int = 2) -> None:
    print(f"跑批无法执行：{msg}", file=sys.stderr)
    sys.exit(code)


def load_expected(path: Path) -> dict:
    if not path.is_file():
        fail(f"期望表不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 期望表坏了必须显式失败，不能静默跳过
        fail(f"期望表无法解析为 JSON：{path}（{exc}）")
    if not isinstance(data, dict) or not data:
        fail(f"期望表必须是「夹具文件名 → 用例」的非空对象：{path}")
    for name, case in data.items():
        if not isinstance(case, dict):
            fail(f"用例 {name} 不是对象")
        missing = [k for k in REQUIRED if k not in case]
        if missing:
            fail(f"用例 {name} 缺字段：{'、'.join(missing)}")
    return data


def transcript_arg(case: dict, fixtures_dir: Path) -> list[str]:
    value = case.get("transcript", False)
    if not value:
        return []
    name = value if isinstance(value, str) else DEFAULT_TRANSCRIPT
    path = Path(name)
    if not path.is_absolute():
        path = fixtures_dir / name
    if not path.is_file():
        fail(f"用例要的转写稿不存在：{path}")
    return ["--transcript", str(path)]


def run_case(name: str, case: dict, fixtures_dir: Path) -> dict:
    report = fixtures_dir / name
    if not report.is_file():
        return {"missing": f"夹具文件不存在：{report}"}
    cmd = [sys.executable, str(VALIDATE), str(report),
           "--kind", str(case["kind"]), "--form", str(case["form"]),
           "--level", str(case["level"])]
    cmd += transcript_arg(case, fixtures_dir)
    if case.get("draft"):
        cmd.append("--draft")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    match = SUMMARY_PAT.search(proc.stdout)
    got = {"exit": proc.returncode,
           "errors": int(match.group(1)) if match else None,
           "warnings": int(match.group(2)) if match else None,
           "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip(), "cmd": cmd}
    return got


def main() -> None:
    ap = argparse.ArgumentParser(description="KPF 夹具回归跑批（纯标准库、离线）")
    ap.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURES,
                    help=f"夹具目录（默认 {DEFAULT_FIXTURES}）")
    ap.add_argument("--expected", type=Path, default=None,
                    help=f"期望表（默认 {DEFAULT_EXPECTED}；给了 --fixtures-dir 且该目录里有 "
                         f"expected.json 时优先用那一份）")
    args = ap.parse_args()

    fixtures_dir = args.fixtures_dir.resolve()
    if not fixtures_dir.is_dir():
        fail(f"夹具目录不存在：{fixtures_dir}")
    expected_path = args.expected
    if expected_path is None:
        local = fixtures_dir / "expected.json"
        expected_path = local if (args.fixtures_dir != DEFAULT_FIXTURES and local.is_file()) \
            else DEFAULT_EXPECTED
    expected_path = expected_path.resolve()
    if not VALIDATE.is_file():
        fail(f"找不到校验器：{VALIDATE}")

    expected = load_expected(expected_path)
    print(f"夹具回归 · {len(expected)} 个用例 · 校验器 {VALIDATE.relative_to(ROOT) if VALIDATE.is_relative_to(ROOT) else VALIDATE}")
    print(f"夹具目录 {fixtures_dir}")
    print(f"期望表   {expected_path}")
    print()

    header = ("用例", "kind/form/level", "退出码", "error", "warning", "结果")
    rows: list[tuple[str, str, str, str, str, bool]] = []
    right = (False, False, True, True, True, False)
    failures: list[tuple[str, dict, dict]] = []

    for name in sorted(expected):
        case = expected[name]
        got = run_case(name, case, fixtures_dir)
        label = f"{case['kind']}/{case['form']}/{case['level']}"
        if "missing" in got:
            rows.append((name, label, "-", "-", "-", False))
            failures.append((name, case, got))
            continue
        ok = (got["exit"] == case["exit"]
              and got["errors"] == case["errors"]
              and got["warnings"] == case["warnings"])
        rows.append((name, label,
                     f"{case['exit']}→{got['exit']}" if got["exit"] != case["exit"] else str(got["exit"]),
                     f"{case['errors']}→{got['errors']}" if got["errors"] != case["errors"] else str(got["errors"]),
                     f"{case['warnings']}→{got['warnings']}" if got["warnings"] != case["warnings"] else str(got["warnings"]),
                     ok))
        if not ok:
            failures.append((name, case, got))

    widths = [dw(header[i]) for i in range(5)]
    for row in rows:
        for i in range(5):
            widths[i] = max(widths[i], dw(row[i]))

    def cell(text: str, width: int, align_right: bool) -> str:
        return (" " * max(0, width - dw(text)) + text) if align_right else pad(text, width)

    header_line = "  ".join(cell(header[i], widths[i], right[i]) for i in range(5)) + "  " + header[5]
    print(header_line)
    print("-" * dw(header_line))
    for row in rows:
        cells = "  ".join(cell(row[i], widths[i], right[i]) for i in range(5))
        print(f"{cells}  {'PASS' if row[5] else 'FAIL'}")
    print("-" * dw(header_line))

    if failures:
        sys.stdout.flush()  # 先把表吐出去，尾巴不对时人先看到表再看到差异
        print(f"\n有 {len(failures)} 个用例与期望不符（期望 → 实际）：", file=sys.stderr)
        for name, case, got in failures:
            print(f"\n  ✗ {name}", file=sys.stderr)
            if "missing" in got:
                print(f"      {got['missing']}", file=sys.stderr)
                continue
            print(f"      期望 exit={case['exit']} error={case['errors']} warning={case['warnings']}",
                  file=sys.stderr)
            print(f"      实际 exit={got['exit']} error={got['errors']} warning={got['warnings']}",
                  file=sys.stderr)
            print(f"      命令：{' '.join(str(c) for c in got['cmd'])}", file=sys.stderr)
            for stream in ("stdout", "stderr"):
                text = got[stream]
                if not text:
                    continue
                head = "\n".join("        " + line for line in text.splitlines()[:20])
                print(f"      --- {stream} ---\n{head}", file=sys.stderr)
        print(f"\n跑批失败：{len(failures)}/{len(expected)} 个用例与期望不符", file=sys.stderr)
        sys.exit(1)

    print(f"\n全绿：{len(rows)}/{len(expected)} 个用例与期望表一致（退出码 + error/warning 计数）")
    sys.exit(0)


if __name__ == "__main__":
    main()
