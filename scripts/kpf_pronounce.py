#!/usr/bin/env python3
"""KPF 口语作业 · 发音评分（provider 链，免费优先、多源备用）

provider 优先级（auto 模式）：
  1. xfyun   讯飞语音评测（ISE / 声通 suntone），国内首选：免信用卡、免代理、有免费额度
             → 评的是**念题部分**，需要参考文本，所以必须给 --questions（一行一题）；
             缺 --questions 时明确降级，不会静默给 0 分
  2. azure   Azure 语音服务发音评估，F0 免费层 5 小时/月（unscripted，无需参考文本）
             → 给 Accuracy / Fluency / Prosody，用 to_band() 映射到 0–5
  3. local   本机 whisper 词级置信度 + 双引擎差异 → **只出风险点位与等级，不出分数**
             （置信度到 0–5 没有经过验证的映射，硬编分数就是编数据）
  4. manual  输出抽听表模板，教师听点位后填分

0–5 的映射阈值只有一份实现：`kpf_xfyun.to_band()`（其他脚本 import 它），
与 references/02-rubric.md 第 5.3 节一致。所有 provider 的输出都归一到同一结构，
保证换源不影响评分口径。

用法：
  kpf_pronounce.py <转写.json> --provider auto [--questions questions/<页号>.txt] [--out 发音.md]
  kpf_pronounce.py <转写.json> --provider auto|xfyun|azure|local|manual
  kpf_pronounce.py <转写.json> --crosscheck <交叉验证.md>
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kpf_analyze import mmss  # 时间戳唯一实现（含进位修正）  # noqa: E402
from kpf_xfyun import avg_of, fmt_band, fmt_score, to_band  # 0–5 映射唯一实现  # noqa: E402

CONFIG_PATH = Path.home() / ".kpf-speaking" / "config.json"
LOW_P = 0.5
WINDOW_WORDS = 10
FUNCTION_WORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with",
    "is", "am", "are", "was", "were", "be", "been", "do", "does", "did", "i", "you",
    "he", "she", "it", "we", "they", "my", "your", "his", "her", "its", "our", "their",
    "that", "this", "these", "those", "as", "so", "if", "not", "no", "yes", "well",
}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9']", "", str(text).lower())


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


# ---------------------------------------------------------------- azure

def to_16k_wav(src: Path, dst: Path) -> None:
    """任意音视频 → 16kHz 单声道 PCM WAV（Azure 要求）。用 PyAV，不需要系统 ffmpeg。"""
    import av  # faster-whisper 的依赖，已随 venv 安装
    import numpy as np

    container = av.open(str(src))
    stream = next(s for s in container.streams if s.type == "audio")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    for frame in container.decode(stream):
        for out in resampler.resample(frame):
            chunks.append(out.to_ndarray().reshape(-1))
    for out in resampler.resample(None):  # flush
        chunks.append(out.to_ndarray().reshape(-1))
    container.close()
    if not chunks:
        sys.exit(f"从 {src} 里没有解出音频。")
    pcm = np.concatenate(chunks).astype("int16")

    dst.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dst), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(16000)
        fh.writeframes(pcm.tobytes())


def provider_azure(payload: dict, cfg: dict, media: Path, language: str = "en-US") -> dict:
    import requests

    az = cfg.get("azure") or {}
    key, region = (az.get("key") or "").strip(), (az.get("region") or "").strip()
    if not key or not region:
        raise RuntimeError(
            f"Azure 未配置。在 {CONFIG_PATH} 写入 "
            '{"azure": {"key": "<SPEECH_KEY>", "region": "<如 eastasia>"}}'
        )

    wav = media.with_suffix(".16k.wav")
    if not wav.exists():
        print(f"[azure] 转 16kHz 单声道 WAV → {wav.name}", flush=True)
        to_16k_wav(media, wav)

    assessment = {
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "EnableMiscue": False,
        # unscripted：不提供 ReferenceText，Azure 按自由说话评测
    }
    url = (f"https://{region}.stt.speech.microsoft.com/speech/recognition/conversation/"
           f"cognitiveservices/v1?language={language}&format=detailed")
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
        "Pronunciation-Assessment": base64.b64encode(
            json.dumps(assessment, ensure_ascii=False).encode("utf-8")).decode("ascii"),
        "Accept": "application/json",
    }
    print("[azure] 调用发音评估…", flush=True)
    with wav.open("rb") as fh:
        resp = requests.post(url, headers=headers, data=fh, timeout=600)
    if resp.status_code != 200:
        raise RuntimeError(f"Azure 返回 {resp.status_code}：{resp.text[:400]}")
    data = resp.json()
    best = (data.get("NBest") or [{}])[0]
    pa = best.get("PronunciationAssessment") or {}
    if not pa:
        raise RuntimeError(f"Azure 响应里没有 PronunciationAssessment：{json.dumps(data)[:400]}")

    words = []
    for w in best.get("Words", []):
        wa = w.get("PronunciationAssessment") or {}
        words.append({
            "w": w.get("Word"),
            "accuracy": wa.get("AccuracyScore"),
            "error_type": wa.get("ErrorType"),
        })
    weakest = sorted([w for w in words if w["accuracy"] is not None], key=lambda x: x["accuracy"])[:12]

    return {
        "provider": "azure",
        "accuracy": pa.get("AccuracyScore"),
        "fluency": pa.get("FluencyScore"),
        "prosody": pa.get("ProsodyScore"),
        "completeness": pa.get("CompletenessScore"),
        "pron_score": pa.get("PronScore"),
        # 用统一的 to_band()，不再用 round(PronScore/20)（那还有 Python 银行家舍入的坑，90 会被舍成 4）
        "score_0_5": to_band(pa.get("PronScore")),
        "band_source": "语音评测引擎（AI 预估，非官方考官分）",
        "weakest_words": weakest,
        "raw_text": best.get("Display"),
    }


# ---------------------------------------------------------------- xfyun

def provider_xfyun(payload: dict, media: Path, questions: Path | None,
                   lang: str = "en", core: str = "sent") -> dict:
    """讯飞 ISE（声通 suntone）：**念题部分**发音分。

    朗读型接口必须有参考文本，所以依赖 --questions（一行一题）。缺参数时明确 raise，
    由 provider 链降级并在 stderr 里说明原因 —— 绝不静默给一个 0 分。
    """
    if questions is None:
        raise RuntimeError("讯飞评测需要参考文本：请加 --questions questions/<页号>.txt（一行一题）。")
    if not media or not media.exists():
        raise RuntimeError(f"找不到原始音频：{media}")

    from kpf_analyze import split_by_questions
    from kpf_xfyun import assess, load_xfyun_cfg, segment_mp3_b64, worst_words

    xcfg = load_xfyun_cfg()  # 缺凭证会 raise RuntimeError，被 provider 链捕获后降级
    qs = [ln.strip() for ln in questions.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not qs:
        raise RuntimeError(f"题目文件里没有内容：{questions}")
    spans, warnings = split_by_questions(payload["words"], qs)

    per_q, all_words, errors = [], {}, []
    for qi, (qspan, _a, _d) in enumerate(spans, 1):
        if not qspan:
            per_q.append({"qi": qi, "ok": False, "error": "题目未定位"})
            continue
        start, end = qspan[0]["start"], qspan[-1]["end"]
        try:
            audio = segment_mp3_b64(media, start, end)
            res = assess(xcfg, audio, qs[qi - 1], lang=lang, core=core)
        except Exception as exc:
            errors.append(f"Q{qi}: {exc}")
            per_q.append({"qi": qi, "ok": False, "error": str(exc)[:160]})
            continue
        row = {"qi": qi, "ok": True}
        for key in ("overall", "pronunciation", "fluency", "integrity", "rhythm", "rear_tone", "speed"):
            row[key] = res.get(key)
        if row["overall"] is None:
            row["ok"] = False
            row["error"] = "接口未返回 overall 分数"
            errors.append(f"Q{qi}: 接口未返回 overall 分数")
        per_q.append(row)
        for word, score, read_type in worst_words(res, limit=99):
            if word and (word not in all_words or score < all_words[word][0]):
                all_words[word] = (score, read_type)

    scored = [q for q in per_q if q.get("ok") and q.get("overall") is not None]
    if not scored:
        raise RuntimeError("讯飞一句都没取得分数；" + ("；".join(errors) if errors else "接口无有效返回"))

    avg_overall = avg_of(scored, "overall")
    weakest = [(w, s, r) for w, (s, r) in sorted(all_words.items(), key=lambda kv: kv[1][0])[:10]]
    return {
        "provider": "xfyun",
        "score_0_5": to_band(avg_overall),
        "band_source": "语音评测引擎（讯飞 ISE，非官方考官分；只覆盖念题部分）",
        "overall": avg_overall,
        "pronunciation": avg_of(scored, "pronunciation"),
        "rhythm": avg_of(scored, "rhythm"),
        "fluency": avg_of(scored, "fluency"),
        "integrity": avg_of(scored, "integrity"),
        "scored": len(scored),
        "total": len(per_q),
        "per_q": per_q,
        "weakest_words": weakest,
        "align_warnings": warnings,
        "errors": errors,
        "note": "只评念题部分（朗读型接口必须有参考文本）；答题部分无参考文本，不评。",
    }


# ---------------------------------------------------------------- local / manual

def provider_local(payload: dict, crosscheck_md: Path | None = None) -> dict:
    tokens = [t for t in payload["words"] if t.get("p") is not None]
    if not tokens:
        raise RuntimeError("本引擎不返回词级置信度（groq），请用 --engine local 跑一份转写。")

    content = [t for t in tokens if norm(t["w"]) and norm(t["w"]) not in FUNCTION_WORDS]
    low = [t for t in content if t["p"] < LOW_P]

    windows = []
    for i in range(0, len(tokens), WINDOW_WORDS):
        chunk = [t for t in tokens[i:i + WINDOW_WORDS] if norm(t["w"]) and norm(t["w"]) not in FUNCTION_WORDS]
        if not chunk:
            continue
        dense = sum(1 for t in chunk if t["p"] < LOW_P)
        if dense >= 3:
            windows.append({"start": chunk[0]["start"], "low": dense, "size": len(chunk)})

    ratio = len(low) / len(content) if content else 0
    level = "高" if windows else ("中" if ratio >= 0.15 else "低")

    return {
        "provider": "local",
        "score_0_5": None,
        "band_source": None,
        "risk_level": level,
        "low_conf_ratio": round(ratio, 3),
        "low_conf_points": low[:40],
        "dense_windows": windows,
        "crosscheck_md": str(crosscheck_md) if crosscheck_md else None,
        "note": "本 provider 只定位疑点，不产出分数（置信度到 0–5 无验证映射）。",
    }


def provider_manual() -> dict:
    return {
        "provider": "manual",
        "score_0_5": None,
        "band_source": "教师判定",
        "note": "请听点位表后填分数，并把听出结论写进作业记录的「教师观察」。",
    }


# ---------------------------------------------------------------- 渲染

def render(payload: dict, result: dict) -> str:
    meta = payload["meta"]
    L = ["## 发音 · 评测结果", "",
         f"provider：`{result['provider']}`　音频：`{Path(meta.get('file', '')).name}`", ""]

    if result["provider"] == "azure":
        L += ["| 指标 | 分数（0–100） | 映射 0–5 |", "|---|---|---|",
              f"| 准确度 Accuracy | {fmt_score(result['accuracy'])} | |",
              f"| 流利度 Fluency | {fmt_score(result['fluency'])} | |",
              f"| 韵律 Prosody | {fmt_score(result.get('prosody'))} | |",
              f"| 完整度 Completeness | {fmt_score(result.get('completeness'))} | |",
              f"| **综合 PronScore** | **{fmt_score(result['pron_score'])}** | "
              f"**{fmt_band(result['score_0_5'])}** |", "",
              f"> {result['band_source']}。韵律分仅 en-US 可用。"]
        if result["score_0_5"] is None:
            L += ["", "> ⚠️ Azure 未返回 PronScore，本次**未取得发音分**（不是 0 分）。"
                      "请教师听点位后填，或改用讯飞 provider。"]
        L += ["", "### 发音最需要练的词（准确度最低 12 个）", "",
              "| 词 | 准确度 | 错误类型 |", "|---|---|---|"]
        L += [f"| {w['w']} | {fmt_score(w['accuracy'])} | {w.get('error_type', '—')} |"
              for w in result["weakest_words"]]
    elif result["provider"] == "xfyun":
        L += ["| 指标 | 平均分（0–100） | 教学映射 0–5 |", "|---|---|---|",
              f"| **总分 overall** | **{fmt_score(result['overall'])}** | "
              f"**{fmt_band(result['score_0_5'])}** |",
              f"| 发音 pronunciation | {fmt_score(result['pronunciation'])} | "
              f"{fmt_band(to_band(result['pronunciation']))} |",
              f"| 韵律 rhythm | {fmt_score(result['rhythm'])} | {fmt_band(to_band(result['rhythm']))} |",
              f"| 流利度 fluency | {fmt_score(result['fluency'])} | —（归话语组织，不重复扣） |",
              f"| 完整度 integrity | {fmt_score(result['integrity'])} | — |", "",
              f"> {result['band_source']}。{result['note']}",
              f"> 计分句数：{result['scored']} / {result['total']}"
              f"（0–5 是教学用映射：≥90→5、80–89→4、70–79→3、60–69→2、<60→1）。"]
        for w in result.get("align_warnings") or []:
            L.append(f"> ⚠️ 对齐提示：{w}")
        L += ["", "### 逐句得分", "",
              "| 题 | overall | 发音 | 韵律 | 句末语调 | 语速 |", "|---|---|---|---|---|---|"]
        for q in result["per_q"]:
            if q.get("ok") and q.get("overall") is not None:
                L.append(f"| Q{q['qi']} | {fmt_score(q.get('overall'))} | {fmt_score(q.get('pronunciation'))} "
                         f"| {fmt_score(q.get('rhythm'))} | {q.get('rear_tone') or '—'} | {q.get('speed') or '—'} |")
            else:
                L.append(f"| Q{q['qi']} | —（未取得） | —（未取得） | —（未取得） | — | — |")
        if result["weakest_words"]:
            L += ["", "### 最需要练的词（逐词发音分最低）", "", "| 词 | 发音分 | 读法 |", "|---|---|---|"]
            for word, score, read_type in result["weakest_words"]:
                tag = {0: "正常", 1: "前面有插入", 2: "漏读"}.get(read_type, "—")
                L.append(f"| {word} | {fmt_score(score)} | {tag} |")
        if result.get("errors"):
            L += ["", "> 未取得分数的句子（已排除出平均）："]
            L += [f"> - {e}" for e in result["errors"]]
    elif result["provider"] == "local":
        L += [f"**发音风险等级：{result['risk_level']}**（低置信词占比 {result['low_conf_ratio']:.1%}）",
              "", "> 注意：本 provider **不给分数**，只定位疑点。发音分请由教师听完点位后填，"
                  "或配好讯飞 / Azure 凭证后重跑 `--provider auto`。", ""]
        if result["low_conf_points"]:
            L += ["| 时间 | 转写词 | 置信度 |", "|---|---|---|"]
            L += [f"| {mmss(t['start'])} | {t['w']} | {t['p']} |" for t in result["low_conf_points"][:30]]
        if result["dense_windows"]:
            L += ["", "**整体含糊的段落**（每 10 词里 ≥3 个低置信）：", ""]
            L += [f"- {mmss(w['start'])} 起，{w['size']} 词里 {w['low']} 个低置信" for w in result["dense_windows"]]
    else:
        L += ["| 听点 | 时间 | 判定（清晰 / 含糊 / 音素错） |", "|---|---|---|",
              "| 1 | | |", "| 2 | | |", "| 3 | | |", "",
              "**发音分（0–5，教师填）**：　　　　", "",
              "> 给分抓手见 references/02-rubric.md：个别音（/θ/ /ð/、/v/ vs /w/、词尾 -s、-ed、多音节重音）、词级清晰度、语调。"]

    L += ["", "### 仍需人工补的点位（脚本无法判断）", "",
          "- [ ] 双引擎交叉验证：`kpf_asr.py crosscheck A.json B.json`",
          "- [ ] 个别音与语调（见上）",
          "- [ ] 念题部分的读音（自问自答作业里，念题也是有效发音证据）"]
    return "\n".join(L) + "\n"


# provider 链的唯一注册表：auto 模式按下面的**顺序**依次尝试，任一成功即用。
# 加新第三方评测（SpeechAce / SpeechSuper…）时，在此登记并补一个同构的 provider 函数。
PROVIDERS = {
    "xfyun": "讯飞语音评测 ISE（念题部分；需 --questions 参考文本）",
    "azure": "Azure 发音评估（unscripted，需 key/region）",
    "local": "本机 whisper 词级置信度（只出疑点，不出分）",
    "manual": "教师听点位后填分",
}


def dispatch(name: str, payload: dict, cfg: dict, media: Path, args) -> dict:
    if name == "xfyun":
        return provider_xfyun(payload, media, args.questions, lang=args.lang, core=args.core)
    if name == "azure":
        if not media or not media.exists():
            raise RuntimeError(f"找不到原始音频：{media}")
        return provider_azure(payload, cfg, media)
    if name == "local":
        return provider_local(payload, args.crosscheck)
    return provider_manual()


def main() -> None:
    ap = argparse.ArgumentParser(description="KPF 口语作业发音评分")
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--provider", choices=["auto", *PROVIDERS], default="auto")
    ap.add_argument("--media", type=Path, default=None, help="原始音视频；默认取转写 meta.file")
    ap.add_argument("--questions", type=Path, default=None,
                    help="标准题目 txt（一行一题）；讯飞 provider 必须给，缺了会明确降级")
    ap.add_argument("--lang", default="en", help="讯飞 provider 语种：cn/en/kr/fr/…")
    ap.add_argument("--core", default="sent", choices=["word", "sent", "para"], help="讯飞 provider 粒度")
    ap.add_argument("--crosscheck", type=Path, default=None, help="交叉验证报告 md（local 引擎用）")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    payload = json.loads(args.transcript.read_text(encoding="utf-8"))
    cfg = load_config()
    media = args.media or Path(payload["meta"].get("file", ""))
    order = [args.provider] if args.provider != "auto" else list(PROVIDERS)

    result, errors = None, []
    for name in order:
        try:
            result = dispatch(name, payload, cfg, media, args)
            break
        except Exception as exc:  # 逐个降级，流程不中断
            errors.append(f"{name}: {exc}")
            print(f"[降级] {name} 不可用 —— {exc}", file=sys.stderr)

    if result is None:
        sys.exit("所有 provider 都不可用：\n  " + "\n  ".join(errors))

    if errors:
        print("降级记录：\n  " + "\n  ".join(errors), file=sys.stderr)

    report = render(payload, result)
    out = args.out or args.transcript.with_name(args.transcript.stem + "-发音.md")
    out.write_text(report, encoding="utf-8")
    print(f"已写出 {out}（provider={result['provider']}）")
    if result.get("score_0_5") is not None:
        print(f"发音预估 {result['score_0_5']} / 5（来源：{result['band_source']}）")
    else:
        print(f"⚠️ 本次未取得发音分（provider={result['provider']}）"
              f"—— {result.get('note') or '请教师听点位后填分。'}")


if __name__ == "__main__":
    main()
