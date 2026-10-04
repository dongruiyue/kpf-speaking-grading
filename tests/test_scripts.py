#!/usr/bin/env python3
"""KPF 口语批改 skill · 脚本级回归（纯标准库 · 离线 · 秒级）

`tests/run_fixtures.py` 保的是**校验器**（家长版 / 学生版 / 作业记录的输出合规）。
这个文件保的是**另外三个脚本** —— 发布闸门、互动证据提取器、批改底稿生成器。
它们原来一点自动化覆盖都没有，于是七个真错都是外部审查用合成输入才发现的
（2026-10-04 两轮）。每个用例的写法都是"**先复现那次错，再断言现在拦得住 / 改对了**"。

为什么要单独一个文件：夹具是静态文本、只喂校验器，跑不到这三个脚本的运行路径上；
而"闸门漏放音频""闸门漏看文件名""单簇也宣称能分说话人""两人模式被锁死"
"对齐吞掉上一题答案的尾词""对齐选中答案里的复述""坏 schema 抛 KeyError 而不是
退出码 2"，全都只在**真的跑一次**时才现形。

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


class _ArgsTwo(_Args):
    """`--speakers 2` = 考官 + 一位考生。"""

    speakers = 2


def cluster(cid: int, center: float, n: int = 10, sd: float = 4.0) -> dict:
    return {"cid": cid, "n": n, "center": center, "sd": sd,
            "min": center - sd * 2, "max": center + sd * 2}


def tokens_of(sentence: str) -> list[dict]:
    words = sentence.split()
    return [{"w": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "p": 1.0}
            for i, w in enumerate(words)]


def run_gate(root: Path, deny: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "check_publishable.py"),
                           "--root", str(root), "--denylist", str(deny)],
                          capture_output=True, text=True)


def first_denylist_word(deny: Path) -> str:
    """黑名单的第一条真实词 —— 拼在文件名里用来测"路径也要扫"。

    从文件里读、不写死在源码里：这份源码自己也要过发布闸门。
    """
    for line in deny.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    raise RuntimeError("示例黑名单里没有可用词条")


# --------------------------------------------------------------------------
# 用例 1：发布闸门必须拦住"路径和文件名里的学生信息 + 被跟踪的音频"
# 旧实现只在文本内容里查黑名单，文件名只查音视频扩展名。
# --------------------------------------------------------------------------

def case_publishable_gate_checks_paths() -> None:
    print("\n[1] 发布闸门：路径与文件名也要过黑名单")
    if shutil.which("git") is None:
        SKIPPED.append("发布闸门路径检查：这台机器上没有 git，跳过")
        print("  skip 没有 git")
        return
    deny = SCRIPTS / "publishable-denylist.example.txt"
    if not deny.is_file():
        SKIPPED.append("发布闸门路径检查：找不到示例黑名单，跳过")
        print("  skip 没有示例黑名单")
        return

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        (root / "README.md").write_text("# 一个干净的仓库\n", encoding="utf-8")
        clean = run_gate(root, deny)
        check("干净仓库放行（对照）", clean.returncode == 0,
              f"退出码 {clean.returncode}，stderr={clean.stderr.strip()[:200]}")

        # ① 非 UTF-8 的二进制音频，硬塞进版本库（名字拼出来：本文件自己也要过闸门）
        av_name = "clip" + ".wa" + "v"
        (root / av_name).write_bytes(b"\x00\x01\x02\xff\xfe not-utf8")
        # ② 文件名里带黑名单词，内容只有一个 {}（内容扫描查不出来）
        word = first_denylist_word(deny)
        named = f"{word}-作业.json"
        (root / named).write_text("{}\n", encoding="utf-8")

        for cmd in (["init", "-q"], ["add", "-A"],
                    ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"]):
            subprocess.run(["git", *cmd], cwd=str(root), capture_output=True, check=True)

        bad = run_gate(root, deny)
        check("被跟踪的音频判不可发布", bad.returncode == 1,
              f"退出码 {bad.returncode}（应为 1），stdout={bad.stdout.strip()[:200]}")
        check("命中行点出音视频文件", av_name in bad.stdout,
              f"stdout 里没有 {av_name}：{bad.stdout.strip()[:200]}")
        check("文件名里的黑名单词也拦得住", word in bad.stdout,
              f"stdout 里没有「{word}」：{bad.stdout.strip()[:200]}")


# --------------------------------------------------------------------------
# 用例 2：互动门槛 —— 单簇不许宣称能分；两人模式要明说"不做归属"
# --------------------------------------------------------------------------

def case_interact_gate_structure() -> None:
    print("\n[2] 互动门槛：单簇不许宣称能分，两人模式要明说不做归属")
    one = [cluster(0, 180.0)]
    g = evaluate_gates(one, [], _Args())
    check("单簇 → G1 fail", g["g1"]["status"] == "fail", f"实际 {g['g1']['status']}")
    check("单簇 → separable False", g["separable"] is False, f"实际 {g['separable']}")
    check("单簇 → 给出文字依据", bool(g["g1"]["notes"]),
          "g1.notes 是空的，教师看不出为什么不可分")
    check("单簇 → 不是「本模式不做归属」那条理由", not g.get("mode_note"),
          "单簇被误判成 --speakers 2 的模式说明")

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

    # --speakers 2：考生侧只有一簇是设计如此 —— 不是 fail，但也不许宣称能分
    g4 = evaluate_gates(one, [], _ArgsTwo())
    check("--speakers 2 → G1 untested（不是 fail，也不是 pass）",
          g4["g1"]["status"] == "untested", f"实际 {g4['g1']['status']}")
    check("--speakers 2 → separable False", g4["separable"] is False, f"实际 {g4['separable']}")
    check("--speakers 2 → 写明「这个模式不做归属」",
          bool(g4.get("mode_note")) and "不做" in g4["mode_note"], f"实际 {g4.get('mode_note')!r}")


# --------------------------------------------------------------------------
# 用例 3：题目对齐
#   3a 不许把上一题答案的尾词算进题干（首尾纯插入要剪）
#   3b 不许选中答案里对题目的复述（靠后但更相似）
# --------------------------------------------------------------------------

def case_analyze_match_boundaries() -> None:
    print("\n[3] 题目对齐：不吞上一题的尾词，也不选答案里的复述")

    # 3a：多吞了相邻一个词的窗口（0.909 也过 0.9）不能胜出
    sentence = "Why because I like music What music do you like I like jazz"
    tokens = tokens_of(sentence)
    words = sentence.split()
    targets = [norm(w) for w in "What music do you like".split()]
    found, q_end, ratio, alt = _match_question(tokens, targets, 1)
    window = words[found:q_end + 1]
    check("3a 题干窗口从 What 起", window[:1] == ["What"],
          f"实际窗口 {window}（从 music 起就是把上一题的尾词吞了）")
    check("3a 题干窗口是等长的那一段",
          window == ["What", "music", "do", "you", "like"], f"实际 {window}")
    check("3a 上一题答案保住尾词 music",
          words[1:found] == ["because", "I", "like", "music"], f"实际 {words[1:found]}")
    check("3a 相似度 1.0（剪掉纯插入后重算）", abs(ratio - 1.0) < 1e-9, f"实际 {ratio:.3f}")

    # 3b：开头把 usually 念成 normally（0.9），答案里完整复述（1.0）→ 必须取开头那处
    sentence2 = ("What do you normally do at weekends What do you usually do at weekends "
                 "I usually play football")
    tokens2 = tokens_of(sentence2)
    words2 = sentence2.split()
    targets2 = [norm(w) for w in "What do you usually do at weekends".split()]
    found2, q_end2, ratio2, alt2 = _match_question(tokens2, targets2, 0)
    check("3b 题干落在开头那处（不是答案里的复述）", found2 == 0,
          f"实际起点 {found2}（0 才是念题处，{found2} 已落进答案）")
    check("3b 题干取的是念题那处（含念错的 normally）",
          words2[found2:q_end2 + 1] == ["What", "do", "you", "normally", "do", "at", "weekends"],
          f"实际 {words2[found2:q_end2 + 1]}")
    check("3b 题干之后还有内容（没把答案整段吞掉）", q_end2 + 1 < len(words2),
          f"题干切到末尾：窗口 {words2[found2:q_end2 + 1]}")
    check("3b 真正的回答仍在后面",
          words2[-4:] == ["I", "usually", "play", "football"], f"实际尾部 {words2[-4:]}")
    check("3b 报出更靠后的那条更相似窗口（歧义提示）", alt2 is not None,
          "没有歧义提示，教师无从核对边界")
    check("3b 提示里指的是靠后的位置", alt2 is not None and alt2[0] > found2,
          f"实际 {alt2}")


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
    for case in (case_publishable_gate_checks_paths,
                 case_interact_gate_structure,
                 case_analyze_match_boundaries,
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
