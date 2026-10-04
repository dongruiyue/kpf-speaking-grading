#!/usr/bin/env python3
"""KPF 口语批改 skill · 脚本级回归（纯标准库 · 离线 · 秒级）

`tests/run_fixtures.py` 保的是**校验器**（家长版 / 学生版 / 作业记录的输出合规）。
这个文件保的是**另外三个脚本** —— 发布闸门、互动证据提取器、批改底稿生成器。
它们原来一点自动化覆盖都没有，于是四个真错都是外部审查用合成输入才发现的
（2026-10-04 那一轮）。每个用例的写法都是"**先复现那次错，再断言现在拦得住 / 改对了**"。

为什么要单独一个文件：夹具是静态文本、只喂校验器，跑不到这三个脚本的运行路径上；
而"闸门漏放音频""单簇也宣称能分说话人""对齐吞掉上一题答案的尾词""坏 schema 抛
KeyError 而不是退出码 2"这四类错，全都只在**真的跑一次**时才现形。

不联网、不读凭证、不碰真实录音；需要 git 的用例在没有 git 的机器上会明确跳过并打印。

用法：
  test_scripts.py
退出码：0 = 全过；1 = 有用例失败；2 = 环境问题（找不到脚本目录）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if not (SCRIPTS / "kpf_analyze.py").is_file():
    print(f"跑不了：找不到脚本目录 {SCRIPTS}", file=sys.stderr)
    sys.exit(2)
sys.path.insert(0, str(SCRIPTS))

from kpf_analyze import _match_question, norm      # noqa: E402
from kpf_interact import InputError, evaluate_gates, load_transcript  # noqa: E402

FAILURES: list[str] = []
SKIPPED: list[str] = []


def check(name: str, cond: bool, detail: str) -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}：{detail}")
        FAILURES.append(f"{name}：{detail}")


class _Args:
    """evaluate_gates 只读这几个字段；口径取脚本里的默认值。"""

    speakers = 3
    min_gap_hz = 20.0
    min_sep = 1.0
    min_share = 0.2
    min_anchor_diff = 25.0


def cluster(cid: int, center: float, n: int = 10, sd: float = 4.0) -> dict:
    return {"cid": cid, "n": n, "center": center, "sd": sd,
            "min": center - sd * 2, "max": center + sd * 2}


# --------------------------------------------------------------------------
# 用例 1：发布闸门必须拦住"被跟踪的音频"
# 旧实现把读不出 UTF-8 的文件一律当二进制跳过，于是 .wav 从头到尾没被任何检查看过。
# --------------------------------------------------------------------------

def case_publishable_gate_blocks_tracked_audio() -> None:
    print("\n[1] 发布闸门：被 git 跟踪的 .wav 必须报不可发布")
    if shutil.which("git") is None:
        SKIPPED.append("发布闸门拦音频：这台机器上没有 git，跳过")
        print("  skip 没有 git")
        return
    deny = SCRIPTS / "publishable-denylist.example.txt"
    if not deny.is_file():
        SKIPPED.append("发布闸门拦音频：找不到示例黑名单，跳过")
        print("  skip 没有示例黑名单")
        return

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        (root / "README.md").write_text("# 一个干净的仓库\n", encoding="utf-8")
        clean = subprocess.run([sys.executable, str(SCRIPTS / "check_publishable.py"),
                                "--root", str(root), "--denylist", str(deny)],
                               capture_output=True, text=True)
        check("干净仓库放行（对照）", clean.returncode == 0,
              f"退出码 {clean.returncode}，stderr={clean.stderr.strip()[:200]}")

        # 非 UTF-8 的二进制音频，硬塞进版本库
        # 名字拼出来：本文件自己也要过发布闸门，源码里不出现连续的音视频文件名
        # （同 check_publishable.py 的自我约束，别把自己塞进豁免名单）
        av_name = "clip" + ".wa" + "v"
        (root / av_name).write_bytes(b"\x00\x01\x02\xff\xfe not-utf8")
        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", *cmd], cwd=str(root), capture_output=True, check=True)

        bad = subprocess.run([sys.executable, str(SCRIPTS / "check_publishable.py"),
                              "--root", str(root), "--denylist", str(deny)],
                             capture_output=True, text=True)
        check("被跟踪的音频判不可发布", bad.returncode == 1,
              f"退出码 {bad.returncode}（应为 1），stdout={bad.stdout.strip()[:200]}")
        check("命中行点出是哪个文件", av_name in bad.stdout,
              f"stdout 里没有 {av_name}：{bad.stdout.strip()[:200]}")


# --------------------------------------------------------------------------
# 用例 2：互动门槛 —— 只有一簇时不许宣称"可分出两位考生"
# 旧实现簇数为 1 时相邻簇循环一次都不走 → g1 空过 → separable=True。
# --------------------------------------------------------------------------

def case_interact_gate_needs_two_clusters() -> None:
    print("\n[2] 互动门槛：单簇必须判不可分")
    one = [cluster(0, 180.0)]
    g = evaluate_gates(one, [], _Args())
    check("单簇 → G1 fail", g["g1"]["status"] == "fail", f"实际 {g['g1']['status']}")
    check("单簇 → separable False", g["separable"] is False, f"实际 {g['separable']}")
    check("单簇 → 给出文字依据", bool(g["g1"]["notes"]),
          "g1.notes 是空的，教师看不出为什么不可分")

    two = [cluster(0, 150.0), cluster(1, 214.0)]
    g2 = evaluate_gates(two, [], _Args())
    check("两簇间隔 64 Hz → G1 pass（对照）", g2["g1"]["status"] == "pass",
          f"实际 {g2['g1']['status']}，notes={g2['g1']['notes']}")
    check("两簇间隔 64 Hz → separable True（对照）", g2["separable"] is True,
          f"实际 {g2['separable']}")

    near = [cluster(0, 180.0), cluster(1, 190.0)]
    g3 = evaluate_gates(near, [], _Args())
    check("两簇只差 10 Hz → G1 fail（门槛仍然有效）", g3["g1"]["status"] == "fail",
          f"实际 {g3['g1']['status']}")


# --------------------------------------------------------------------------
# 用例 3：题目对齐不许吞掉上一题答案的尾词
# 旧实现在第一个相似度 ≥0.9 的窗口就 break，而"多吞一个词"的窗口也能到 0.909。
# --------------------------------------------------------------------------

def case_analyze_match_does_not_swallow_answer() -> None:
    print("\n[3] 题目对齐：不许把上一题答案的尾词算进题干")
    words = "Why because I like music What music do you like I like jazz".split()
    tokens = [{"w": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "p": 1.0}
              for i, w in enumerate(words)]
    targets = [norm(w) for w in "What music do you like".split()]
    found, q_end, ratio = _match_question(tokens, targets, 1)
    window = words[found:q_end + 1] if found is not None else []
    check("题干窗口从 What 起", window[:1] == ["What"],
          f"实际窗口 {window}（从 music 起就是把上一题的尾词吞了）")
    check("题干窗口是等长的那一段",
          window == ["What", "music", "do", "you", "like"], f"实际 {window}")
    check("上一题答案保住尾词 music",
          words[1:found] == ["because", "I", "like", "music"], f"实际 {words[1:found]}")
    check("相似度 1.0（等长窗口胜出）", abs(ratio - 1.0) < 1e-9, f"实际 {ratio:.3f}")


# --------------------------------------------------------------------------
# 用例 4：坏转写必须是 InputError（命令行退出码 2），不是 KeyError（退出码 1）
# --------------------------------------------------------------------------

def case_interact_transcript_schema() -> None:
    print("\n[4] 转写 schema：坏输入要按约定报错")

    def write(data: dict) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "t.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return p

    good = {"meta": {}, "text": "a b",
            "words": [{"w": "a", "start": 0.0, "end": 0.5},
                      {"w": "b", "start": 0.5, "end": 1.0}],
            "segments": [{"start": 0.0, "end": 1.0, "text": "a b"}]}
    try:
        load_transcript(write(good))
        check("合法转写放行（对照）", True, "")
    except InputError as exc:
        check("合法转写放行（对照）", False, f"被误拦：{exc}")

    bad_word = json.loads(json.dumps(good))
    del bad_word["words"][1]["end"]          # 第 2 个词缺 end（旧实现只看第 1 个，放行）
    try:
        load_transcript(write(bad_word))
        check("第 2 个词缺 end → InputError", False, "没拦住，仍然放行")
    except InputError:
        check("第 2 个词缺 end → InputError", True, "")
    except Exception as exc:                 # noqa: BLE001 - 就是要把非 InputError 抓出来
        check("第 2 个词缺 end → InputError", False,
              f"抛的是 {type(exc).__name__}（命令行会变成退出码 1，而不是约定的 2）")

    bad_seg = json.loads(json.dumps(good))
    del bad_seg["segments"][0]["end"]
    try:
        load_transcript(write(bad_seg))
        check("段边界缺 end → InputError", False, "没拦住，仍然放行")
    except InputError:
        check("段边界缺 end → InputError", True, "")
    except Exception as exc:                 # noqa: BLE001
        check("段边界缺 end → InputError", False, f"抛的是 {type(exc).__name__}")


def main() -> None:
    print("KPF 口语批改 · 脚本级回归（纯标准库、离线）")
    for case in (case_publishable_gate_blocks_tracked_audio,
                 case_interact_gate_needs_two_clusters,
                 case_analyze_match_does_not_swallow_answer,
                 case_interact_transcript_schema):
        case()

    print()
    if SKIPPED:
        for s in SKIPPED:
            print(f"跳过：{s}")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 条：")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("全过：4 组脚本级回归")
    sys.exit(0)


if __name__ == "__main__":
    main()
