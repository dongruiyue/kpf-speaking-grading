#!/usr/bin/env python3
"""KPF 口语作业 · 语音评测（流式版）发音评分 —— 讯飞自研 ISE

与 suntone 的区别（详见 references/06-verification.md）：
  - 讯飞自研引擎，评分标准公开：准确度 50% / 流畅度 30% / 标准度 20%（标准度含"无中式口音"）
  - 独有 `is_rejected` 乱读拒识 + `except_info` 音频质量诊断（无语音/音量小/信噪比低/截幅）
  - 缺点：不能限定英式/美式；语调重音类字段官方标注"效果优化中"；只返回 XML
所以定位是**质检引擎**：主引擎用 suntone 出分，分数异常时用本脚本复核"是不是乱读/音频有没有问题"。

协议（官方文档 https://www.xfyun.cn/doc/Ise/IseAPI.html）：
  1. ssb 参数帧（cmd=ssb, data.status=0）
  2. 音频帧（cmd=auw，aus=1 首 / 2 中 / 4 末；data.status 0→1→2）
  3. 服务端逐帧返回，data.status=2 时 data.data 为 base64(XML)，即最终结果

用法：
  kpf_ise_stream.py <转写.json> --questions questions/FCE-P1-P102-holidays.txt [--out 发音-ise流式版.md]
  kpf_ise_stream.py <转写.json> --questions q.txt --debug
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kpf_analyze import split_by_questions  # noqa: E402
from kpf_xfyun import (  # noqa: E402
    MAX_FRAMES, MAX_WAIT, avg_of, build_url, fmt_band, fmt_score, load_xfyun_cfg,
    segment_mp3_b64, to_band,
)

ISE_HOST = "ise-api.xfyun.cn"
ISE_PATH = "/v2/open-ise"
CHUNK = 1280
FRAME_GAP = 0.02  # 帧间 20ms，避免把服务端灌爆
EXCEPT_INFO = {
    0x7001: "无语音或音量过小",
    0x7004: "乱说",
    0x7008: "信噪比低（环境噪音大）",
    0x7012: "录音截幅（音量爆表）",
    0x7011: "没有音频输入",
}


def is_rejected(q: dict) -> bool:
    return str(q.get("is_rejected")).lower() == "true"


def parse_xml_result(xml_text: str) -> dict:
    """把评测 XML 归一到扁平字典。结构：read_sentence/read_chapter → sentence → word → syll → phone"""
    out: dict = {"words": []}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {"parse_error": str(exc), "raw_xml": xml_text[:800]}

    # 分数**不在** read_sentence 上（那里只有 lan/type/version），而在内层的
    # rec_paper → read_chapter 上。所以按"谁带 total_score"来定位，不要按标签名猜。
    top = None
    for tag in ("read_chapter", "read_sentence", "rec_paper", "sentence", "read_word"):
        for el in root.iter(tag):
            if el.get("total_score") is not None:
                top = el
                break
        if top is not None:
            break
    if top is None:
        top = root

    for key, val in top.attrib.items():
        try:
            out[key] = float(val)
        except ValueError:
            out[key] = val
    # 部分字段名带 _score，统一暴露成短名方便比对
    for src, dst in (("accuracy_score", "accuracy"), ("fluency_score", "fluency"),
                     ("standard_score", "standard"), ("integrity_score", "integrity"),
                     ("total_score", "overall"), ("phone_score", "phone")):
        if src in out:
            out[dst] = out[src]

    if "except_info" in out:
        try:
            code = int(float(out["except_info"]))
            out["except_info_msg"] = "正常" if code == 0 else EXCEPT_INFO.get(code, f"未知({code})")
        except (TypeError, ValueError):
            pass

    for word in top.iter("word"):
        score = word.get("total_score") or word.get("score")
        row = {"word": word.get("content", ""), "score": None, "dp_message": int(word.get("dp_message", 0) or 0)}
        if score is not None:
            try:
                row["score"] = float(score)
            except ValueError:
                pass
        if row["word"]:
            out["words"].append(row)
    return out


def assess_ise(cfg: dict, audio_b64: str, ref_text: str, category: str = "read_sentence") -> dict:
    """调一次流式版评测，返回归一化结果。"""
    from websockets.sync.client import connect

    url = build_url(ISE_HOST, ISE_PATH, cfg["api_key"], cfg["api_secret"])
    business_common = {
        "sub": "ise",
        "ent": "en_vip",
        "category": category,
        "tte": "utf-8",
        "ttp_skip": True,
        "aue": "lame",
        "auf": "audio/L16;rate=16000",
        "rstcd": "utf8",
        "rst": "entirety",
        "ise_unite": "1",
        "extra_ability": "multi_dimension;syll_phone_err_msg",
    }
    chunks = [audio_b64[i:i + CHUNK] for i in range(0, len(audio_b64), CHUNK)]
    if len(chunks) < 2:  # 首帧与末帧不能同一帧
        mid = max(1, len(chunks[0]) // 2)
        chunks = [chunks[0][:mid], chunks[0][mid:]]

    deadlines = time.time() + MAX_WAIT
    raw, errors, xml_text = [], [], ""

    with connect(url, open_timeout=20, close_timeout=5, max_size=None) as ws:
        # 1) 参数帧
        ws.send(json.dumps({
            "common": {"app_id": cfg["appid"]},
            "business": {**business_common, "cmd": "ssb", "text": "\ufeff" + ref_text},
            "data": {"status": 0},
        }))
        # 2) 音频帧
        for idx, chunk in enumerate(chunks):
            aus = 1 if idx == 0 else (4 if idx == len(chunks) - 1 else 2)
            data_status = 0 if idx == 0 else (2 if idx == len(chunks) - 1 else 1)
            ws.send(json.dumps({
                "business": {**business_common, "cmd": "auw", "aus": aus},
                "data": {"status": data_status, "data": chunk},
            }))
            time.sleep(FRAME_GAP)
        print(f"[ise-flow] 已发送 ssb + {len(chunks)} 个音频帧，开始接收", file=sys.stderr, flush=True)

        # 3) 收结果
        # 服务端会为每个音频帧回一个 status=1 的确认（data 为 null），最后才给 status=2 的结果，
        # 所以帧数上限必须显著大于音频帧数（实测 8 秒音频约 40–52 帧，用 MAX_FRAMES 常量统一）。
        while len(raw) < MAX_FRAMES and time.time() < deadlines:
            left = deadlines - time.time()
            if left <= 0:
                break
            try:
                resp = json.loads(ws.recv(timeout=min(20.0, left)))
            except Exception as exc:
                if not xml_text:
                    errors.append(f"接收失败：{exc}")
                break
            raw.append(resp)
            code = resp.get("code")
            if code not in (0, None):
                errors.append(f"code={code} message={resp.get('message')}")
                break
            body = resp.get("data") or {}
            blob = body.get("data")
            status = body.get("status")
            if blob:
                try:
                    xml_text = base64.b64decode(blob).decode("utf-8", "replace")
                except Exception as exc:
                    errors.append(f"XML 解码失败：{exc}")
            if status == 2 and xml_text:
                break

    if not xml_text:
        detail = json.dumps(raw, ensure_ascii=False)[:600] if raw else "（无返回）"
        raise RuntimeError("；".join(errors) + f"｜原始返回：{detail}")

    parsed = parse_xml_result(xml_text)
    parsed["errors"] = errors
    return parsed


def render(payload: dict, per_q: list[dict], debug: bool) -> str:
    meta = payload["meta"]
    scored = [q for q in per_q if q.get("ok") and q.get("overall") is not None]
    # 被拒识（乱读）的句子，官方明确说"分值不能作为参考"，平均前必须剔除
    valid = [q for q in scored if not is_rejected(q)]
    rejected = [q for q in scored if is_rejected(q)]
    L = ["## 发音 · 语音评测（流式版 / 讯飞自研 ISE）结果", "",
         f"评测范围：**念题部分**（共 {len(per_q)} 句）。音频：`{Path(meta.get('file', '')).name}`", "",
         "> 本引擎定位是**质检**：主看 `is_rejected`（是否乱读）与 `except_info`（音频质量）。",
         "> 它不能限定英式/美式，语调重音类字段官方标注「效果优化中」，因此不作为主评分引擎。", ""]

    keys = ("overall", "accuracy", "fluency", "standard", "integrity")
    avg = {k: avg_of(valid, k) for k in keys}
    L += ["| 指标 | 平均分（0–100） | 教学映射 0–5 |", "|---|---|---|",
          f"| **总分 total** | **{fmt_score(avg['overall'])}** | **{fmt_band(to_band(avg['overall']))}** |",
          f"| 准确度 accuracy（权重 50%） | {fmt_score(avg['accuracy'])} | {fmt_band(to_band(avg['accuracy']))} |",
          f"| 流畅度 fluency（权重 30%） | {fmt_score(avg['fluency'])} | —（归话语组织） |",
          f"| 标准度 standard（权重 20%） | {fmt_score(avg['standard'])} | {fmt_band(to_band(avg['standard']))} |",
          f"| 完整度 integrity | {fmt_score(avg['integrity'])} | — |", ""]
    if rejected:
        names = "、".join(f"Q{q['qi']}" for q in rejected)
        L += [f"> ⚠️ **被判定为乱读的句子已剔除出平均值**（官方明确其分值不可作参考）：{names}", ""]
    if not valid:
        L += ["> ⚠️ **本次没有可用于参考的句子**（要么未取得分数、要么全部被判乱读），上表为「—（未取得）」。", ""]

    L += ["### 逐句得分（含乱读与音频质量判定）", "",
          "| 题 | total | 准确度 | 流畅度 | 标准度 | 完整度 | 乱读? | 音频诊断 |",
          "|---|---|---|---|---|---|---|---|"]
    for q in per_q:
        if not q.get("ok"):
            L.append(f"| Q{q['qi']} | —（未取得） | — | — | — | — | — | {q.get('error', '失败')} |")
            continue
        flag = "—" if not is_rejected(q) else "**⚠️ 是（不计入平均）**"
        if q.get("overall") is None:
            L.append(f"| Q{q['qi']} | —（未取得） | {fmt_score(q.get('accuracy'))} | {fmt_score(q.get('fluency'))} "
                     f"| {fmt_score(q.get('standard'))} | {fmt_score(q.get('integrity'))} | {flag} "
                     f"| {q.get('except_info_msg', '—')} |")
        else:
            L.append(f"| Q{q['qi']} | {fmt_score(q.get('overall'))} | {fmt_score(q.get('accuracy'))} "
                     f"| {fmt_score(q.get('fluency'))} | {fmt_score(q.get('standard'))} "
                     f"| {fmt_score(q.get('integrity'))} | {flag} | {q.get('except_info_msg', '—')} |")
    L.append("")
    if valid:
        L += ["> 官方评分标准（原文）：准确度「单词发音准确清晰」；流畅度「朗读流利，语速正常，基本不出现停顿、重复、自我更正」；"
              "标准度「发音习惯符合英语母语标准（**无中式口音**），能灵活运用连读、重读、失音、爆破等发音技巧，节奏良好」。", ""]
    if debug:
        L += ["### 原始解析结果（debug）", "", "```json",
              json.dumps(per_q, ensure_ascii=False, indent=1)[:3000], "```", ""]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="语音评测（流式版）· 念题部分发音评分 / 质检")
    ap.add_argument("transcript", type=Path)
    ap.add_argument("--questions", type=Path, required=True)
    ap.add_argument("--category", default="read_sentence",
                    choices=["read_sentence", "read_chapter", "read_word"])
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

    per_q = []
    for qi, (qspan, _a, _d) in enumerate(spans, 1):
        if not qspan:
            per_q.append({"qi": qi, "ok": False, "error": "题目未定位"})
            continue
        start, end = qspan[0]["start"], qspan[-1]["end"]
        print(f"[ise-flow] Q{qi} {start:.1f}–{end:.1f}s", flush=True)
        try:
            audio = segment_mp3_b64(media, start, end)
            res = assess_ise(cfg, audio, questions[qi - 1], category=args.category)
        except Exception as exc:
            print(f"[ise-flow] Q{qi} 失败：{exc}", file=sys.stderr)
            per_q.append({"qi": qi, "ok": False, "error": str(exc)[:120]})
            continue
        res["qi"] = qi
        res["ok"] = True
        per_q.append(res)

    report = render(payload, per_q, args.debug)
    out = args.out or args.transcript.with_name(args.transcript.stem + "-发音-ise流式版.md")
    out.write_text(report, encoding="utf-8")
    print(f"\n已写出 {out}")
    ok = [q for q in per_q if q.get("ok") and q.get("overall") is not None]
    valid = [q for q in ok if not is_rejected(q)]
    rejected = [f"Q{q['qi']}" for q in ok if is_rejected(q)]
    if not valid:
        print("❌ 没有一句可用于参考（未取得分数，或全部被判乱读）——本次结果不可用于给分。",
              file=sys.stderr)
        sys.exit(1)
    avg = avg_of(valid, "overall")
    print(f"念题部分平均总分 {avg:.1f}/100 → 教学映射 {to_band(avg)}/5（{len(valid)}/{len(per_q)} 句计入）")
    if rejected:
        print(f"⚠️ 被判定为乱读、已剔除出平均值的句子：{', '.join(rejected)} —— 这些分数不可作为参考")
    if len(valid) < len(per_q):
        failed = [f"Q{q['qi']}" for q in per_q if not (q.get("ok") and q.get("overall") is not None)]
        if failed:
            print(f"⚠️ 未取得分数的句子：{', '.join(failed)}", file=sys.stderr)


if __name__ == "__main__":
    main()
