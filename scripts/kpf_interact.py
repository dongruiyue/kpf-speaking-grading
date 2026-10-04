#!/usr/bin/env python3
"""KPF 口语作业 · 「有对手方」录音的互动证据（提示 / 沉默 / 话轮结构）

为什么要有这个脚本
------------------
`references/02-rubric.md` 的「互动交际」在 A2 只有两条判据：**能不能维持简单交流、
需要多少提示与支持**。这两条在纯转写里看不见——转写只有文字，没有"考官提示了几次、
中间沉默了多久"。少了这层证据，A2 互动交际只能标「待补」。

本脚本把校准目录里**已经人工做过一遍**的那套归属方法（话轮切分 / 考官话术正则 /
基频双估计 / 覆盖率，见私有目录 `_calibration/*/README-方法.md`）工程化成一个通用脚本，
让每次批改都能直接产出这批互动证据。**只做证据测量，不做任何评分判断。**

量什么
------
A 组（不需要说话人归属，永远都能量）
  1. 全片时长、词数、**话轮单元数**
  2. **沉默**：≥1.0 s 的次数 / 最长 / 总秒数与占比 + 最长的 5 段（含前后话轮归属）
  3. **话轮交接间隙**：一位说话人结束到下一位开始的间隔（有归属时按"谁→谁"分组）
B 组（需要说话人归属；分不出来时必须明说，不许硬凑）
  4. 每位说话人的话轮数 / 总词数 / 平均与最长话轮词数
  5. **考官对每位考生的提示次数**：紧随该考生话轮之后、且很短（默认 ≤8 词）的外来话轮条数；
     其中命中**高精度考官话术**的单独列出（这类是"重问/追加提问"，是最硬的"需要支持"证据）
  6. 每位考生自己的**思考型停顿**（话轮内部 ≥1.0 s 的停顿次数与最长值）

归属怎么做（沿用校准目录那套，不另发明）
--------------------------------------
  - 逐话轮估 f0：40 ms 窗 / 10 ms 跳，**倒谱 + 自相关双估计，两者相差 <12% 才采信**，取话轮内中位数。
    为什么要双估计：单用某一种估计会系统性折半/倍频（校准记录里 HPS 曾把 235 Hz 的考官读成 121 Hz）。
  - 把话轮聚成 --speakers 簇（考生侧 = --speakers − 1 簇），1-D 最优 k-means，
    用**动态规划求全局最优**（不用随机初始化 → 同输入必然同输出）。
  - **高精度考官话术正则优先于声学判断**：命中即判考官。为什么优先：校准记录里
    有一处考官问句在声学上完全不像那位考官（实测 152.8 Hz、质心 839 Hz，比男声还低沉），
    只信声学一定会判错。
  - 两位考生的**自我介绍**是零推断的归属锚点（"My name is X" / 姓名问句的答句）。
    它有两个用途：标定两条归属带；以及做硬门槛——两个锚点若落在同一簇，
    说明声学上分不开两位考生。
  - **分离质量差就不硬分**：见下面门槛。不达标只报 A 组，并写明"考官提示次数本次未测"。
  - **`--speakers 2`（考官 + 一位考生）不做说话人归属、只出 A 组指标**：本工具只实现了
    「区分两位考生」这一条归属路径（G2 要两个自我介绍锚点），考生侧在 `--speakers 3` 下
    才切成两簇。这个模式下 G1 记「untested」并写明原因，不会伪装成"可分出两位考生"。

可靠性门槛（两把都要过；数值都写进报告，便于复核）
------------------------------------------------
  G1 聚类门槛：每个相邻簇心间隙 ≥ MIN_GAP_HZ，且 间隙 /(σ1+σ2) ≥ MIN_SEP，
     且每个簇占有效话轮 ≥ MIN_SHARE（防"切出去一个孤立点就算分层"）
  G2 锚点门槛：两位考生的自我介绍锚点各自落在不同簇，且两者 f0 中位差 ≥ MIN_ANCHOR_DIFF_HZ
     没找到锚点时 G2 记「未测」（报告里降级声明，只靠 G1）

用法
----
  kpf_interact.py <转写.json> [--media <音视频>] [--speakers 3] [--silence 1.0]
                  [--out 报告.md] [--json 机器可读.json]
  退出码：0 = 正常出报告；2 = 输入不合法（文件不存在 / schema 不对 / 参数越界）。
  输入不合法时错误信息明确打出来并退出，**不会静默降级成一份"看起来正常"的报告**。
  `--media` 缺失时跳过 f0 相关部分、只出 A 组指标（报告里写明原因）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

# 时间戳沿用 kpf_analyze 的唯一实现（与 kpf_pronounce.py 同法）：教师靠 mm:ss.s 直接跳位，
# 两处实现不同就会出现 00:60.0 这类无效时间戳。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kpf_analyze import mmss  # noqa: E402

try:  # 运行期依赖：只有 --media 那条路要用。缺了仍能出 A 组，但必须说明原因。
    import av
    import numpy as np
except ImportError as _IMPORT_ERR:  # pragma: no cover
    av = None
    np = None

# ======================================================================
# 口径常量：默认值，全部可被命令行覆盖，且会被原样写进报告（口径固定 = 可复核）
# ======================================================================

SR = 16000               # 基频分析采样率（16 kHz 足够覆盖 70–400 Hz 的基频与少量谐波）
FRAME_S = 0.040          # 40 ms 窗：至少含 2–3 个基频周期，短窗才能跟上声调变化
HOP_S = 0.010            # 10 ms 跳：比窗小得多，保证一个话轮内有足够多的帧做中位数
F0_MIN, F0_MAX = 70.0, 400.0   # 人声基频搜索范围（70 覆盖成年男低音，400 覆盖儿童女高音）
E_MIN = 0.02             # 帧能量门限（RMS）：静音帧不估 f0，否则噪声也会给出一个"基频"
DUAL_AGREE = 0.12        # 双估计一致门限：相差超过 12% 视为不可靠，丢弃该帧

TURN_GAP = 0.52          # 话轮切分：词间静音 ≥ 此值即断开
TURN_SEG_GAP = 0.10      # 话轮切分：whisper 段边界处 ≥ 此值即断开（换人常常连得紧）
SILENCE_MIN = 1.0        # 沉默阈值；思考型停顿用同一个阈值
PROMPT_WORDS = 8         # "提示"上限词数：更长的外来话轮算正常提问/发言，不算提示

MIN_VOICED = 8           # 一个话轮至少这么多帧估出 f0（≈80 ms）才参与聚类
MIN_GAP_HZ = 20.0        # G1：相邻簇心最小间隙
MIN_SEP = 1.0            # G1：间隙 /(σ1+σ2)，≥1 表示两簇至少相隔各自 0.5 个标准差
MIN_SHARE = 0.15         # G1：最小簇占比（防"把孤立点切成簇"骗过间隙门槛）
GUARD_HZ = 12.0          # 归属带从锚点向内缩的半宽（沿用校准目录 ±12 Hz 的做法）
MIN_ANCHOR_DIFF_HZ = 25.0  # G2：两位考生自我介绍锚点的 f0 中位差门槛

L_EXAM = "考官"
L_UNKNOWN = "未归属"

# 高精度"只可能是考官说"的句型（沿用校准 README 第 3 节那一组）。
# 为什么叫"高精度"：这些句子要么是考试流程用语，要么只有考官会对考生说，
# 考生几乎不可能自己讲出来；命中即判考官，优先于任何声学判断。
STRONG_CUES = [
    r"this is my colleague",          # 考官介绍同事（考生不会说）
    r"can i have your mark sheets?",  # 收分卡
    r"your names are",                # 点名
    r"say why or why not",            # Part 2 指令
    r"in this part of the test",      # 换部分
    r"that is the end of the test",   # 收尾
    r"\bhere (are|is) (your|some)",   # 发图/发材料
    r"i'?d like you to",              # 布置任务
    r"please tell me (something|what)",
    r"what'?s your name",             # Part 1 逐个问姓名
    r"what is your name",
]
STRONG_RE = re.compile("|".join(STRONG_CUES))


def name_address_re(tokens: list[str]):
    """考官点名话术：用**录音里学到的考生姓名**动态生成正则（不算高精度清单里的一份子？算）。

    为什么必须动态：校准 README 那组话术里原本写死了考生姓名（"^now, luca" / "and federica,"），
    但仓库不许出现具体姓名 → 改成用自我介绍锚点学到的名字现算，等价但通用。
    为什么算"高精度"：句子开头或连接词后面紧跟你名字，几乎只有考官（或同伴点名）会这么说。
    已知盲点：考生互相点名时（"And you, Federica?"）也会命中 → 会被判成考官。
    """
    alts = sorted({re.escape(t) for t in tokens if t and len(t) >= 3}, key=len, reverse=True)
    if not alts:
        return None
    names = "|".join(alts)
    return re.compile(r"(?:" + names + r")\b\s*(?:[,?!]|\s+(?:what|which|where|when|who|why|"
                      r"how|do|does|did|are|is|can|could|would|will|tell|say)\b)")

# 姓名问句（用来定位"回答姓名"的位置 → 考生自我介绍锚点）。
# 只取高精度子集：泛泛的 "what's your X" 会把 "what's your favourite room" 也吃进来。
NAME_Q_RE = re.compile(r"what'?s your name|what is your name|your names are")

# 候选人自报姓名："My name is X" / "I'm X"。X 必须是字母开头的词，
# 且不能是常见后续词（"I'm from Holland" 的 from、"I'm 13" 的数字都不是名字）。
SELF_ID_RE = re.compile(r"\b(?:my name(?:'s| is)|i(?:'m| am))\s+([a-z][a-z'\-]{1,})")
SELF_ID_STOP = {
    "from", "not", "going", "here", "sorry", "fine", "ok", "okay", "sure", "afraid",
    "still", "just", "so", "very", "in", "at", "on", "a", "an", "the", "also", "really",
    "only", "good", "well", "back", "out", "up", "down", "now", "then", "too", "no", "yes",
}

# 弱话术：只做**诊断**用（统计"疑似考官残留"），不参与归属。
# 为什么分开：这组里有 thank you / do you think / what about you 这类宽泛句型，
# 校准记录里它们在别的视频上吃过考生的正常发言（PET 实测）。归属只信高精度那组。
WEAK_CUES = [
    r"thank you", r"thanks",
    r"^hello\b", r"^hi\b", r"^good (morning|afternoon|evening)\b",
    r"mark ?sheets?", r"please tell me", r"tell me something about",
    r"would you like to", r"let'?s (talk about|start)", r"now i'?d like",
    r"do you agree", r"would you agree", r"what about you", r"how about you",
    r"^\s*and you[,?]",
    r"what'?s your\b", r"where are you from", r"how old are you",
    r"can i (have|take)", r"could? you tell",
    r"that'?s the end", r"is the end of the test", r"some people say",
    r"first (of all|you have)", r"some time to look", r"talk to each other",
    r"talk together", r"about (two minutes|a minute)", r"work in pairs",
    r"(^|[.?!]\s*)do you (think|find|prefer|like)\b",
    r"which (is more fun|of these)", r"would you prefer", r"tell us about",
    r"we'?d like to know", r"what do you think", r"why do you think",
    r"it'?s your turn", r"^\s*(all right|alright|right)\b", r"how often do you",
]
WEAK_RE = re.compile("|".join(WEAK_CUES))


class InputError(Exception):
    """输入不合法（文件缺失 / schema 不对 / 参数越界）。→ 退出码 2，不产出报告。"""


# ======================================================================
# 基础工具
# ======================================================================

def norm_text(s: str) -> str:
    """小写 + 统一弯引号 + 压空白：所有正则都在这个形态上匹配。"""
    return re.sub(r"\s+", " ", str(s).lower().replace("’", "'"))


def disp(s: str) -> str:
    """展示用：全角转半角 + 去掉标点前的多余空格。"""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"\s+([,.?!;:])", r"\1", s)


def cell(s: str) -> str:
    """markdown 单元格里放转写原文时要把竖线转义，否则表格会被撑断。"""
    return str(s).replace("|", "\\|").replace("\n", " ")


def f1(x: float | None) -> str:
    """固定一位小数（None 显示为 –）。报告与 JSON 都走它，保证两次运行逐字节一致。"""
    return "–" if x is None else f"{x:.1f}"


def cand_label(k: int) -> str:
    """考生匿名标签：考生A / 考生B …（不写真实姓名，仓库与报告都用代号）。"""
    return f"考生{chr(ord('A') + k)}" if 0 <= k < 26 else f"考生{k}"


# ======================================================================
# 1. 读转写 + 切话轮单元
# ======================================================================

def load_transcript(path: Path) -> dict:
    """读 kpf_asr.py 产出的统一 schema：{meta, words[{w,start,end,p}], segments[], text}。"""
    if not path.is_file():
        raise InputError(f"转写文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"转写文件读不了或不是 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict) or not isinstance(data.get("words"), list):
        raise InputError("转写 schema 不对：缺 words[]（需要 kpf_asr.py 产出的词级时间戳）")
    if not data["words"]:
        raise InputError("转写里一个词都没有（words[] 为空）")
    # **逐个查，不能只看第一个**：旧实现写的是 `data["words"][:1]`，于是第 2 个词缺 `end`
    # 时这里放行，后面在 build_turns 里抛 KeyError、退出码 1 —— 而本脚本的约定是
    # "输入不合法 → 退出码 2"（外审 2026-10-04 复现）。坏 schema 必须在这一步拦住。
    for i, w in enumerate(data["words"]):
        if not isinstance(w, dict) or not {"w", "start", "end"} <= set(w):
            raise InputError(f"words[{i}] 必须含 w/start/end 三个字段（实际："
                             f"{sorted(w) if isinstance(w, dict) else type(w).__name__}）")
        if not all(isinstance(w[k], (int, float)) and not isinstance(w[k], bool)
                   for k in ("start", "end")):
            raise InputError(f"words[{i}] 的 start/end 必须是数字（实际："
                             f"start={w['start']!r}, end={w['end']!r}）")
    if not isinstance(data.get("segments"), list) or not data["segments"]:
        # segments 只用于话轮切分的第二个规则；缺了会让换人处切不开，属于口径问题，必须报错
        raise InputError("转写 schema 不对：缺 segments[]（话轮切分第二规则要用它的段边界）")
    for i, s in enumerate(data["segments"]):
        if not isinstance(s, dict) or not {"start", "end"} <= set(s):
            raise InputError(f"segments[{i}] 必须含 start/end 两个字段（实际："
                             f"{sorted(s) if isinstance(s, dict) else type(s).__name__}）")
        if not all(isinstance(s[k], (int, float)) and not isinstance(s[k], bool)
                   for k in ("start", "end")):
            raise InputError(f"segments[{i}] 的 start/end 必须是数字（实际："
                             f"start={s['start']!r}, end={s['end']!r}）")
    return data


def build_turns(words: list[dict], segments: list[dict],
                gap_t: float = TURN_GAP, seg_gap_t: float = TURN_SEG_GAP) -> list[dict]:
    """切话轮单元：词间静音 ≥ gap_t，或 whisper 段边界处静音 ≥ seg_gap_t。

    为什么要两条规则：whisper 经常把"考官问 + 考生答"粘进同一段、且中间几乎不留白，
    只在段边界上有 0.1–0.5 s 的缝；只按 0.52 s 切会把两个人的话合成一个话轮单元。
    """
    bounds = [(s["start"], s["end"]) for s in segments]

    def seg_id(t: float) -> int:
        for k, (a, b) in enumerate(bounds):
            if a - 0.05 <= t <= b + 0.05:
                return k
        for k, (a, _) in enumerate(bounds):
            if t < a:
                return k
        return len(bounds) - 1

    spans, cur = [], []
    for i, w in enumerate(words):
        if cur:
            gap = w["start"] - words[cur[-1]]["end"]
            if gap >= gap_t or (seg_id(w["start"]) != seg_id(words[cur[-1]]["end"])
                                and gap >= seg_gap_t):
                spans.append((cur[0], cur[-1]))
                cur = []
        cur.append(i)
    if cur:
        spans.append((cur[0], cur[-1]))

    turns = []
    for k, (i0, i1) in enumerate(spans):
        ws = words[i0:i1 + 1]
        turns.append({
            "i": k, "i0": i0, "i1": i1,
            "start": round(float(ws[0]["start"]), 3),
            "end": round(float(ws[-1]["end"]), 3),
            "n_words": len(ws),
            "text": disp(" ".join(str(w["w"]) for w in ws)),
        })
    return turns


def recording_length(meta: dict, media_len: float | None, words: list[dict]) -> tuple[float, str]:
    """全片时长：优先转写 meta.duration → 媒体实际时长 → 末词结束时间。返回 (秒, 来源)。"""
    for key in ("duration", "dur"):
        val = meta.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val), f"转写 meta.{key}"
    if media_len:
        return float(media_len), "媒体文件音轨"
    return float(words[-1]["end"]), "末词结束时间（没有 meta.duration / --media）"


# ======================================================================
# 2. 沉默 + 话轮交接间隙（A 组）
# ======================================================================

def find_silences(words: list[dict], threshold: float) -> list[dict]:
    """词间空白 ≥ threshold 的段落。起点 = 前词结束，终点 = 后词开始。

    口径提醒：这里的"沉默"= ASR 词间空白，**不是能量 VAD**。whisper 没转写出来的
    嘟囔、噪声、翻页声都会被算成沉默，所以它是"沉默的上限"，报告里要写明。
    """
    out = []
    for k, (prev, nxt) in enumerate(zip(words, words[1:])):
        dur = float(nxt["start"]) - float(prev["end"])
        if dur >= threshold:
            out.append({"start": round(float(prev["end"]), 3),
                        "end": round(float(nxt["start"]), 3),
                        "dur": round(dur, 3),
                        "i_before": k, "i_after": k + 1})
    return out


def edge_silences(words: list[dict], threshold: float,
                  length: float) -> list[dict]:
    """片头/片尾静音（有意义的证据：开头拖多久才出声）。只报不参与"沉默总秒数占比"。"""
    out = []
    head = float(words[0]["start"])
    if head >= threshold:
        out.append({"start": 0.0, "end": round(head, 3), "dur": round(head, 3), "edge": "片头"})
    tail = length - float(words[-1]["end"])
    if tail >= threshold:
        out.append({"start": round(float(words[-1]["end"]), 3), "end": round(length, 3),
                    "dur": round(tail, 3), "edge": "片尾"})
    return out


def handover_gaps(turns: list[dict]) -> list[dict]:
    """话轮交接间隙：上一话轮结束 → 下一话轮开始。

    它和"沉默"用的是同一批数字（交接间隙 <0.52 s 的话轮边界由段边界规则切出），
    但视角不同：沉默看"停了多久"，交接看看"谁接谁、接得多快"。
    """
    out = []
    for a, b in zip(turns, turns[1:]):
        out.append({"i_from": a["i"], "i_to": b["i"],
                    "start": a["end"], "end": b["start"],
                    "dur": round(b["start"] - a["end"], 3)})
    return out


def word_gaps(words: list[dict], i0: int, i1: int) -> list[float]:
    """话轮内部（词 i0..i1）的逐词间隔：思考型停顿就藏在这里。"""
    return [round(float(words[k + 1]["start"]) - float(words[k]["end"]), 3)
            for k in range(i0, i1)]


def word_to_turn(turns: list[dict], n_words: int) -> list[int]:
    """词序号 → 所属话轮序号（沉默表要写"前后话轮属于谁"）。"""
    owner = [0] * n_words
    for t in turns:
        for k in range(t["i0"], t["i1"] + 1):
            owner[k] = t["i"]
    return owner


# ======================================================================
# 3. 基频（f0）：倒谱 + 自相关双估计
# ======================================================================

def _need_audio() -> None:
    if np is None or av is None:
        raise InputError(f"没装音频依赖（numpy / av）：{_IMPORT_ERR}；"
                         "装法见 scripts/requirements.txt")


def decode_mono16k(path: Path) -> "np.ndarray":
    """解码任意音/视频为 16 kHz 单声道 float32（av 直接吃 mp4/m4a/mp3/wav）。"""
    _need_audio()
    if not path.is_file():
        raise InputError(f"媒体文件不存在：{path}")
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise InputError(f"这个媒体没有音轨：{path}")
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
            chunks = []
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    arr = out.to_ndarray()
                    chunks.append((arr[0] if arr.ndim == 2 else arr).astype(np.float32) / 32768.0)
            for out in resampler.resample(None):     # 冲刷重采样器缓冲，否则尾巴会缺
                arr = out.to_ndarray()
                chunks.append((arr[0] if arr.ndim == 2 else arr).astype(np.float32) / 32768.0)
    except InputError:
        raise
    except Exception as exc:                          # av 对坏文件抛的是各种底层异常
        raise InputError(f"解码失败：{path}（{exc}）") from exc
    if not chunks:
        raise InputError(f"解码得到空音频：{path}")
    return np.concatenate(chunks)


def _cep_f0(x: "np.ndarray") -> float:
    """倒谱法基频：对 log 幅度谱做逆 FFT，取基频对应的 quefrency 峰。"""
    if float(np.sqrt((x ** 2).mean())) < E_MIN:
        return float("nan")
    xw = (x - x.mean()) * np.hanning(len(x))
    nfft = 1 << int(np.ceil(np.log2(len(x) * 4)))
    cep = np.fft.irfft(np.log(np.abs(np.fft.rfft(xw, nfft)) + 1e-12))
    lo, hi = int(SR / F0_MAX), int(SR / F0_MIN)
    if hi >= len(cep):
        return float("nan")
    k = int(np.argmax(cep[lo:hi])) + lo
    return SR / k if k else float("nan")


def _ac_f0(x: "np.ndarray") -> float:
    """自相关法基频：取相关值 ≥0.92×峰值的那批 lag 里**最长**的（抑制倍频）。"""
    if float(np.sqrt((x ** 2).mean())) < E_MIN:
        return float("nan")
    n = len(x)
    xw = (x - x.mean()) * np.hanning(n)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    sp = np.fft.rfft(xw, nfft)
    corr = np.fft.irfft(sp * np.conj(sp), nfft)[:n]
    if corr[0] <= 0:
        return float("nan")
    lo, hi = int(SR / F0_MAX), min(int(SR / F0_MIN), n - 1)
    seg = corr[lo:hi]
    ks = [k for k in range(len(seg)) if seg[k] >= 0.92 * seg.max()]
    return SR / (lo + ks[-1]) if ks else float("nan")


def dual_f0(x: "np.ndarray") -> float:
    """倒谱 + 自相关一致才采信（差 <12%），返回两者均值；否则 NaN。

    为什么必须双估计：单用谐波积谱/自相关会在某些嗓音上系统性折半或倍频
    （校准记录：HPS 把 235 Hz 的考官读成 121 Hz，进而把考官问句判成男考生）。
    """
    a, b = _cep_f0(x), _ac_f0(x)
    if a != a or b != b:
        return float("nan")
    if abs(a - b) / min(a, b) > DUAL_AGREE:
        return float("nan")
    return 0.5 * (a + b)


def span_f0(audio: "np.ndarray", t0: float, t1: float) -> dict:
    """一段音频的基频统计：中位数（主输出）、帧数、逐帧值。

    帧网格**锚定在文件起点**（第 k 帧 = 采样 [k·HOP, k·HOP+FRAME)），不锚定在片段起点：
    校准目录那套人工结果就是用文件网格算的，锚在片段起点会让同一句话的中位数差几个 Hz
    （片段起点不是 HOP 的整数倍时，窗会整体错开一格），两边数字就对不上了。
    """
    _need_audio()
    frame = int(FRAME_S * SR)
    hop = int(HOP_S * SR)
    i0 = max(0, int(t0 * SR))
    i1 = min(len(audio), int(t1 * SR))
    k0 = i0 // hop
    k1 = i1 // hop
    vals = []
    n_frames = 0
    for k in range(k0, k1 + 1):
        s = k * hop
        if s + frame > len(audio):
            break
        n_frames += 1
        v = dual_f0(audio[s:s + frame])
        if v == v:
            vals.append(float(v))
    return {"median": (float(np.median(vals)) if vals else None),
            "n_voiced": len(vals), "n_frames": n_frames, "vals": vals}


# ======================================================================
# 4. 1-D 最优 k-means（动态规划，确定性）
# ======================================================================

def optimal_kmeans_1d(values: list[float], k: int, min_size: int) -> tuple[list[float], list[tuple[int, int]]]:
    """把一维数值切成 k 段使段内平方和最小，且每段至少 min_size 个点。

    为什么用动态规划而不是迭代 k-means：① 一维有全局最优解，DP 能拿到；
    ② 迭代法依赖随机初始化，同一份输入两次跑可能给出不同簇 → 报告就不确定了。
    min_size 的作用：防止"把一个孤立的高音点切出去"这种伪分层骗过间隙门槛。
    返回 (升序数值, [(起, 止) 半开区间…])；无解时返回 (数值, [])。
    """
    v = sorted(float(x) for x in values)
    n = len(v)
    if k < 1 or n < k * min_size:
        return v, []
    pre = [0.0]
    pre2 = [0.0]
    for x in v:
        pre.append(pre[-1] + x)
        pre2.append(pre2[-1] + x * x)

    def cost(i: int, j: int) -> float:            # 段 [i, j) 的段内平方和
        m = j - i
        s = pre[j] - pre[i]
        s2 = pre2[j] - pre2[i]
        return s2 - s * s / m

    INF = float("inf")
    dp = [[INF] * (n + 1) for _ in range(k + 1)]
    back = [[0] * (n + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for c in range(1, k + 1):
        for j in range(c * min_size, n - (k - c) * min_size + 1):
            best, bi = INF, -1
            for i in range((c - 1) * min_size, j - min_size + 1):
                if dp[c - 1][i] == INF:
                    continue
                val = dp[c - 1][i] + cost(i, j)
                if val < best:
                    best, bi = val, i
            dp[c][j], back[c][j] = best, bi
    if dp[k][n] == INF:
        return v, []
    bounds = []
    j = n
    for c in range(k, 0, -1):
        i = back[c][j]
        bounds.append((i, j))
        j = i
    bounds.reverse()
    return v, bounds


def cluster_of(values: list[float], bounds: list[tuple[int, int]]) -> list[dict]:
    """把 DP 切出来的区间整理成簇：成员值、簇心、标准差、范围。"""
    clusters = []
    for cid, (i, j) in enumerate(bounds):
        members = values[i:j]
        mean = sum(members) / len(members)
        var = sum((x - mean) ** 2 for x in members) / len(members)
        clusters.append({"cid": cid, "n": len(members), "center": mean,
                         "sd": var ** 0.5, "min": members[0], "max": members[-1]})
    return clusters

# ======================================================================
# 5. 归属：自我介绍锚点 → 可靠性门槛 → 话术优先的逐话轮判定
# ======================================================================

def find_anchor_spans(turns: list[dict], words: list[dict], max_words_after: int) -> list[dict]:
    """找"考生自我介绍"锚点的位置（纯文本，不需要音频）。

    只认**回答姓名问题位置**上的自称，这样考官的自我介绍（"I'm Sandra and this is
    my colleague Kerry."）不会被误当成考生锚点——它不在姓名问句的答句位置上。
    两种形态都要收：
      ① 同一话轮内自称（whisper 常把"考官问姓名 + 考生答"粘成一条，如
         "And your names are? My name is Florine."）→ 只取自称标记之后的词，
         否则 f0 会被考官的问句带偏；
      ② 紧接姓名问句的下一条短话轮（答句被单独切出来，如 "Federica."）。
    """
    anchors = []
    for t in turns:
        if not NAME_Q_RE.search(t["norm"]):
            continue
        toks = [norm_text(w["w"]) for w in words[t["i0"]:t["i1"] + 1]]
        joined = " ".join(toks)
        starts, pos = [], 0
        for tk in toks:
            starts.append(pos)
            pos += len(tk) + 1

        def tok_index(char_pos: int) -> int:
            idx = 0
            for k, st in enumerate(starts):
                if st <= char_pos:
                    idx = k
                else:
                    break
            return idx

        for m in SELF_ID_RE.finditer(joined):
            if m.group(1) in SELF_ID_STOP:
                continue
            k = tok_index(m.start())
            anchors.append({"turn": t["i"], "i0": t["i0"] + k, "i1": t["i1"],
                            "where": "同一话轮（问与答粘在一起）", "token": m.group(1)})
        if t["i"] + 1 < len(turns):
            nxt = turns[t["i"] + 1]
            if nxt["n_words"] <= max_words_after:
                nm = SELF_ID_RE.search(nxt["norm"])
                token = (nm.group(1) if nm and nm.group(1) not in SELF_ID_STOP
                         else disp(nxt["text"]).strip(" .?!"))
                anchors.append({"turn": nxt["i"], "i0": nxt["i0"], "i1": nxt["i1"],
                                "where": "紧接姓名问句的答句", "token": token})

    # 去重：同一话轮可能被两种规则同时命中（例如 "What's your name? My name is X."）
    seen, uniq = set(), []
    for a in anchors:
        key = (a["turn"], a["i0"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(a)
    return uniq


def find_anchors(anchor_spans: list[dict], words: list[dict], audio) -> list[dict]:
    """给锚点位置补上文与 f0；估不出 f0 的锚点丢掉（不能用来定归属带）。"""
    for a in anchor_spans:
        span = span_f0(audio, words[a["i0"]]["start"], words[a["i1"]]["end"])
        a["f0"] = span["median"]
        a["n_voiced"] = span["n_voiced"]
        a["start"] = round(float(words[a["i0"]]["start"]), 3)
        a["text"] = disp(" ".join(str(w["w"]) for w in words[a["i0"]:a["i1"] + 1]))
    return [a for a in anchor_spans if a["f0"] is not None]


def nearest_cluster(f0: float, clusters: list[dict]) -> int:
    """按最近簇心归属（并列时取较低的那个 → 结果确定）。"""
    return min(range(len(clusters)), key=lambda c: (abs(f0 - clusters[c]["center"]), c))


def evaluate_gates(clusters: list[dict], anchors: list[dict], args) -> dict:
    """G1 聚类门槛 + G2 锚点门槛。任一硬失败 → 不许硬分。"""
    total = sum(c["n"] for c in clusters)
    speakers = int(getattr(args, "speakers", 3))
    need = max(1, speakers - 1)                    # 考生侧应切的簇数
    boundaries = []
    g1_notes = []
    mode_note = None                               # 非 None：本模式不做归属，理由写这里
    # **结构前置条件：簇本身不够就没有"间隙"可比。**
    # 旧实现在只有 1 簇时下面那个 zip 一次都不走 → g1_notes 为空 → `g1_status = "pass"`，
    # 于是"只切出一个人"也会被判成"声学上可分出两位考生"，考官提示次数还会被标成"已测"
    # （外审 2026-10-04 用单簇输入复现）。要区分说话人，至少得有 2 簇才谈得上比较。
    if need < 2:
        # `--speakers 2` = 考官 + 一位考生：考生侧只有一簇是**设计如此**，不是聚类失败。
        # 但本工具只实现了"区分两位考生"这一条归属路径（G2 要两个自我介绍锚点），
        # 所以这个模式**不做**考官/考生的声学归属、B 组指标不产出。
        # （外审 2026-10-04 指出：上一版的硬门槛修掉了单簇误判，却把这条本来能跑的模式
        # 一并锁死了 —— 收窄可以，但要说清"为什么没有"，而不是含糊地报 fail。）
        g1_status = "untested"
        mode_note = (f"--speakers {speakers} 只设一位考生：考生侧本来就只切出 "
                     f"{len(clusters)} 簇，这是设计如此、不是聚类失败。本工具只实现"
                     f"「区分两位考生」这一条归属路径（G2 需要两个自我介绍锚点），"
                     f"所以这个模式不做考官/考生的声学归属，B 组指标不产出")
        g1_notes.append(mode_note)
    elif len(clusters) < 2:
        g1_status = "fail"
        g1_notes.append(
            f"考生侧只切出 {len(clusters)} 簇（--speakers {speakers} → 需要 {need} 簇，"
            f"而且要区分说话人至少得有两簇可比）→ 没有可比较的相邻簇，"
            f"结构上不足以区分说话人")
    else:
        g1_status = "pass"
    for a, b in zip(clusters, clusters[1:]):
        gap = b["center"] - a["center"]
        sep = gap / (a["sd"] + b["sd"]) if (a["sd"] + b["sd"]) > 0 else float("inf")
        share = min(a["n"], b["n"]) / total if total else 0.0
        ok = (gap >= args.min_gap_hz) and (sep >= args.min_sep) and (share >= args.min_share)
        boundaries.append({"from": a["cid"], "to": b["cid"], "gap": gap, "sep": sep,
                           "share": share, "ok": bool(ok)})
        if not ok:
            why = []
            if gap < args.min_gap_hz:
                why.append(f"间隙 {gap:.1f} Hz < {args.min_gap_hz:.1f} Hz")
            if sep < args.min_sep:
                why.append(f"分离指数 {sep:.2f} < {args.min_sep:.2f}")
            if share < args.min_share:
                why.append(f"最小簇占比 {share:.0%} < {args.min_share:.0%}")
            g1_notes.append(f"簇{a['cid']}→簇{b['cid']}：" + "；".join(why))
    if g1_notes and g1_status == "pass":
        g1_status = "fail"

    g2 = {"status": "untested", "note": "", "lo": None, "hi": None, "clusters": None}
    if len(anchors) < 2:
        g2["note"] = ("只找到 " + str(len(anchors)) + " 个自我介绍锚点（需要 2 个，一位考生一个）"
                      "→ 本门槛未测，只有聚类门槛生效")
    else:
        lo = min(anchors, key=lambda a: a["f0"])
        hi = max(anchors, key=lambda a: a["f0"])
        diff = hi["f0"] - lo["f0"]
        c_lo, c_hi = nearest_cluster(lo["f0"], clusters), nearest_cluster(hi["f0"], clusters)
        g2.update({"lo": lo, "hi": hi, "diff": diff, "clusters": (c_lo, c_hi)})
        if len(clusters) < 2:
            g2["status"] = "fail"
            g2["note"] = (f"考生侧只切出 {len(clusters)} 簇（--speakers {args.speakers}），"
                          f"结构上放不下两位考生，但录音里找到了 {len(anchors)} 个自我介绍锚点"
                          f"（{lo['f0']:.1f} / {hi['f0']:.1f} Hz）→ 若真有两位考生，--speakers 应给 3")
        elif c_lo == c_hi:
            g2["status"] = "fail"
            g2["note"] = (f"两位考生的自我介绍锚点都落在同一簇（簇{c_lo}）："
                          f"{lo['f0']:.1f} Hz ↔ {hi['f0']:.1f} Hz（差 {diff:.1f} Hz）"
                          f"→ 声学上分不开两位考生")
        elif diff < args.min_anchor_diff:
            g2["status"] = "fail"
            g2["note"] = (f"两位考生锚点相隔仅 {diff:.1f} Hz < {args.min_anchor_diff:.1f} Hz"
                          f"→ 小于同一说话人自身的中位 f0 波动，不可用")
        else:
            g2["status"] = "pass"
            g2["note"] = (f"两位考生锚点相隔 {diff:.1f} Hz（簇{c_lo} ↔ 簇{c_hi}）"
                          f"≥ {args.min_anchor_diff:.1f} Hz")
    separable = (g1_status == "pass") and (g2["status"] != "fail")
    return {"g1": {"status": g1_status, "boundaries": boundaries, "notes": g1_notes},
            "g2": g2, "separable": bool(separable), "mode_note": mode_note}


def assign_turns(turns: list[dict], exam_idx: set[int], clusters: list[dict],
                 anchors: list[dict], args) -> tuple[dict[int, str], dict[int, str]]:
    """逐话轮定归属。优先级：高精度考官话术 > 考生归属带（锚点±GUARD）> 声学簇 > 未归属。"""
    owner: dict[int, str] = {}
    how: dict[int, str] = {}
    bands = None
    anchored = {c["cid"]: [] for c in clusters}
    for a in anchors:
        anchored[nearest_cluster(a["f0"], clusters)].append(a["f0"])
    have = sorted(cid for cid, vals in anchored.items() if vals)
    if len(have) == 2:                       # 两簇各有锚点 → 各自开一条 ±GUARD 的判定带
        centers = [sum(anchored[cid]) / len(anchored[cid]) for cid in have]
        bands = {"low_cid": have[0], "high_cid": have[1],
                 "low_center": centers[0], "high_center": centers[1],
                 "thr_lo": centers[0] + args.guard_hz, "thr_hi": centers[1] - args.guard_hz}

    for t in turns:
        f0 = t["f0"]["median"] if t.get("f0") else None
        nv = t["f0"]["n_voiced"] if t.get("f0") else 0
        if t["i"] in exam_idx:
            owner[t["i"]], how[t["i"]] = L_EXAM, "strong-cue"
        elif f0 is None or nv < MIN_VOICED:
            owner[t["i"]], how[t["i"]] = L_UNKNOWN, "no-f0"
        elif bands:
            if f0 < bands["thr_lo"]:
                cid = bands["low_cid"]
            elif f0 > bands["thr_hi"]:
                cid = bands["high_cid"]
            else:
                cid = None
            if cid is None:
                owner[t["i"]], how[t["i"]] = L_UNKNOWN, "band"
            else:
                owner[t["i"]] = cand_label(cid)
                how[t["i"]] = f"anchor-band({f0:.0f}Hz)"
        else:
            cid = nearest_cluster(f0, clusters)
            owner[t["i"]] = cand_label(cid)
            how[t["i"]] = f"cluster({f0:.0f}Hz)"
    return owner, how


# ======================================================================
# 6. 提示次数 / 思考型停顿 / 覆盖率
# ======================================================================

def prompt_stats(turns: list[dict], owner: dict[int, str], args) -> dict:
    """"紧接着某考生话轮之后、且很短的外来话轮" = 提示。

    为什么这样定义：互动交际问的是"需不需要支持"。考生说完一句，对面立刻接一个
    短话轮（追问/重问/换角度），就是"需要支持"的现场痕迹；其中命中高精度考官话术的
    那些（"say why or why not" / "please tell me…"）是最硬的一类——只有考官会这么说。
    「外来」按"归属不是这位考生"算，**未归属也算外来**（它确实不是这位考生说的话）。
    """
    out: dict[str, dict] = {}
    cands = sorted({v for v in owner.values() if v.startswith("考生")})
    for label in cands:
        rec = {"short_incoming": 0, "strong": [], "exam_narrow": 0, "exam_wide": 0,
               "peer": 0, "unknown": 0, "normal_short": 0}
        for t in turns:
            if owner.get(t["i"]) != label:
                continue
            nxt = next((u for u in turns if u["i"] == t["i"] + 1), None)
            if nxt is None or owner.get(nxt["i"]) == label:
                continue
            if nxt["n_words"] > args.prompt_words:
                continue
            rec["short_incoming"] += 1
            strong = bool(nxt["strong"])
            if strong:
                rec["strong"].append({"i": nxt["i"], "start": nxt["start"], "text": nxt["text"],
                                      "words": nxt["n_words"], "kind": nxt["cue_kind"],
                                      "owner": owner.get(nxt["i"], L_UNKNOWN)})
            else:
                rec["normal_short"] += 1
            if owner.get(nxt["i"]) == L_EXAM:
                rec["exam_narrow"] += 1
            if strong or WEAK_RE.search(nxt["norm"]):
                rec["exam_wide"] += 1
            elif owner.get(nxt["i"], L_UNKNOWN).startswith("考生"):
                rec["peer"] += 1
            else:
                rec["unknown"] += 1
        out[label] = rec
    return out


def thinking_pauses(turns: list[dict], owner: dict[int, str], words: list[dict],
                    threshold: float) -> dict:
    """考生自己的"思考型停顿"。

    先说一个**结构约束**：话轮切分本身在静音 ≥0.52 s 处就断开，所以「某个话轮**内部**
    ≥1.0 s 的停顿」在数学上恒为 0（内部间隔必然 <0.52 s）。因此这里按它的实际含义实现：
    **该考生连续发言段内的 ≥threshold 停顿** —— 即同一说话人的相邻两条话轮之间、
    中间没有被别人插话的那段空白（考生说完一句、停一会儿、自己接着说）。
    同时单列"话轮内部最长停顿"（恒 <0.52 s）供复核。

    为什么要按"连续发言段"而不是"所有话轮之间的空白"：后者会把考官翻页/写字的空白
    算到考生头上（把"考官慢"记成"考生卡"）。
    偏差：同一说话人相邻话轮之间可能夹着**被误判成考生的考官话轮**，所以这个数偏大，
    明细里给了时间与前后的词，教师可以按听核对。
    """
    out: dict[str, dict] = {}
    runs: list[tuple[str, list[dict]]] = []      # (label, 连续同归属的话轮序列)
    for t in turns:
        label = owner.get(t["i"], L_UNKNOWN)
        if runs and runs[-1][0] == label:
            runs[-1][1].append(t)
        else:
            runs.append((label, [t]))
    for label, group in runs:
        rec = out.setdefault(label, {"count": 0, "max": 0.0, "detail": [],
                                     "in_turn_max": 0.0})
        for a, b in zip(group, group[1:]):
            gap = float(b["start"]) - float(a["end"])
            if gap >= threshold:
                rec["count"] += 1
                rec["max"] = max(rec["max"], gap)
                rec["detail"].append({
                    "start": round(float(a["end"]), 3), "dur": round(gap, 3),
                    "before": str(words[a["i1"]]["w"]), "after": str(words[b["i0"]]["w"]),
                    "prev_turn": a["i"], "next_turn": b["i"],
                    # 前一条话轮如果命中弱话术，它其实更像考官问句（被误判成考生）
                    # → 这段空白就不是"考生自己在想"，报告里要标出来
                    "suspect": bool(a["weak"])})
    for t in turns:                              # 话轮内部的间隔（恒 < 切分阈值，只做参考）
        rec = out.setdefault(owner.get(t["i"], L_UNKNOWN),
                             {"count": 0, "max": 0.0, "detail": [], "in_turn_max": 0.0})
        gaps = word_gaps(words, t["i0"], t["i1"])
        if gaps:
            rec["in_turn_max"] = max(rec["in_turn_max"], max(gaps))
    for rec in out.values():
        rec["detail"].sort(key=lambda d: (-d["dur"], d["start"]))
    return out


def coverage(turns: list[dict], owner: dict[int, str], wide_owner: dict[int, str]) -> dict:
    """词数覆盖率（窄口径 / 宽口径两套），口径与校准目录的 coverage.json 对齐。"""
    def tally(mapping: dict[int, str]) -> dict[str, int]:
        acc: dict[str, int] = {}
        for t in turns:
            label = mapping.get(t["i"], L_UNKNOWN)
            acc[label] = acc.get(label, 0) + t["n_words"]
        return acc

    narrow, wide = tally(owner), tally(wide_owner)
    total = sum(narrow.values())
    order = ([L_EXAM] + sorted(k for k in narrow if k.startswith("考生")) + [L_UNKNOWN])
    rows = []
    for label in order:
        if label not in narrow and label not in wide:
            continue
        n = narrow.get(label, 0)
        w = wide.get(label, 0)
        rows.append({"label": label, "narrow_words": n,
                     "narrow_pct": (100.0 * n / total) if total else 0.0,
                     "wide_words": w, "wide_pct": (100.0 * w / total) if total else 0.0})
    return {"total_words": total, "rows": rows}


def speaker_table(turns: list[dict], owner: dict[int, str], wide_owner: dict[int, str]) -> list[dict]:
    """逐说话人：话轮数 / 总词数 / 平均与最长话轮词数（窄口径），并给宽口径的对账数。"""
    order = ([L_EXAM] + sorted({v for v in owner.values() if v.startswith("考生")}) + [L_UNKNOWN])
    rows = []
    for label in order:
        mine = [t for t in turns if owner.get(t["i"]) == label]
        if not mine:
            continue
        words = sum(t["n_words"] for t in mine)
        wide_mine = [t for t in turns if wide_owner.get(t["i"]) == label]
        rows.append({
            "label": label, "turns": len(mine), "words": words,
            "mean_words": words / len(mine),
            "longest_words": max(t["n_words"] for t in mine),
            "wide_turns": len(wide_mine),
            "wide_words": sum(t["n_words"] for t in wide_mine),
            "weak_cue_turns": sum(1 for t in mine if WEAK_RE.search(t["norm"])),
        })
    return rows


# ======================================================================
# 7. 主流程
# ======================================================================

def analyze(args) -> dict:
    """跑完全部测量，返回一个可直接渲染成 markdown / JSON 的结果字典。"""
    transcript_path = Path(args.transcript)
    tr = load_transcript(transcript_path)
    words: list[dict] = tr["words"]
    segments: list[dict] = tr["segments"]
    meta: dict = tr.get("meta") or {}

    turns = build_turns(words, segments, args.word_gap, args.seg_gap)
    for t in turns:
        t["norm"] = norm_text(t["text"])
        t["weak"] = bool(WEAK_RE.search(t["norm"]))

    # 锚点先于话术判定：考官点名话术的正则要用锚点学到的考生姓名现算（仓库里不留姓名）
    anchor_spans = find_anchor_spans(turns, words, args.prompt_words)
    addr_re = name_address_re([a["token"] for a in anchor_spans])
    for t in turns:
        flow = bool(STRONG_RE.search(t["norm"]))
        addr = bool(addr_re.search(t["norm"])) if addr_re else False
        t["strong"] = flow or addr
        t["cue_kind"] = "流程话术" if flow else ("点名" if addr else "")
    exam_idx = {t["i"] for t in turns if t["strong"]}

    audio = None
    media_note = ""
    if args.media:
        audio = decode_mono16k(Path(args.media))
        media_note = f"已解码（{len(audio) / SR:.1f} s 音轨）"
    else:
        media_note = "未提供 --media → 跳过全部基频相关工作（无法做说话人归属）"
    length, length_src = recording_length(meta, (len(audio) / SR) if audio is not None else None,
                                         words)

    if audio is not None:
        for t in turns:
            t["f0"] = span_f0(audio, t["start"], t["end"])

    # ---- A 组 ----
    silences = find_silences(words, args.silence)
    edges = edge_silences(words, args.silence, length)
    sil_total = sum(s["dur"] for s in silences)
    handovers = handover_gaps(turns)
    w2t = word_to_turn(turns, len(words))

    group_a = {
        "duration": length, "duration_src": length_src,
        "n_words": len(words), "n_turns": len(turns),
        "turns_without_f0": sum(1 for t in turns if not t.get("f0") or t["f0"]["median"] is None),
        "silences": {"threshold": args.silence, "count": len(silences),
                     "total_s": sil_total,
                     "share_pct": (100.0 * sil_total / length) if length else 0.0,
                     "longest": max((s["dur"] for s in silences), default=0.0),
                     "edges": edges, "detail": silences},
        "handovers": {"n": len(handovers),
                      "median": (sorted(h["dur"] for h in handovers)[len(handovers) // 2]
                                 if handovers else 0.0),
                      "min": min((h["dur"] for h in handovers), default=0.0),
                      "max": max((h["dur"] for h in handovers), default=0.0),
                      "detail": handovers},
    }

    # ---- B 组：没有音轨就只有 A 组 ----
    if audio is None:
        # 仍然给出 owner（只按话术能判的那几条）——沉默表要写"前后话轮属于谁"，
        # 没有它就会把整张表写成"未归属"以外的样子（或直接 KeyError）
        owner = {t["i"]: (L_EXAM if t["i"] in exam_idx else L_UNKNOWN) for t in turns}
        how = {t["i"]: ("strong-cue" if t["i"] in exam_idx else "no-media") for t in turns}
        return {"transcript": str(transcript_path),
                "media": None, "media_note": media_note,
                "caliber": caliber_dict(args),
                "group_a": group_a, "group_b": None,
                "owner": owner, "how": how, "turns": turns, "w2t": w2t,
                "verdict": {"separable": False,
                            "reason": "未提供 --media：无法估基频，说话人归属本次未测",
                            "prompts_tested": False}}

    pool = [t["f0"]["median"] for t in turns
            if t["i"] not in exam_idx and t["f0"]["median"] is not None
            and t["f0"]["n_voiced"] >= MIN_VOICED]
    k_pool = max(1, args.speakers - 1)
    min_size = max(1, int(round(args.min_share * len(pool)))) if pool else 1
    vals, bounds = optimal_kmeans_1d(pool, k_pool, min_size) if pool else ([], [])
    clusters = cluster_of(vals, bounds) if bounds else []
    if not clusters and pool:            # 点太少切不动 → 退化成 1 簇，仍然如实报告
        clusters = cluster_of(sorted(pool), [(0, len(pool))])

    anchors = find_anchors(anchor_spans, words, audio)
    anchors = [a for a in anchors if a["f0"] is not None]
    gates = evaluate_gates(clusters, anchors, args) if clusters else {
        "g1": {"status": "fail", "boundaries": [], "notes": ["有效话轮太少，聚不出簇"]},
        "g2": {"status": "untested", "note": "无簇", "lo": None, "hi": None, "clusters": None},
        "separable": False}

    # 归属：只在判定"可分"时才做；不可分时所有人都是未归属 → 只报 A 组
    if gates["separable"]:
        owner, how = assign_turns(turns, exam_idx, clusters, anchors, args)
    else:
        owner = {t["i"]: (L_EXAM if t["i"] in exam_idx else L_UNKNOWN) for t in turns}
        how = {t["i"]: ("strong-cue" if t["i"] in exam_idx else "not-separable") for t in turns}

    # 宽口径：把命中弱话术的"考生"话轮改判考官，用来给考生指标划一个下限
    wide_owner = dict(owner)
    for t in turns:
        if wide_owner.get(t["i"], "").startswith("考生") and t["weak"]:
            wide_owner[t["i"]] = L_EXAM

    group_b = {
        "clusters": clusters,
        "boundaries": gates["g1"]["boundaries"],
        "anchors": anchors,
        "gates": gates,
        "bands": None,
        "speakers": speaker_table(turns, owner, wide_owner) if gates["separable"] else [],
        "prompts": prompt_stats(turns, owner, args) if gates["separable"] else {},
        "thinking": thinking_pauses(turns, owner, words, args.silence),
        "coverage": coverage(turns, owner, wide_owner),
        "mixed": [{"i": t["i"], "start": t["start"], "words": t["n_words"], "text": t["text"]}
                  for t in turns if t["strong"] and t["n_words"] > args.prompt_words],
    }
    anchored = {c["cid"]: [] for c in clusters}
    for a in anchors:
        anchored[nearest_cluster(a["f0"], clusters)].append(a["f0"])
    have = sorted(cid for cid, v in anchored.items() if v)
    if len(have) == 2:
        centers = [sum(anchored[cid]) / len(anchored[cid]) for cid in have]
        group_b["bands"] = {"low_cid": have[0], "high_cid": have[1],
                            "low_center": centers[0], "high_center": centers[1],
                            "thr_lo": centers[0] + args.guard_hz,
                            "thr_hi": centers[1] - args.guard_hz}

    verdict = {
        "separable": gates["separable"],
        "reason": (gates.get("mode_note") or
                   ("声学上可分出两位考生"
                    if gates["separable"] else
                    "说话人无法可靠区分 → 只报 A 组；考官提示次数本次未测")),
        "prompts_tested": bool(gates["separable"]),
    }
    return {"transcript": str(transcript_path), "media": args.media, "media_note": media_note,
            "caliber": caliber_dict(args), "group_a": group_a, "group_b": group_b,
            "verdict": verdict, "owner": owner, "how": how, "turns": turns, "w2t": w2t}


def caliber_dict(args) -> dict:
    """本次实际生效的口径（原样写进报告，供复核/复现）。"""
    return {
        "turn_gap_s": args.word_gap, "turn_seg_gap_s": args.seg_gap,
        "silence_s": args.silence, "prompt_words": args.prompt_words,
        "speakers": args.speakers, "pool_clusters": max(1, args.speakers - 1),
        "f0_window_ms": FRAME_S * 1000, "f0_hop_ms": HOP_S * 1000,
        "f0_range_hz": [F0_MIN, F0_MAX], "dual_agree": DUAL_AGREE,
        "min_gap_hz": args.min_gap_hz, "min_sep": args.min_sep,
        "min_share": args.min_share, "guard_hz": args.guard_hz,
        "min_anchor_diff_hz": args.min_anchor_diff,
        "min_voiced_frames": MIN_VOICED,
    }


# ======================================================================
# 8. 报告渲染
# ======================================================================

def render_markdown(res: dict) -> str:
    """把结果渲染成给人看的 markdown：口径在前，A 组照常，B 组按可分性出现。"""
    cal = res["caliber"]
    a, b = res["group_a"], res["group_b"]
    owner, turns = res["owner"], res["turns"]
    w2t = res["w2t"]
    lines: list[str] = []
    ap = lines.append

    ap(f"# 互动交际证据 · {Path(res['transcript']).name}")
    ap("")
    ap("> 由 `scripts/kpf_interact.py` 生成（离线、不联网）。**只测量互动证据，不做任何评分判断。**")
    ap("")
    ap("| 项 | 值 |")
    ap("|---|---|")
    ap(f"| 转写文件 | `{res['transcript']}` |")
    ap(f"| 媒体文件 | {('`' + str(res['media']) + '`') if res['media'] else '（未提供）'} |")
    ap(f"| 音轨状态 | {res['media_note']} |")
    ap(f"| 全片时长 | {f1(a['duration'])} s（来源：{a['duration_src']}） |")
    ap(f"| 词数 | {a['n_words']} |")
    ap(f"| 话轮单元数 | **{a['n_turns']}**（切分规则见下） |")
    ap("")

    ap("## 一、本次口径（固定写出，便于复核）")
    ap("")
    ap(f"- 话轮切分：词间静音 ≥{cal['turn_gap_s']:.2f} s，或 whisper 段边界处 ≥{cal['turn_seg_gap_s']:.2f} s")
    ap(f"- 沉默 = ASR 词间空白（**不是能量 VAD**：whisper 没转写出的嘟囔、噪声也算沉默，"
       f"它是沉默的上限）；≥{cal['silence_s']:.1f} s 才计入")
    ap(f"- 思考型停顿：话轮切分在 ≥{cal['turn_gap_s']:.2f} s 就断开，所以「某个话轮内部 ≥"
       f"{cal['silence_s']:.1f} s」恒为 0；本脚本按它的实际含义统计**该考生连续发言段内**的 "
       f"≥{cal['silence_s']:.1f} s 停顿（说完一句、停一会儿、自己接着说），并单列话轮内部最长停顿")
    ap(f"- 提示判定：紧随某考生话轮之后、词数 ≤{cal['prompt_words']} 的外来话轮"
       "（「外来」= 归属不是这位考生；未归属也算外来）")
    ap(f"- 考官话术：① 考试流程话术（高精度清单：this is my colleague / can I have your mark "
       f"sheets? / say why or why not / in this part of the test / that is the end of the test / "
       f"here are… / I'd like you to / please tell me… / what's your name 等）；"
       "② **点名**——用自我介绍锚点学到的考生姓名现算（仓库里不留具体姓名）。命中即判考官，"
       "优先于声学判断")
    ap(f"- 基频：{cal['f0_window_ms']:.0f} ms 窗 / {cal['f0_hop_ms']:.0f} ms 跳，"
       f"{cal['f0_range_hz'][0]:.0f}–{cal['f0_range_hz'][1]:.0f} Hz；"
       f"倒谱 + 自相关双估计，相差 <{cal['dual_agree']:.0%} 才采信；取话轮内中位数（帧网格锚定文件起点）")
    ap(f"- 聚类：1-D 最优 k-means（动态规划，确定性），考生侧 {cal['pool_clusters']} 簇，"
       f"每簇 ≥{cal['min_share']:.0%} 有效话轮")
    ap(f"- 归属带：以考生自我介绍锚点为心 ±{cal['guard_hz']:.1f} Hz；窗外不硬塞，进未归属")
    ap(f"- 可靠性门槛：相邻簇心间隙 ≥{cal['min_gap_hz']:.1f} Hz 且 "
       f"间隙/(σ₁+σ₂) ≥{cal['min_sep']:.2f}（G1）；两位考生锚点间隔 ≥{cal['min_anchor_diff_hz']:.1f} Hz "
       "且落在不同簇（G2）。**两把都要过，否则不硬分**")
    ap("")

    # ---------------- A 组 ----------------
    ap("## 二、A 组（不需要说话人归属）")
    ap("")
    ap("### 沉默")
    ap("")
    sil = a["silences"]
    ap("| 指标 | 值 |")
    ap("|---|---|")
    ap(f"| ≥{sil['threshold']:.1f} s 沉默次数 | {sil['count']} |")
    ap(f"| 最长沉默 | {f1(sil['longest'])} s |")
    ap(f"| 沉默总秒数 | {f1(sil['total_s'])} s |")
    ap(f"| 占全片 | {f1(sil['share_pct'])} % |")
    for e in sil["edges"]:
        ap(f"| {e['edge']}静音 | {f1(e['dur'])} s（{mmss(e['start'])}–{mmss(e['end'])}） |")
    ap("")
    ap(f"### 最长的 5 段沉默")
    ap("")
    ap("| # | 起 | 止 | 时长 s | 前话轮 | 后话轮 |")
    ap("|---|---|---|---:|---|---|")
    for n, s in enumerate(sorted(sil["detail"], key=lambda x: (-x["dur"], x["start"]))[:5], 1):
        before = owner.get(w2t[s["i_before"]], L_UNKNOWN) if s.get("i_before") is not None else "未知"
        after = owner.get(w2t[s["i_after"]], L_UNKNOWN) if s.get("i_after") is not None else "未知"
        ap(f"| {n} | {mmss(s['start'])} | {mmss(s['end'])} | {f1(s['dur'])} | {before} | {after} |")
    ap("")

    ap("### 话轮交接间隙")
    ap("")
    ho = a["handovers"]
    pairs: dict[tuple[str, str], list[float]] = {}
    for h in ho["detail"]:
        f = owner.get(h["i_from"], L_UNKNOWN)
        t = owner.get(h["i_to"], L_UNKNOWN)
        pairs.setdefault((f, t), []).append(h["dur"])
    ap(f"共 {ho['n']} 处交接；中位 {f1(ho['median'])} s，最短 {f1(ho['min'])} s，最长 {f1(ho['max'])} s。")
    ap("")
    ap("| 交接 | 次数 | 中位 s | 最短 s | 最长 s |")
    ap("|---|---:|---:|---:|---:|")
    for (f, t), ds in sorted(pairs.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        ds_sorted = sorted(ds)
        ap(f"| {f} → {t} | {len(ds)} | {f1(ds_sorted[len(ds_sorted) // 2])} | "
           f"{f1(ds_sorted[0])} | {f1(ds_sorted[-1])} |")
    ap("")

    # ---------------- B 组：不可分 / 未测 ----------------
    if b is None or not res["verdict"]["separable"]:
        ap("## 三、B 组（说话人归属）：本次未测")
        ap("")
        ap(f"**{res['verdict']['reason']}**")
        ap("")
        if b is not None:
            ap(render_gate_section(b))
        ap("### 因此以下指标本次没有产出")
        ap("")
        ap("- 每位说话人的话轮数 / 词数 / 平均与最长话轮词数")
        ap("- **考官对每位考生的提示次数**（含高精度话术命中）——按硬要求记为「未测」，不硬凑")
        ap("- 每位考生自己的思考型停顿")
        ap("")
        ap(render_caliber_notes(res))
        return "\n".join(lines) + "\n"

    # ---------------- B 组：可分 ----------------
    ap("## 三、B 组（说话人归属）")
    ap("")
    ap(render_gate_section(b))

    ap("### 每位说话人")
    ap("")
    ap("| 说话人 | 话轮数 | 总词数 | 平均话轮词数 | 最长话轮词数 | 宽口径话轮数 | 疑似考官残留 |")
    ap("|---|---:|---:|---:|---:|---:|---:|")
    for r in b["speakers"]:
        ap(f"| {r['label']} | {r['turns']} | {r['words']} | {f1(r['mean_words'])} | "
           f"{r['longest_words']} | {r['wide_turns']} | {r['weak_cue_turns']} |")
    ap("")
    ap("> 宽口径 = 把命中弱话术（thank you / do you think / what about you 等宽泛句型）的考生话轮"
       "改判考官后的对账数；**疑似考官残留**那一列是「该说话人话轮里命中弱话术的条数」——"
       "这一列越大，说明话轮数/词数里混进考官问句越多（考生侧是上限）。")
    ap("")

    ap("### 考官提示次数（紧随考生话轮之后的短外来话轮）")
    ap("")
    ap(f"| 考生 | 外来短话轮（≤{cal['prompt_words']} 词） | 其中高精度话术 | 其中归属考官（窄） | "
       "归属考官（宽口径） | 普通短话轮 | 来自同伴 | 未归属 |")
    ap("|---|---:|---:|---:|---:|---:|---:|---:|")
    for label in sorted(b["prompts"]):
        r = b["prompts"][label]
        ap(f"| {label} | {r['short_incoming']} | **{len(r['strong'])}** | {r['exam_narrow']} | "
           f"{r['exam_wide']} | {r['normal_short']} | {r['peer']} | {r['unknown']} |")
    ap("")
    ap("高精度话术命中的那些（「重问/追加提问」，是最硬的「需要支持」证据）：")
    prev_of = {t["i"] + 1: t for t in turns}
    for label in sorted(b["prompts"]):
        for s in b["prompts"][label]["strong"]:
            pv = prev_of.get(s["i"])
            pv_txt = (f"；前一条是 {pv['cue_kind'] or '考生话轮'} {mmss(pv['start'])}"
                      f"“{pv['text'][:34]}”" if pv else "")
            ap(f"- **{label}** ← {mmss(s['start'])}（{s['words']} 词，归属：{s['owner']}，"
               f"{s['kind']}）：{chr(8220)}{s['text']}{chr(8221)}{pv_txt}")
    if not any(b["prompts"][l]["strong"] for l in b["prompts"]):
        ap("- （本次没有命中高精度考官话术的短外来话轮）")
    ap("")
    ap("> 口径提醒：这一列依赖「前一条话轮确实属于该考生」。若前一条其实是考官"
       "（未命中话术），这条提示就记错了人——上表「前一条是…」写明了前一条被判成什么，可逐条核对。")
    ap("")

    ap("### 考生自己的思考型停顿（连续发言段内 ≥阈值 的停顿）")
    ap("")
    ap("| 说话人 | 次数 | 最长 s | 其中可疑条数 | 话轮内部最长 s | 明细（时间 / 前词→后词 / 时长） |")
    ap("|---|---:|---:|---:|---:|---|")
    for label in [r["label"] for r in b["speakers"]]:
        rec = b["thinking"].get(label, {"count": 0, "max": 0.0, "detail": [], "in_turn_max": 0.0})
        if not label.startswith("考生"):
            continue
        detail = "；".join(f"{mmss(d['start'])} {d['before']}→{d['after']} {d['dur']:.1f}s"
                           + ("（前一条疑似考官）" if d.get("suspect") else "")
                           for d in rec["detail"][:6])
        suspect = sum(1 for d in rec["detail"] if d.get("suspect"))
        ap(f"| {label} | {rec['count']} | {f1(rec['max'])} | {suspect} | {f1(rec['in_turn_max'])} | "
           f"{detail or '–'} |")
    ap("")
    ap("> 「话轮内部最长」恒 <0.52 s（话轮切分就在 0.52 s 处断开）——列在这里是为了让"
       "「这条指标为什么是 0」有据可查；真正的思考型停顿落在连续发言段之间。")
    ap("")

    ap("### 词数覆盖率（两种口径）")
    ap("")
    cov = b["coverage"]
    ap(f"| 说话人 | 窄口径词数 | 占比 | 宽口径词数 | 占比 |")
    ap("|---|---:|---:|---:|---:|")
    for r in cov["rows"]:
        ap(f"| {r['label']} | {r['narrow_words']} | {f1(r['narrow_pct'])} % | "
           f"{r['wide_words']} | {f1(r['wide_pct'])} % |")
    ap(f"| **合计** | {cov['total_words']} | 100.0 % | {cov['total_words']} | 100.0 % |")
    ap("")

    ap(render_caliber_notes(res))
    return "\n".join(lines) + "\n"


def render_gate_section(b: dict) -> str:
    """分离质量 + 门槛判定（无论可分与否都写出来，让人能自己复核）。"""
    out: list[str] = []
    ap = out.append
    ap("### 声学分离质量")
    ap("")
    if not b["clusters"]:
        ap("本次聚不出簇（有效话轮太少或没有有效基频）→ 直接判定不可分。")
        ap("")
        return "\n".join(out) + "\n"
    ap("| 簇 | 话轮数 | 簇心 f0 | σ | f0 范围 |")
    ap("|---|---:|---:|---:|---|")
    for c in b["clusters"]:
        ap(f"| 簇{c['cid']}（考生{cand_label(c['cid'])[-1]}） | {c['n']} | {f1(c['center'])} Hz | "
           f"{f1(c['sd'])} Hz | {f1(c['min'])}–{f1(c['max'])} Hz |")
    ap("")
    if b["boundaries"]:
        ap("| 相邻簇 | 间隙 | 分离指数 间隙/(σ₁+σ₂) | 最小簇占比 | 判定 |")
        ap("|---|---:|---:|---:|---|")
        for bd in b["boundaries"]:
            ap(f"| 簇{bd['from']} → 簇{bd['to']} | {f1(bd['gap'])} Hz | {bd['sep']:.2f} | "
               f"{bd['share']:.0%} | {'过' if bd['ok'] else '不过'} |")
        ap("")
    ap("### 自我介绍锚点（零推断的身份证据）")
    ap("")
    if b["anchors"]:
        ap("| 位置 | 原文 | f0 中位 | 落在 | 取处 |")
        ap("|---|---|---:|---|---|")
        for a in sorted(b["anchors"], key=lambda x: x["f0"]):
            cl = nearest_cluster(a["f0"], b["clusters"])
            ap(f"| {mmss(a['start'])}（话轮 {a['turn']}） | “{cell(a['text'][:48])}” | "
               f"{f1(a['f0'])} Hz | 簇{cl} | {a['where']} |")
    else:
        ap("本次没有找到自我介绍锚点（录音里没有「回答姓名问题」的位置）。")
    ap("")
    g1, g2 = b["gates"]["g1"], b["gates"]["g2"]
    ap("### 门槛判定")
    ap("")
    ap(f"- G1 聚类门槛：**{g1['status']}**" + ("；".join([""] + g1["notes"]) if g1["notes"] else ""))
    ap(f"- G2 锚点门槛：**{g2['status']}**（{g2['note']}）")
    if b.get("bands"):
        bd = b["bands"]
        ap(f"- 归属带：<{f1(bd['thr_lo'])} Hz → 音高较低的考生；>{f1(bd['thr_hi'])} Hz → "
           f"音高较高的考生；中间保留进未归属（锚点中心 "
           f"{f1(bd['low_center'])} / {f1(bd['high_center'])} Hz）")
    ap("")
    return "\n".join(out) + "\n"


def render_caliber_notes(res: dict) -> str:
    """第四节：本次哪些可信 / 哪些没测 / 已知会把话说错的地方。"""
    b = res["group_b"]
    a = res["group_a"]
    out: list[str] = []
    ap = out.append
    ap("## 四、本次哪些可信 / 哪些没测")
    ap("")
    ap("**可信**")
    ap("")
    ap("- A 组全部指标：话轮单元数、沉默、交接间隙、片头片尾静音。"
       "这些只依赖词级时间戳，不依赖说话人归属")
    if res["verdict"]["separable"] and b:
        ap("- **高精度考官话术命中那几条**是最硬的一类证据：它只依赖文本正则，"
           "不需要声学判对（声学判错时它反而更可靠，见下）")
        ap("- 考生自己的思考型停顿：只统计该考生连续发言段内的停顿，与「别处谁在说话」无关；"
           "但**依赖前一条话轮的归属正确**，明细里标了「前一条疑似考官」的可疑条数")
    ap("")
    ap("**没测 / 有偏差**")
    ap("")
    if b is None:
        ap("- 说话人归属相关的一切（逐说话人话轮数、考官提示次数、逐考生停顿）："
           "本次没有 `--media`，无法估基频 → 未测")
    else:
        if not res["verdict"]["separable"]:
            ap("- **说话人无法可靠区分 → 逐说话人指标与考官提示次数全部未测**（不硬凑）")
        else:
            ex = next((r for r in b["speakers"] if r["label"] == L_EXAM), None)
            if ex:
                ap(f"- 考官侧只算了**高精度话术命中的 {ex['turns']} 条话轮**；"
                   "没有命中话术的考官问句（很多）会落到考生侧或未归属 → "
                   "**考生词数是上限、考官词数是下限**（两种口径的差见覆盖率表）")
            weak = [r for r in b["speakers"] if r["label"].startswith("考生") and r["weak_cue_turns"]]
            if weak:
                ap("- 可能被高估的："
                   + "；".join(f"{r['label']} 有 {r['weak_cue_turns']} 条话轮命中弱话术"
                               f"（更像考官问句）" for r in weak))
        if b["mixed"] and res["verdict"]["separable"]:
            ap("")
            ap("**混段清单**：这些话轮里考官的问与考生的答粘在一起（whisper 没有断开），"
               "整条记在考官名下，里头的考生词数会偏少：")
            for m in b["mixed"]:
                ap(f"  - {mmss(m['start'])}（{m['words']} 词）：“{m['text'][:60]}”")
        if b["mixed"] and not res["verdict"]["separable"]:
            ap(f"- 另有 {len(b['mixed'])} 条「混段」（考官的问与考生的答粘在同一话轮里，"
               "whisper 没断开）；因为本次没有归属，它不影响任何数字，只记在 JSON 里")
        ap("")
        ap("**已知会把话说错的地方**")
        ap("")
        ap(f"- 沉默用的是 ASR 词间空白，不是能量 VAD：whisper 漏写的词会被算成沉默"
           f"（本片共 {a['turns_without_f0']} 条话轮连 f0 都估不出来，就是段太短/太低信）")
        ap("- 音高带重叠的录音（两位同性别的考生）**本来就不该用这套方法分**；"
           "门槛拦住的情况会直接写「无法可靠区分」，而不是给一个像模像样的错答案")
    ap("")
    return "\n".join(out)


# ======================================================================
# 9. 命令行
# ======================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从「有对手方」的口语录音里量出互动证据（提示 / 沉默 / 话轮结构）",
        epilog="退出码：0 = 出了报告；2 = 输入不合法（文件缺失 / schema 不对 / 参数越界）")
    p.add_argument("transcript", help="kpf_asr.py 产出的转写 JSON")
    p.add_argument("--media", default=None, help="原始音/视频（给不出就只报 A 组指标）")
    p.add_argument("--speakers", type=int, default=3,
                   help="录音里有几个人（默认 3 = 考官 + 2 考生；支持 2–4）。"
                        "**2 = 考官 + 一位考生：这个模式不做说话人归属、只出 A 组指标**"
                        "（本工具只实现「区分两位考生」这一条归属路径，G2 要两个自我介绍锚点）")
    p.add_argument("--silence", type=float, default=SILENCE_MIN, help=f"沉默阈值（默认 {SILENCE_MIN}）")
    p.add_argument("--out", default=None, help="markdown 报告路径（默认 <转写名>-互动证据.md）")
    p.add_argument("--json", dest="json_out", default=None, help="机器可读结果的路径")
    p.add_argument("--word-gap", type=float, default=TURN_GAP, help=f"话轮切分静音阈值（默认 {TURN_GAP}）")
    p.add_argument("--seg-gap", type=float, default=TURN_SEG_GAP, help=f"段边界切分阈值（默认 {TURN_SEG_GAP}）")
    p.add_argument("--prompt-words", type=int, default=PROMPT_WORDS,
                   help=f'"提示"的词数上限（默认 {PROMPT_WORDS}）')
    p.add_argument("--min-gap-hz", type=float, default=MIN_GAP_HZ, help="G1 相邻簇心最小间隙")
    p.add_argument("--min-sep", type=float, default=MIN_SEP, help="G1 分离指数门槛")
    p.add_argument("--min-cluster-share", dest="min_share", type=float, default=MIN_SHARE,
                   help="G1 最小簇占比")
    p.add_argument("--guard-hz", type=float, default=GUARD_HZ, help="归属带半宽")
    p.add_argument("--min-anchor-diff-hz", dest="min_anchor_diff", type=float,
                   default=MIN_ANCHOR_DIFF_HZ, help="G2 两位考生锚点最小间隔")
    return p


def validate_args(args) -> None:
    """参数越界一律当输入不合法（退出码 2），不悄悄改成默认值。"""
    if not 2 <= args.speakers <= 4:
        raise InputError(f"--speakers 只支持 2–4（收到 {args.speakers}）："
                         "1 个人没有「对手方」，4 个以上不在本脚本口径内")
    if args.silence <= 0 or args.word_gap <= 0 or args.seg_gap <= 0:
        raise InputError("阈值参数必须为正数")
    if args.word_gap < args.seg_gap:
        raise InputError("--word-gap 必须 ≥ --seg-gap（段边界上允许更短的缝，但不能反过来）")
    if args.prompt_words < 1:
        raise InputError("--prompt-words 至少为 1")
    if args.min_share <= 0 or args.min_share >= 0.5:
        raise InputError("--min-cluster-share 需要在 (0, 0.5) 之间（两簇时上界是 50%）")
    if args.speakers > 2 and args.min_share * (args.speakers - 1) >= 1:
        raise InputError("--min-cluster-share 太大，按这个占比切不出这么多个簇")


def machine_json(res: dict) -> dict:
    """机器可读结果（不含话轮明细以外的临时字段；排序固定 → 逐字节可复现）。"""
    b = res["group_b"]
    out = {
        "transcript": res["transcript"], "media": res["media"],
        "media_note": res["media_note"], "caliber": res["caliber"],
        "group_a": {k: v for k, v in res["group_a"].items()},
        "verdict": res["verdict"],
        "group_b": None,
    }
    if b is not None:
        out["group_b"] = {
            "clusters": b["clusters"], "boundaries": b["boundaries"],
            "anchors": [{k: v for k, v in a.items() if k != "vals"} for a in b["anchors"]],
            "gates": {"g1": b["gates"]["g1"], "g2": {k: v for k, v in b["gates"]["g2"].items()
                                                     if k not in ("lo", "hi")},
                      "separable": b["gates"]["separable"]},
            "bands": b["bands"], "speakers": b["speakers"], "prompts": b["prompts"],
            "thinking": b["thinking"], "coverage": b["coverage"], "mixed": b["mixed"],
        }
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        res = analyze(args)
    except InputError as exc:
        print(f"输入不合法：{exc}", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else Path(args.transcript).with_suffix("").with_name(
        Path(args.transcript).stem + "-互动证据.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(res), encoding="utf-8")
    if args.json_out:
        jp = Path(args.json_out)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(machine_json(res), ensure_ascii=False, indent=1) + "\n",
                      encoding="utf-8")

    a = res["group_a"]
    print(f"话轮单元 {a['n_turns']} 个 / {a['n_words']} 词 / {f1(a['duration'])} s")
    print(f"沉默 ≥{a['silences']['threshold']:.1f} s：{a['silences']['count']} 次，"
          f"最长 {f1(a['silences']['longest'])} s，占 {f1(a['silences']['share_pct'])}%")
    if res["verdict"]["separable"]:
        for r in res["group_b"]["speakers"]:
            pr = res["group_b"]["prompts"].get(r["label"])
            extra = (f"，提示 {pr['short_incoming']}（硬话术 {len(pr['strong'])}）" if pr else "")
            print(f"  {r['label']}：{r['turns']} 话轮 / {r['words']} 词 / "
                  f"平均 {f1(r['mean_words'])} 词{extra}")
    else:
        print(f"B 组：{res['verdict']['reason']}")
    print(f"报告 → {out_path}")
    if args.json_out:
        print(f"JSON → {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
