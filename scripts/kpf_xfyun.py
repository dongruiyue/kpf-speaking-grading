#!/usr/bin/env python3
"""KPF 口语作业 · 讯飞语音评测（ISE / 声通 suntone）发音评分

为什么只用它评「念题」部分：ISE 是**朗读型**评测，必须提供参考文本（refText）。
自问自答作业里，只有念题部分有标准原文（questions/*.txt），所以拿它当参考文本。
答题部分没有参考文本，理论上可以拿机器转写充当，但那测的是「清晰度/吻合度」而不是
「发音正确性」，参考价值有限，因此默认不评、也不出分。

接口要点（来自官方文档 https://www.xfyun.cn/doc/voiceservice/suntone/API.html）：
  - 端点：中英文评测 /v1/private/s8e098720；其他语种 /v1/private/sffc17cdb
  - 鉴权：HMAC-SHA256 签名拼到 wss URL（host/date/authorization）
  - 语种：parameter.st.lang = cn/en/kr/fr/de/ru/sp/jp
  - 音频：16k/8k、16bit、单声道、mp3 或 speex，base64 后 ≤10M
  - 句子模式返回：overall / pronunciation / fluency / integrity / rhythm / rear_tone
    / speed / words[]（逐词得分）/ phonemes[]（音素得分，需 phoneme_output=1）

用法：
  kpf_xfyun.py <转写.json> --questions questions/FCE-P1-P102-holidays.txt [--out 发音-ise.md]
  kpf_xfyun.py <转写.json> --questions q.txt --debug     # 打印原始响应，排错用
"""
from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import hmac
import io
import json
import sys
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent))
from kpf_analyze import split_by_questions  # 复用题目对齐（含容错）  # noqa: E402

CONFIG_PATH = Path.home() / ".kpf-speaking" / "config.json"
HOST = "cn-east-1.ws-api.xf-yun.com"
PATH_ZH_EN = "/v1/private/s8e098720"
CHUNK = 1280
MAX_WAIT = 45      # 单次评测的总时长上限（秒）；超时即用已拿到的结果，不让一条坏音频挂死整批
MAX_FRAMES = 500   # 单个连接最多接收的帧数。实测 8 秒音频就有 40–52 帧，设成 40 会永远收不到结果
                   # 且不报错（分数全为 None）；kpf_ise_stream.py 复用本常量，不要再写字面量。
# 教学用映射：把 ISE 的 0–100 分对到剑桥四项的 0–5。**不是官方换算表**，仅用于统一口径。
# 阈值是本 skill 唯一一份，与 references/02-rubric.md 第 5.3 节一致；其他脚本 import to_band()。
BANDS = [(90, 5), (80, 4), (70, 3), (60, 2), (0, 1)]


def load_xfyun_cfg() -> dict:
    """读讯飞凭证。缺字段时 raise（不是 sys.exit），好让 provider 链能降级。"""
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    x = cfg.get("xfyun") or {}
    missing = [k for k in ("appid", "api_key", "api_secret") if not x.get(k)]
    if missing:
        raise RuntimeError(f"{CONFIG_PATH} 里的 xfyun 缺少字段：{', '.join(missing)}")
    return {
        "appid": x["appid"],
        "api_key": x["api_key"],
        "api_secret": x["api_secret"],
        "host": x.get("host", HOST),
        "path": x.get("path") or PATH_ZH_EN,
    }


def build_url(host: str, path: str, api_key: str, api_secret: str) -> str:
    """按官方规则生成带签名的 wss 地址。"""
    date = email.utils.formatdate(usegmt=True)
    signature_origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), signature_origin.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")
    authorization_origin = (
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(authorization_origin.encode("utf-8")).decode("utf-8")
    return f"wss://{host}{path}?" + urlencode({"authorization": authorization, "date": date, "host": host})


def segment_mp3_b64(src: Path, start: float, end: float, rate: int = 16000) -> str:
    """截取 [start, end] 秒的音频，转成 16k 单声道 MP3 再 base64（接口只收 mp3/speex）。

    **不能用 seek**：实测对 .mov 容器 seek 会静默失效，每段都从 0 秒解起，
    片段变成 [0, end]（Q2 上传了 33 秒、Q5 上传了 121 秒），评分结果全错。
    改为从头解码 + 按采样数精确切片；140 秒音频解一遍只要 1 秒左右，完全可接受。
    """
    import av
    import numpy as np

    inp = av.open(str(src))
    try:
        stream = next(s for s in inp.streams if s.type == "audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        want_from, want_to = int(start * rate), int(end * rate)

        pieces, pos = [], 0
        for frame in inp.decode(stream):
            for f in resampler.resample(frame):
                n = f.samples
                lo, hi = max(0, want_from - pos), min(n, want_to - pos)
                if lo < hi:
                    pieces.append(f.to_ndarray()[:, lo:hi])
                pos += n
                if pos >= want_to:
                    break
            if pos >= want_to:
                break

        buf = io.BytesIO()
        out = av.open(buf, mode="w", format="mp3")
        ost = out.add_stream("libmp3lame", rate=rate)
        if pieces:
            pcm = np.ascontiguousarray(np.concatenate(pieces, axis=1))
            for i in range(0, pcm.shape[1], 1024):
                piece = av.AudioFrame.from_ndarray(
                    np.ascontiguousarray(pcm[:, i:i + 1024]), format="s16", layout="mono")
                piece.sample_rate = rate
                for pkt in ost.encode(piece):
                    out.mux(pkt)
        for pkt in ost.encode(None):
            out.mux(pkt)
        out.close()
        data = buf.getvalue()
    finally:
        inp.close()
    if not data:
        raise RuntimeError(f"音频片段为空（{start:.1f}–{end:.1f}s）")
    return base64.b64encode(data).decode("ascii")


def assess(cfg: dict, audio_b64: str, ref_text: str, lang: str = "en", core: str = "sent",
           phoneme: bool = True) -> dict:
    """调一次评测，返回解析后的 result 字典。"""
    from websockets.sync.client import connect

    url = build_url(cfg["host"], cfg["path"], cfg["api_key"], cfg["api_secret"])
    common = {
        "header": {"app_id": cfg["appid"], "status": 0},
        "parameter": {"st": {
            "lang": lang,
            "core": core,
            "refText": ref_text,
            # 必填！缺这个块服务端直接返回 10106 wrapper output data invalid(key or type)；
            # 且必须放在 st 里——放到 parameter 层会报 10163 unknown field。（两条都是实测踩出来的）
            "result": {"encoding": "utf8", "compress": "raw", "format": "json"},
            "scale": 100,
            "phoneme_output": 1 if phoneme else 0,
            "dict_type": "IPA88" if lang == "en" else None,
            "getParam": 0,
        }},
    }
    common["parameter"]["st"] = {k: v for k, v in common["parameter"]["st"].items() if v is not None}

    import time  # 仅此处需要，放在函数内避免影响启动速度

    chunks = [audio_b64[i:i + CHUNK] for i in range(0, len(audio_b64), CHUNK)]
    # 关键：讯飞流式接口要求首帧 status=0（开始）、末帧 status=2（结束），二者不能是同一帧。
    # 只有一帧时服务端会直接断连（实测 close 1000，无任何返回），所以强制拆成至少两帧。
    if len(chunks) < 2:
        mid = max(1, len(chunks[0]) // 2)
        chunks = [chunks[0][:mid], chunks[0][mid:]]

    results, errors, raw = [], [], []
    deadline = time.time() + MAX_WAIT
    with connect(url, open_timeout=20, close_timeout=5, max_size=None) as ws:
        for idx, chunk in enumerate(chunks):
            if time.time() > deadline:
                errors.append(f"总耗时超过 {MAX_WAIT}s，提前结束")
                break
            first, last = idx == 0, idx == len(chunks) - 1
            status = 2 if last else (0 if first else 1)
            frame = json.loads(json.dumps(common))  # 深拷贝，逐帧改 status
            frame["header"]["status"] = status
            frame["payload"] = {"data": {
                "encoding": "lame",
                "sample_rate": 16000,
                "channels": 1,
                "bit_depth": 16,
                "status": status,
                "seq": idx,
                "audio": chunk,
            }}
            ws.send(json.dumps(frame))
            print(f"[ise] 发送第 {idx + 1}/{len(chunks)} 帧（status={status}）", file=sys.stderr, flush=True)

        # 关键：讯飞是"发完再收"的流式模型——逐帧等响应会一路等到超时（实测第 2 帧起服务端就不回了，
        # 只在最后统一返回结果）。所以上面的 for 只负责发送，结果统一在这里收。
        print(f"[ise] 已发送 {len(chunks)} 帧，开始接收结果", file=sys.stderr, flush=True)
        while len(raw) < MAX_FRAMES:
            budget = deadline - time.time()
            if budget <= 0:
                break
            try:
                resp = json.loads(ws.recv(timeout=min(20.0, budget)))
            except Exception as exc:
                if not results:
                    errors.append(f"接收结果失败：{exc}")
                break
            raw.append(resp)
            head = resp.get("header") or {}
            print(f"[ise] 收到第 {len(raw)} 帧 code={head.get('code')} status={head.get('status')}",
                  file=sys.stderr, flush=True)
            if head.get("code") not in (0, None):
                errors.append(f"header.code={head.get('code')} message={head.get('message')}")
                break
            payload = resp.get("payload") or {}
            if payload.get("result"):
                results.append(payload["result"])
                text = payload["result"].get("text")
                eof = None
                if text:
                    try:
                        eof = json.loads(base64.b64decode(text).decode("utf-8")).get("eof")
                    except Exception:
                        eof = None
                if eof == 1:
                    break

    if errors and not results:
        detail = json.dumps(raw, ensure_ascii=False)[:600] if raw else "（服务端未返回任何内容）"
        raise RuntimeError("；".join(errors) + f"｜原始返回：{detail}")

    merged: dict = {"raw_frames": len(results), "errors": errors}
    for res in results:
        text = res.get("text")
        if not text:
            continue
        try:
            body = json.loads(base64.b64decode(text).decode("utf-8"))
        except Exception:
            continue
        if not isinstance(body, dict):
            continue
        # 分数在 body["result"] 里，不在顶层——顶层是 recordId / refText / eof / params 等元信息（实测确认）
        scores = body.get("result")
        if isinstance(scores, dict):
            merged.update(scores)
        elif body.get("overall") is not None:
            merged.update(body)  # 兼容其他返回形态
        for key in ("eof", "refText", "recordId"):
            if key in body:
                merged[key] = body[key]
    return merged


def to_band(score) -> int | None:
    """引擎原始分（0–100）→ 教学 0–5。**本 skill 唯一实现，其他脚本 import 它。**

    阈值见 BANDS（≥90→5、80–89→4、70–79→3、60–69→2、<60→1），与
    references/02-rubric.md 第 5.3 节一致；不是官方换算表。
    分值为 None（引擎未取得）时返回 None，调用方必须显示"未取得"，不得当 0 分。
    """
    if score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return None
    for floor, band in BANDS:
        if s >= floor:
            return band
    return 1


def avg_of(rows: list[dict], key: str):
    """各维度平均分；一个有效值都没有时返回 None（而不是 0.0）。"""
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def fmt_score(v) -> str:
    return "—（未取得）" if v is None else str(v)


def fmt_band(band) -> str:
    return "—（未取得）" if band is None else f"{band} / 5"


def worst_words(result: dict, limit: int = 10) -> list[tuple]:
    rows = []
    words = result.get("words") or []
    if isinstance(words, dict):
        words = [{"word": k, **(v if isinstance(v, dict) else {})} for k, v in words.items()]
    for w in words:
        scores = w.get("scores") or {}
        val = scores.get("pronunciation") if isinstance(scores, dict) else None
        val = val if val is not None else w.get("pronunciation")
        if val is None:
            continue
        rows.append((w.get("word") or w.get("charType"), val, w.get("readType")))
    return sorted(rows, key=lambda x: x[1])[:limit]


def render(payload: dict, per_q: list[dict], result_all: dict, debug: bool) -> str:
    meta = payload["meta"]
    L = ["## 发音 · 讯飞语音评测（ISE）结果", "",
         f"评测范围：**念题部分**（共 {len(per_q)} 句）。答题部分无参考文本，本接口无法评，另见抽听点位表。",
         f"音频：`{Path(meta.get('file', '')).name}`", ""]

    ok = [q for q in per_q if q.get("ok") and q.get("overall") is not None]
    avg = {k: avg_of(ok, k) for k in ("overall", "pronunciation", "fluency", "integrity", "rhythm")}
    L += ["| 指标 | 平均分（0–100） | 教学映射 0–5 |", "|---|---|---|",
          f"| **总分 overall** | **{fmt_score(avg['overall'])}** | **{fmt_band(to_band(avg['overall']))}** |",
          f"| 发音 pronunciation | {fmt_score(avg['pronunciation'])} | {fmt_band(to_band(avg['pronunciation']))} |",
          f"| 韵律 rhythm | {fmt_score(avg['rhythm'])} | {fmt_band(to_band(avg['rhythm']))} |",
          f"| 流利度 fluency | {fmt_score(avg['fluency'])} | —（归话语组织，不重复扣） |",
          f"| 完整度 integrity | {fmt_score(avg['integrity'])} | — |", "",
          "> 0–5 是**教学用映射**（≥90→5、80–89→4、70–79→3、60–69→2、<60→1），不是官方换算表。",
          "> 流利度归剑桥的「话语组织」维度，此处只作参考，不要与发音重复扣分。"]
    if not ok:
        L += ["> ⚠️ **本次一句都没有取得分数**，上表全部为「—（未取得）」，不可用于给分。", ""]
    L += ["### 逐句得分", "", "| 题 | overall | 发音 | 韵律 | 句末语调 | 语速 |", "|---|---|---|---|---|---|"]
    for q in per_q:
        if q.get("ok") and q.get("overall") is not None:
            L.append(f"| Q{q['qi']} | {fmt_score(q.get('overall'))} | {fmt_score(q.get('pronunciation'))} "
                     f"| {fmt_score(q.get('rhythm'))} | {q.get('rear_tone') or '—'} | {q.get('speed') or '—'} |")
        else:
            L.append(f"| Q{q['qi']} | —（未取得） | —（未取得） | —（未取得） | — | — |")
    L.append("")

    weak = result_all.get("weakest") or []
    if weak:
        L += ["### 最需要练的词（逐词发音分最低）", "", "| 词 | 发音分 | 读法 |", "|---|---|---|"]
        for word, score, read_type in weak:
            tag = {0: "正常", 1: "前面有插入", 2: "漏读"}.get(read_type, "—")
            L.append(f"| {word} | {score} | {tag} |")
        L.append("")

    if result_all.get("errors"):
        L += ["### 接口返回的告警（不影响已得分数）", ""]
        L += [f"- {e}" for e in result_all["errors"]]
        L.append("")

    if debug:
        L += ["### 原始响应（debug）", "", "```json",
              json.dumps(result_all.get("raw", {}), ensure_ascii=False, indent=1)[:4000], "```", ""]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="讯飞语音评测（ISE）· 念题部分发音评分")
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--questions", type=Path, required=True, help="标准题目 txt（一行一题）")
    ap.add_argument("--lang", default="en", help="cn/en/kr/fr/de/ru/sp/jp")
    ap.add_argument("--core", default="sent", choices=["word", "sent", "para"])
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    try:
        cfg = load_xfyun_cfg()
    except RuntimeError as exc:
        sys.exit(str(exc))
    payload = json.loads(args.transcript.read_text(encoding="utf-8"))
    media = Path(payload["meta"].get("file", ""))
    if not media.exists():
        sys.exit(f"找不到原始音频：{media}")

    questions = [ln.strip() for ln in args.questions.read_text(encoding="utf-8").splitlines() if ln.strip()]
    spans, warnings = split_by_questions(payload["words"], questions)
    for w in warnings:
        print(f"[对齐提示] {w}", file=sys.stderr)

    per_q, all_words, errors = [], {}, []
    for qi, (qspan, _aspan, _diff) in enumerate(spans, 1):
        if not qspan:
            per_q.append({"qi": qi, "ok": False})
            continue
        start, end = qspan[0]["start"], qspan[-1]["end"]
        ref = questions[qi - 1]
        print(f"[ISE] Q{qi} {start:.1f}–{end:.1f}s  refText={ref[:50]}…", flush=True)
        try:
            audio = segment_mp3_b64(media, start, end)
            res = assess(cfg, audio, ref, lang=args.lang, core=args.core)
        except Exception as exc:
            print(f"[ISE] Q{qi} 失败：{exc}", file=sys.stderr)
            errors.append(f"Q{qi}: {exc}")
            per_q.append({"qi": qi, "ok": False})
            continue
        if args.debug:
            print(f"[ISE] Q{qi} 原始返回：{json.dumps(res, ensure_ascii=False)[:1200]}", flush=True)
        row = {"qi": qi, "ok": True}
        for key in ("overall", "pronunciation", "fluency", "integrity", "rhythm", "rear_tone", "speed"):
            row[key] = res.get(key)
        if row["overall"] is None:
            # 接口回了帧但没带分数：必须当失败处理，否则会渲染出"总分 0.0 → 1/5"这种假分
            row["ok"] = False
            msg = f"Q{qi}: 接口返回了结果帧但没有 overall 分数，按未取得处理"
            errors.append(msg)
            print(f"[ISE] {msg}", file=sys.stderr)
            per_q.append(row)
            continue
        per_q.append(row)
        for word, score, read_type in worst_words(res, limit=99):
            if word not in all_words or score < all_words[word][0]:
                all_words[word] = (score, read_type)

    weakest = [(w, s, r) for w, (s, r) in sorted(all_words.items(), key=lambda kv: kv[1][0])[:10]]
    merged = {"weakest": weakest, "errors": errors}
    if args.debug:
        merged["raw"] = {"per_q_scores": per_q}

    report = render(payload, per_q, merged, args.debug)
    out = args.out or args.transcript.with_name(args.transcript.stem + "-发音-ise.md")
    out.write_text(report, encoding="utf-8")
    print(f"\n已写出 {out}")
    scored = [q for q in per_q if q.get("ok") and q.get("overall") is not None]
    if not scored:
        print("❌ 没有一句取得分数（报告里各维度均为「—（未取得）」）。"
              "请检查音频、题目对齐与接口返回；本次结果不可用于给分。", file=sys.stderr)
        if errors:
            for e in errors:
                print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    avg = avg_of(scored, "overall")
    print(f"念题部分平均总分 {avg:.1f}/100 → 教学映射 {to_band(avg)}/5（{len(scored)}/{len(per_q)} 句成功）")
    if len(scored) < len(per_q):
        failed = [f"Q{q['qi']}" for q in per_q if not q.get("ok")]
        print(f"⚠️ 未取得分数的句子：{', '.join(failed)} —— 平均值已排除这些句子", file=sys.stderr)


if __name__ == "__main__":
    main()
