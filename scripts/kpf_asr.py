#!/usr/bin/env python3
"""KPF 口语作业 · 识别与转写（多引擎，统一输出 schema）

引擎：
  local   faster-whisper，免费无限，含词级置信度（默认）
  groq    Groq 免费层，秒级出稿，音频需上传（需 key）
  xftj    讯飞听见，复用既有 skill，消耗额度

统一 schema（供 kpf_analyze.py 消费）：
{
  "meta": {"engine","model","file","student","class","level","duration","transcribed_at"},
  "words": [{"w": str, "start": float, "end": float, "p": float|null}],
  "segments": [{"start": float, "end": float, "text": str}],
  "text": str
}

用法：
  kpf_asr.py transcribe <文件或文件夹> [--engine local] [--student X --class Y --level FCE] [--out DIR]
  kpf_asr.py crosscheck <a.json> <b.json>
  kpf_asr.py --download-model large-v3-turbo
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kpf_analyze import mmss  # 时间戳唯一实现（含 59.97 → 01:00.0 的进位修正）  # noqa: E402

CONFIG_PATH = Path.home() / ".kpf-speaking" / "config.json"
# 讯飞听见（xftj）走的是另一个 skill，本仓库不含它——所以位置允许被指路：
# 环境变量 KPF_XFTJ_DIR > config 的 xftj.dir > 下面这个默认位置。
DEFAULT_XFTJ_DIR = Path.home() / "Documents" / "skills" / "xftj-transcribe" / "scripts"
MEDIA_EXT = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".mp4", ".mov", ".m4v", ".caf", ".aiff"}
GROQ_LIMIT_MB = 25


def _prepare_network() -> None:
    """处理网络环境（这两件事不做，模型下载会直接失败）。

    1. macOS 系统代理常指向未启动的代理程序，requests / huggingface_hub 会连不上。
       默认绕过；确实需要走代理（例如访问 Groq）时用 KPF_USE_PROXY=1。
    2. 国内直连 huggingface.co 常超时，默认走镜像；有科学上网时用 KPF_HF_ENDPOINT= 置空。

    只在 main() 里调用，不要在 import 时执行——那会让"import kpf_asr"顺带改掉全局 env。
    """
    if os.environ.get("KPF_USE_PROXY") != "1":
        os.environ.setdefault("NO_PROXY", "*")
        os.environ.setdefault("no_proxy", "*")
    if os.environ.get("KPF_HF_ENDPOINT") is None:
        os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"


# ---------------------------------------------------------------- 基础工具

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            sys.exit(f"配置文件不是合法 JSON：{CONFIG_PATH}\n{exc}")
    return {}


def norm(text: str) -> str:
    """归一化：去标点、转小写，用于跨引擎比对。"""
    return re.sub(r"[^a-z0-9']", "", str(text).lower())


def collect_media(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    files = sorted(p for p in target.rglob("*") if p.suffix.lower() in MEDIA_EXT)
    if not files:
        sys.exit(f"目录里没有找到音视频文件：{target}")
    return files


def wrap_schema(meta: dict, words: list[dict], segments: list[dict]) -> dict:
    return {
        "meta": meta,
        "words": words,
        "segments": segments,
        "text": " ".join(s["text"] for s in segments).strip(),
    }


def save(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(payload["words"])
    print(f"已写出 {path}（{n} 词，{payload['meta'].get('duration', 0):.1f}s）")


# ---------------------------------------------------------------- local 引擎

def engine_local(src: Path, model: str, language: str = "en") -> dict:
    from faster_whisper import WhisperModel

    print(f"[local] 加载模型 {model}（首次使用会下载）…", flush=True)
    asr = WhisperModel(model, device="cpu", compute_type="int8")
    print(f"[local] 转写 {src.name} …", flush=True)
    seg_iter, info = asr.transcribe(
        str(src),
        language=language,
        word_timestamps=True,
        temperature=0.0,
        condition_on_previous_text=False,  # 抑制幻觉循环，避免"顺"出没说过的话
        vad_filter=False,
        beam_size=5,
    )

    words, segments = [], []
    for seg in seg_iter:
        segments.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip()})
        for w in (seg.words or []):
            words.append({
                "w": w.word.strip(),
                "start": round(w.start, 3),
                "end": round(w.end, 3),
                "p": round(w.probability, 4) if w.probability is not None else None,
            })
        print(f"  …{seg.end:6.1f}s", end="\r", flush=True)
    print(" " * 20, end="\r")

    meta = {"engine": "local", "model": model, "duration": round(info.duration or 0, 2)}
    return wrap_schema(meta, words, segments)


# ---------------------------------------------------------------- groq 引擎

def engine_groq(src: Path, cfg: dict, model: str = "whisper-large-v3-turbo", language: str = "en") -> dict:
    import requests

    key = (cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY") or "").strip()
    if not key:
        sys.exit(
            "缺少 Groq API key。\n"
            f"在 {CONFIG_PATH} 写入 {{\"groq_api_key\": \"gsk_...\"}}，或设置环境变量 GROQ_API_KEY。\n"
            "免费申请：https://console.groq.com/keys"
        )
    size_mb = src.stat().st_size / 1024 / 1024
    if size_mb > GROQ_LIMIT_MB:
        sys.exit(f"文件 {size_mb:.1f}MB 超过 Groq 免费层 {GROQ_LIMIT_MB}MB 上限，请改用 local 引擎。")

    print(f"[groq] 上传并转写 {src.name}（{size_mb:.2f}MB）…", flush=True)
    with src.open("rb") as fh:
        resp = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (src.name, fh)},
            data={
                "model": model,
                "language": language,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
                "temperature": "0",
            },
            timeout=600,
        )
    if resp.status_code != 200:
        sys.exit(f"Groq 返回 {resp.status_code}：{resp.text[:400]}")
    data = resp.json()

    words = [{"w": w.get("word", "").strip(), "start": round(w.get("start", 0), 3),
              "end": round(w.get("end", 0), 3), "p": None}
             for w in data.get("words", [])]
    segments = [{"start": round(s.get("start", 0), 3), "end": round(s.get("end", 0), 3),
                 "text": (s.get("text") or "").strip()}
                for s in data.get("segments", [])]
    if not segments and data.get("text"):
        segments = [{"start": 0.0, "end": 0.0, "text": data["text"].strip()}]
    duration = segments[-1]["end"] if segments else 0.0

    meta = {"engine": "groq", "model": model, "duration": round(duration, 2), "word_probability": False}
    return wrap_schema(meta, words, segments)


# ---------------------------------------------------------------- 讯飞引擎

def resolve_xftj_dir() -> Path:
    """讯飞听见转发件的位置：环境变量 KPF_XFTJ_DIR > config 的 xftj.dir > 默认位置。

    那个 skill 不在本仓库里，位置必须能被指路——否则换台机器就得改源码。
    """
    env = os.environ.get("KPF_XFTJ_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
        custom = str((cfg.get("xftj") or {}).get("dir", "") or "").strip()
    except Exception:  # noqa: BLE001 - 配置文件坏了不该连累其它引擎
        custom = ""
    return Path(custom).expanduser() if custom else DEFAULT_XFTJ_DIR


def engine_xftj(src: Path, allow_pay: bool, language: str = "en") -> dict:
    xftj_dir = resolve_xftj_dir()
    if not xftj_dir.is_dir():
        sys.exit(f"找不到讯飞听见的转发件：{xftj_dir}\n"
                 f"  它不在本仓库里（本仓库不含那个 skill）。装好后用任一方式指路：\n"
                 f"    export KPF_XFTJ_DIR=/path/to/xftj-transcribe/scripts\n"
                 f"    或在 {CONFIG_PATH} 里写 {{\"xftj\": {{\"dir\": \"...\"}}}}\n"
                 f"  只想转写的话用默认的 --engine local（免费，音频不出本机）。")
    sys.path.insert(0, str(xftj_dir))
    try:
        from xftj_api import XFTJClient
        from xftj_common import load_config as xftj_cfg, get_base_url
    except ImportError as exc:
        sys.exit(f"导入讯飞模块失败：{exc}")

    cfg = xftj_cfg()
    client = XFTJClient(cfg["apiKey"], get_base_url(cfg))

    print(f"[xftj] 上传 {src.name} …", flush=True)
    link = client.get_upload_link(src.name, src.stat().st_size)
    client.upload_file(str(src), link["uploadLink"])
    biz = client.start_transcription(link["uploadFileId"], language=language)
    info = client.poll_transcription_status(biz["processId"], max_wait=900)
    hj_id = info["hjId"]

    benefits = client.query_benefits(hj_id)
    quotas = []
    for q in benefits.get("permissions", []):
        quotas.append((q.get("userRoleId"), "permission", q.get("roleName"), q.get("remainQuantity")))
    for q in benefits.get("personalQuotas", []):
        quotas.append((q.get("id"), "personalQuota", q.get("name"), q.get("remainQuantity")))
    if not quotas:
        sys.exit("讯飞账号没有可用权益，无法解锁转写结果。请改用 local 引擎。")
    if not allow_pay:
        listing = "\n".join(f"  - {t}｜{n}｜剩余 {r}ms" for _, t, n, r in quotas)
        sys.exit(
            "讯飞引擎会消耗账户权益，需要显式确认。\n可用权益：\n"
            f"{listing}\n确认后重跑并加 --yes-pay（默认建议改用免费的 local 引擎）。"
        )

    quota_id, quota_type, name, _ = quotas[0]
    print(f"[xftj] 使用权益：{name}", flush=True)
    client.pay_with_quota(hj_id, quota_id, quota_type)
    origin_id, file_source = client.get_origin_audio_id(hj_id)
    raw = client.get_transcript_results(hj_id, origin_id, result_type=16, file_source=file_source)
    payload = json.loads(raw) if isinstance(raw, str) else raw

    words, segments = [], []
    for para in payload.get("ps", []):
        buf = []
        for w in para.get("words", []):
            words.append({"w": w["text"].strip(), "start": round(w["time"][0] / 1000, 3),
                          "end": round(w["time"][1] / 1000, 3), "p": None})
            buf.append(w["text"].strip())
        if buf:
            segments.append({
                "start": round(para["words"][0]["time"][0] / 1000, 3),
                "end": round(para["words"][-1]["time"][1] / 1000, 3),
                "text": " ".join(buf),
            })
    duration = words[-1]["end"] if words else 0.0
    meta = {"engine": "xftj", "model": "iflyrec-type16", "duration": duration,
            "word_probability": False, "hj_id": hj_id}
    return wrap_schema(meta, words, segments)


# ---------------------------------------------------------------- 交叉验证

def crosscheck(a_path: Path, b_path: Path, window: float = 1.5, low_p: float = 0.5) -> str:
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))

    b_words = b["words"]
    b_index: dict[int, list[dict]] = {}
    for w in b_words:
        b_index.setdefault(int(w["start"] // window), []).append(w)
        b_index.setdefault(int(w["start"] // window) - 1, []).append(w)
        b_index.setdefault(int(w["start"] // window) + 1, []).append(w)

    def low_conf(word: dict) -> bool:
        return word.get("p") is not None and word["p"] < low_p

    high, low = [], []
    for w in a["words"]:
        if not norm(w["w"]):
            continue
        t0 = w["start"] - window
        t1 = w["end"] + window
        neighbours = [x for x in b_index.get(int(w["start"] // window), []) if t0 <= x["start"] <= t1]
        if not neighbours:
            continue
        b_text = {norm(x["w"]) for x in neighbours}
        agree = norm(w["w"]) in b_text
        # 任一方置信度低即可判定：讯飞 / Groq 不返回词级 p，只看一方会永远判不出高置信疑点
        weak = low_conf(w) or any(low_conf(x) for x in neighbours)
        shown = " / ".join(x["w"] for x in neighbours[:6]) + ("…" if len(neighbours) > 6 else "")
        when = w["start"]  # 显示该词自身时间，不要用 t0（会显示成负数）
        if not agree and weak:
            high.append((when, w["w"], shown, w.get("p"), "两引擎不一致 + 词级置信度低"))
        elif not agree:
            low.append((when, w["w"], shown, w.get("p"), "两引擎不一致"))
        elif weak:
            low.append((when, w["w"], shown, w.get("p"), "词级置信度低（两引擎一致）"))

    engine_a, engine_b = a["meta"]["engine"], b["meta"]["engine"]
    lines = [f"# 交叉验证 · {a_path.name} × {b_path.name}", "",
             f"引擎：`{engine_a}` × `{engine_b}`　时间窗 ±{window}s　低置信阈值 p<{low_p}", ""]
    if b["meta"].get("word_probability") is False:
        lines += [f"> 注意：`{engine_b}` 不返回词级置信度，判定主要依据两引擎文字差异。", ""]
    lines += [f"## 高置信疑点（优先听）· {len(high)} 处", "",
              "| 时间 | 引擎A | 引擎B | p | 判定 |", "|---|---|---|---|---|"]
    lines += [f"| {mmss(t)} | {x} | {y} | {p if p is not None else '—'} | {r} |"
              for t, x, y, p, r in high] or ["| — | — | — | — | 无 |"]
    lines += ["", f"## 低置信疑点 · {len(low)} 处", "",
              "| 时间 | 引擎A | 引擎B | p | 判定 |", "|---|---|---|---|---|"]
    lines += [f"| {mmss(t)} | {x} | {y} | {p if p is not None else '—'} | {r} |"
              for t, x, y, p, r in low[:60]] or ["| — | — | — | — | 无 |"]
    lines += ["", "> 高置信疑点才可写入抽听点位表并注明「两引擎一致」；低置信疑点仅作排序参考（见 references/03-scoring-rules.md）。"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

def main() -> None:
    _prepare_network()
    ap = argparse.ArgumentParser(description="KPF 口语作业识别与转写")
    ap.add_argument("--download-model", metavar="NAME", help="预下载并加载指定 Whisper 模型后退出")
    sub = ap.add_subparsers(dest="cmd")

    t = sub.add_parser("transcribe", help="转写一个文件或整个文件夹")
    t.add_argument("target", type=Path)
    t.add_argument("--engine", choices=["local", "groq", "xftj"], default="local")
    t.add_argument("--model", default="large-v3-turbo")
    t.add_argument("--student", default="")
    t.add_argument("--class", dest="klass", default="")
    t.add_argument("--level", default="")
    t.add_argument("--out", type=Path, default=Path("work"))
    t.add_argument("--language", default="en")
    t.add_argument("--yes-pay", action="store_true", help="讯飞引擎：确认消耗账户权益")

    c = sub.add_parser("crosscheck", help="两份转写结果交叉验证")
    c.add_argument("a", type=Path)
    c.add_argument("b", type=Path)
    c.add_argument("--window", type=float, default=1.5)
    c.add_argument("--out", type=Path, default=None)

    args = ap.parse_args()

    if args.download_model:
        from faster_whisper import WhisperModel
        print(f"下载并加载 {args.download_model} …")
        WhisperModel(args.download_model, device="cpu", compute_type="int8")
        print("模型就绪。")
        return

    if args.cmd == "crosscheck":
        report = crosscheck(args.a, args.b, args.window)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report, encoding="utf-8")
            print(f"已写出 {args.out}")
        else:
            print(report)
        return

    if args.cmd != "transcribe":
        ap.print_help()
        return

    cfg = load_config()
    files = collect_media(args.target)
    print(f"共 {len(files)} 个文件，引擎 = {args.engine}")

    for path in files:
        if args.engine == "local":
            payload = engine_local(path, args.model, args.language)
        elif args.engine == "groq":
            payload = engine_groq(path, cfg, args.model if args.model != "large-v3-turbo" else "whisper-large-v3-turbo",
                                  args.language)
        else:
            payload = engine_xftj(path, args.yes_pay, args.language)

        payload["meta"].update({
            "file": str(path),
            "student": args.student,
            "class": args.klass,
            "level": args.level,
            "transcribed_at": dt.datetime.now().isoformat(timespec="seconds"),
        })
        out = args.out / f"{path.stem}--{payload['meta']['engine']}.json"
        save(payload, out)


if __name__ == "__main__":
    main()
