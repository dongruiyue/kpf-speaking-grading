#!/usr/bin/env python3
"""KPF 口语批改 skill · 环境与凭证自检（离线：不联网、不装包、不花额度）

配完 config 之后、或换一台机器之后跑一次，它一次告诉你：

  · 凭证在哪、哪些块配齐了（**只显示掩码，不打印密钥本体**）
  · 发音分会从哪个 provider 来；一个都没有时明确告诉你"只能手填"
  · 题库、虚拟环境、依赖（faster-whisper / av / websockets / requests）有没有问题
  · 下一步该跑哪条命令

它**不发任何网络请求**——所以"到底打不打得通"它答不了。真打通用 1 句试，
步骤见 `docs/xfyun-setup.md` 第四节（消耗 1 次额度）。

用法：
  kpf_doctor.py [--config <path>] [--questions <目录>] [--venv <目录>]
退出码：0 = 发音分有引擎来源；1 = 一个评测引擎都没配（发音分只能教师手填）；2 = 用法错。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = Path.home() / ".kpf-speaking" / "config.json"
# 讯飞听见的转发件不在本仓库里，默认位置与 kpf_asr.py 保持一致
DEFAULT_XFTJ_DIR = Path.home() / "Documents" / "skills" / "xftj-transcribe" / "scripts"
MODULES = ["faster_whisper", "av", "websockets", "requests", "numpy"]

# 端点默认值从实现处取，免得两处硬编码各说各话；取不到就退回字面量（本脚本要能独立跑）
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from kpf_xfyun import HOST, PATH_ZH_EN  # noqa: E402
except Exception:  # noqa: BLE001
    HOST, PATH_ZH_EN = "cn-east-1.ws-api.xf-yun.com", "/v1/private/s8e098720"


def mask(value: str) -> str:
    """只露前 4 位与长度——自检报告会被复制到聊天里，别把密钥带出去。"""
    value = str(value)
    return f"{value[:4]}…（{len(value)} 位）" if len(value) > 4 else "已填（太短，可疑）"


class Check:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def say(self, text: str = "") -> None:
        self.lines.append(text)

    def ok(self, text: str) -> None:
        self.say(f"  ✅ {text}")

    def warn(self, text: str) -> None:
        self.say(f"  ⚠️  {text}")

    def bad(self, text: str) -> None:
        self.say(f"  ❌ {text}")


def load_config(path: Path, chk: Check) -> dict:
    if not path.exists():
        chk.warn(f"没有 {path}（所有 key 都可以留空，但发音分就需要老师手填）")
        chk.say(f"      要配就跑：mkdir -p {path.parent} && "
                f"cp {ROOT / 'config.example.json'} {path}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 配置坏了必须说清楚，不能当没配
        chk.bad(f"{path} 不是合法 JSON：{exc}")
        return {}
    chk.ok(f"读到配置 {path}")
    return data if isinstance(data, dict) else {}


def check_xfyun(cfg: dict, chk: Check) -> bool:
    x = cfg.get("xfyun") or {}
    missing = [k for k in ("appid", "api_key", "api_secret") if not str(x.get(k, "")).strip()]
    if not x:
        chk.say("  ⬜ xfyun：没配（发音分首选来源；配法见 docs/xfyun-setup.md）")
        return False
    if missing:
        chk.bad(f"xfyun：缺字段 {', '.join(missing)} —— 三个都要填，且来自同一个应用")
        return False
    chk.ok(f"xfyun：已配 appid={mask(x['appid'])} api_key={mask(x['api_key'])} "
           f"api_secret={mask(x['api_secret'])}")
    chk.say(f"      端点：{x.get('host', HOST)}{x.get('path', PATH_ZH_EN)}")
    chk.say("      ⚠️  自检不联网，所以「服务是否已开通」这个前提它验不了——")
    chk.say("         第一次请按 docs/xfyun-setup.md 第四节用 1 句试（消耗 1 次额度）")
    return True


def check_azure(cfg: dict, chk: Check) -> bool:
    a = cfg.get("azure") or {}
    missing = [k for k in ("key", "region") if not str(a.get(k, "")).strip()]
    if not a or len(missing) == 2:   # 示例配置里就有这个空块：空的 = 没配，不是填错
        chk.say("  ⬜ azure：没配（可选备用；能评自由说的答题部分）")
        return False
    if missing:
        chk.bad(f"azure：缺字段 {', '.join(missing)} —— key 与 region 要一起填")
        return False
    chk.ok(f"azure：已配 key={mask(a['key'])} region={a['region']}")
    chk.say("      ⚠️  Azure 这条链**尚未用真实 key 验证过**（见 references/06-verification.md 第四节）")
    return True


def check_groq(cfg: dict, chk: Check) -> None:
    key = str(cfg.get("groq_api_key", "") or "").strip()
    if not key:
        chk.say("  ⬜ groq_api_key：没配（可选；配了转写走云端，秒级，但**学生音频会上传**）")
        return
    if not key.startswith("gsk_"):
        chk.warn(f"groq_api_key：已填 {mask(key)}，但不像 Groq key（通常以 gsk_ 开头）")
        return
    chk.ok(f"groq_api_key：已配 {mask(key)}（注意：用它会把你学生的音频上传到第三方）")


def check_xftj(cfg: dict, chk: Check) -> None:
    """讯飞听见走的是另一个 skill；本仓库不含它，所以这里只负责说清楚去哪找。"""
    env = os.environ.get("KPF_XFTJ_DIR", "").strip()
    cfg_dir = str((cfg.get("xftj") or {}).get("dir", "") or "").strip()
    path = Path(env or cfg_dir or DEFAULT_XFTJ_DIR)
    source = "环境变量 KPF_XFTJ_DIR" if env else ("config 的 xftj.dir" if cfg_dir else "默认位置")
    if path.is_dir():
        chk.ok(f"讯飞听见转发件：找到 {path}（{source}）")
    else:
        chk.say(f"  ⬜ 讯飞听见转发件：没找到 {path}（{source}）")
        chk.say("      不影响主流程——只有 `kpf_asr.py --engine xftj` 需要它，"
                "而且它要另装、另有配置、还会消耗账户权益（见 docs/xfyun-setup.md 第七节）")
        chk.say("      指路：export KPF_XFTJ_DIR=/path/to/xftj-transcribe/scripts")


def check_questions(path: Path, chk: Check) -> None:
    if not path.is_dir():
        chk.bad(f"题库目录不存在：{path}")
        return
    files = sorted(p for p in path.glob("*.txt"))
    if not files:
        chk.warn(f"{path} 里没有 .txt 题库 —— 讯飞是朗读型接口，**必须有参考文本**，"
                 f"缺了会降级（见 docs/xfyun-setup.md 第九节）")
        return
    chk.ok(f"题库 {path}：{len(files)} 份 .txt（一行一题；只放念题，放多了会多扣额度）")
    for f in files[:5]:
        lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        chk.say(f"      {f.name}：{len(lines)} 题")
    if len(files) > 5:
        chk.say(f"      …还有 {len(files) - 5} 份")


def check_venv(venv: Path, chk: Check) -> None:
    py = venv / "bin" / "python"
    if not py.exists():
        chk.bad(f"没有虚拟环境 {venv} —— 先跑：bash scripts/setup.sh")
        return
    chk.ok(f"虚拟环境：{venv}")
    probe = ("import importlib, sys\n"
             f"for m in {MODULES!r}:\n"
             "    try:\n"
             "        importlib.import_module(m)\n"
             "        print(m, 'OK')\n"
             "    except Exception as exc:\n"
             "        print(m, 'MISSING', type(exc).__name__)\n")
    try:
        out = subprocess.run([str(py), "-c", probe], capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        chk.bad(f"跑 {py} 失败：{exc}")
        return
    missing = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "OK":
            chk.say(f"      {parts[0]} ✓")
        elif len(parts) >= 2:
            missing.append(parts[0])
    if missing:
        chk.bad(f"缺依赖：{', '.join(missing)} —— 跑：bash scripts/setup.sh")
    else:
        chk.ok("依赖齐全（faster-whisper / av / websockets / requests / numpy）")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="KPF 口语批改 skill 环境与凭证自检（离线，不联网、不花额度）")
    ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--questions", type=Path, default=ROOT / "questions")
    ap.add_argument("--venv", type=Path, default=ROOT / ".venv")
    args = ap.parse_args()

    chk = Check()
    chk.say("KPF 口语批改 skill · 自检（离线；不联网、不花额度）")
    chk.say(f"仓库：{ROOT}")
    chk.say()

    cfg = load_config(args.config, chk)

    chk.say()
    chk.say("一、发音分的来源（有任何一个，发音维度就不用老师手填）")
    has_xfyun = check_xfyun(cfg, chk)
    has_azure = check_azure(cfg, chk)

    chk.say()
    chk.say("二、转写与其它")
    check_groq(cfg, chk)
    check_xftj(cfg, chk)

    chk.say()
    chk.say("三、题库与环境")
    check_questions(args.questions, chk)
    chk.say()
    check_venv(args.venv, chk)

    chk.say()
    chk.say("─" * 62)
    if has_xfyun or has_azure:
        engine = "讯飞 ISE（首选）" if has_xfyun else "Azure"
        chk.say(f"结论：发音分有引擎来源 → **{engine}**。")
        if has_xfyun:
            chk.say("下一步（消耗 1 次额度验证真的打通）：")
            chk.say(f"  echo \"What kind of music do you listen to in your free time? Why?\" > /tmp/one.txt")
            chk.say(f"  {args.venv}/bin/python scripts/kpf_pronounce.py "
                    f"work/<日期>-<学生>/<文件名>--local.json \\")
            chk.say(f"      --provider xfyun --questions /tmp/one.txt --out /tmp/发音-test.md")
            chk.say("看到 `provider=xfyun` 就是通了；看到 `[降级]` 就去 "
                    "docs/xfyun-setup.md 第六节查那行原因。")
        code = 0
    else:
        chk.say("结论：**没有任何评测引擎**，发音分会降级成教师手填（流程仍然完整）。")
        chk.say("要接通讯飞，按 docs/xfyun-setup.md 走一遍（5 步，约 10 分钟，免信用卡）。")
        code = 1

    print("\n".join(chk.lines))
    sys.exit(code)


if __name__ == "__main__":
    main()
