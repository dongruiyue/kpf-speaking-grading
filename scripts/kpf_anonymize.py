#!/usr/bin/env python3
"""KPF 口语作业 · 真实 case → 可公开夹具

一条命令把真实 case 目录转成能进公开仓库的版本，并在产出后自动再扫一遍：
姓名 / 班号 / 绝对路径有残留就退出码 1 并指出位置。
「以为匿名了其实没有」是这类工具最常见的翻车方式，所以自检是核心，不是附赠。

保留什么：所有英文原句、分数、耗时、指标——夹具的价值就在错误类型与数据形态，
把这些抹掉等于把夹具变成空壳。

替换什么：
  真实姓名        → 学生甲 / 学生乙 / 学生丙（--map 姓名=代号 或按出现顺序自动编号，
                    同一姓名全篇一致）
  真实班号        → 级别字母 + 「-A」（例如某班的 FCE 编号一律变成 FCE-A）
  家目录绝对路径  → macOS / Linux / Windows 三种家目录前缀（斜杠 + 用户名开头的绝对路径）
                    与 `~/…` 一律换成 `<工作目录>`，后面的目录结构保留
  音视频文件名    → 去掉人名后的占位名 `<页号>-<代号>` 加原扩展名（P56 页那份录音
                    占位成 P56-学生甲）
  转写 JSON 的    → meta.student / meta.class / meta.file 随同替换（文本替换 + 产物 JSON
                    meta.*           仍能 json.loads 的自检）

姓名从哪来：--map / --names 显式给的，加上自动识别（case 目录名与文件名的 <姓名> 段、
转写 JSON 的 meta.student、文档里的「学生: 某某」）。自动识别会漏掉只在正文里提过一次的名字，
所以自检另外用百家姓启发式兜底：宁可误报，也不放过。误报了用 --allow 显式豁免。

用法：
  kpf_anonymize.py <真实case目录> --out <产物目录>
  kpf_anonymize.py <真实case目录> --out <产物目录> --map 张三=学生甲 --names 李四
  kpf_anonymize.py <真实case目录> --dry-run          # 只打印将要做的替换
  kpf_anonymize.py <真实case目录> --out <产物目录> --media placeholder --binaries copy

--media / --binaries 默认都 skip：音视频与二进制（图片、pdf、docx）不复制进产物，只在
引用处改名——公开夹具不该带学生的原始录音与照片。确有授权时再显式 copy。

退出码：
  0 = 产物干净，或 --dry-run 正常
  1 = 自检发现残留（姓名 / 班号 / 绝对路径 / 产物 JSON 解析不了）——产物已写出但不干净
  2 = 用法或路径错误
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

MEDIA_EXT = {".m4a", ".mp3", ".mp4", ".mov", ".wav", ".aac", ".flac", ".mkv", ".m4v"}
PSEUDO_CHARS = "甲乙丙丁戊己庚辛壬癸"
# 班号：级别字母 + 2–4 位数字，一律换成「级别字母 + -A」（FCE 班的任何编号都收敛成 FCE-A）
CLASS_PAT = re.compile(r"(?<![A-Za-z0-9])(FCE|PET|KET|CAE|CPE)[ -]?\d{2,4}(?![0-9])")
ABS_PATS = [
    (re.compile(r"/(?:Users|home|Volumes)/[^/\s\"'`，。）)、；;]+"), "<工作目录>"),
    (re.compile(r"~(?=/[^/\s])"), "<工作目录>"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"'`]+"), "<工作目录>"),
]
# 残留扫描里「绝对路径」的判定与替换同源，避免改了替换规则却忘了自检
TOKEN_SPLIT = re.compile(r"[-_—–·．.、,，;；:：\s()（）\[\]【】{}]+")
CTX_NAME = re.compile(r"(?:学生|姓名|学员|同学)\s*[:：]\s*([\u4e00-\u9fff]{2,4})")
# 百家姓里的低歧义子集：歧义大的（高/白/金/方/任/于/曾/段/苏/温/陆/汤/顾/武/严/钱/毛/龙/史…）
# 不进这个表——「高分」「严格」「方式」这类词一旦误报，红灯就没人看了。
SURNAMES = "张王李赵刘陈杨黄周吴徐孙朱胡郭何林罗郑梁谢宋唐许韩冯曹邓蒋蔡杜魏吕丁沈姚卢姜崔谭潘叶汪闫尹秦邱薛雷贺陶黎龚郝邵廖邹孟韦马"
NAME_PAT = re.compile("([" + SURNAMES + r"][\u4e00-\u9fff]{1,2})")
STOP_NAMES = {
    "周末", "周围", "周期", "周一", "周五", "周天", "马上", "马路", "孙子", "何必", "何况",
    "何时", "许多", "谢谢", "谢了", "叶子", "叶片", "雷同", "雷区", "黎明", "陶瓷", "韩国",
    "唐朝", "唐诗", "秦朝", "宋朝", "黄色", "胡说", "胡子", "陈旧", "杜绝", "徐徐", "罗列",
    "杨树", "朱红", "胡乱", "黄老师", "张老师", "李老师",
}
# 启发式会把「李四的录音」这类黏在名字后的虚词一起吞掉，报告前削掉，免得让人对着
# 「李四的」去 --names 里找名字
TAIL_PARTICLES = "的了是和与在有我你他她它们把被就都也还很会能要来去上下里中对给"


class UsageError(Exception):
    """用法或路径问题（退出码 2），与「产物残留」（退出码 1）分开。"""


def die(msg: str) -> None:
    print(f"匿名化无法执行：{msg}", file=sys.stderr)
    sys.exit(2)


class Plan:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}      # 原名 → 代号（按看到顺序）
        self.media: dict[str, str] = {}      # 原文件名 → 占位名
        self.primary = "学生甲"

    @property
    def pseudonyms(self) -> set:
        return set(self.names.values())

    def component(self, comp: str) -> str:
        """目录名 / 文件名（不含路径）的去标识化。"""
        if comp in self.media:
            return self.media[comp]
        t = comp
        for orig in sorted(self.names, key=len, reverse=True):
            t = t.replace(orig, self.names[orig])
        return CLASS_PAT.sub(lambda m: m.group(1) + "-A", t)

    def text(self, s: str) -> tuple[str, dict]:
        hits: dict[str, int] = {}

        def bump(rule: str, n: int = 1) -> None:
            if n:
                hits[rule] = hits.get(rule, 0) + n

        for pat, repl in ABS_PATS:
            s, n = pat.subn(repl, s)
            bump("绝对路径", n)
        for orig, new in self.media.items():
            if orig in s:
                bump("音视频名", s.count(orig))
                s = s.replace(orig, new)
        # 只换整名（连扩展名）。换主干会误伤「P56页自问自答。张三--local.json」这类
        # 同主干不同后缀的引用，把产物里还存在的文件链接改写成不存在的路径。
        for orig in sorted(self.names, key=len, reverse=True):
            if orig in s:
                bump("姓名", s.count(orig))
                s = s.replace(orig, self.names[orig])
        s, n = CLASS_PAT.subn(lambda m: m.group(1) + "-A", s)
        bump("班号", n)
        return s, hits


def iter_files(src: Path):
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            yield Path(root) / f


def looks_like_name(tok: str) -> bool:
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", tok)) and bool(NAME_PAT.fullmatch(tok)) \
        and tok not in STOP_NAMES


def gather_names(src: Path, mapping: dict, extra: list) -> list:
    """按「先显式、后自动」的顺序收集姓名；顺序决定 甲乙丙 的编号，所以必须确定。"""
    found: list[str] = []

    def see(name: str, force: bool = False) -> None:
        name = (name or "").strip()
        if not name or name in found:
            return
        if force or looks_like_name(name):
            found.append(name)

    for n in mapping:
        see(n, force=True)
    for n in extra:
        see(n, force=True)

    def scan_label(label: str) -> None:
        for tok in TOKEN_SPLIT.split(label):
            see(tok)
            for m in NAME_PAT.finditer(tok):
                see(m.group(1))

    scan_label(src.name)
    files = list(iter_files(src))
    for p in files:
        scan_label(p.name)
        for part in p.relative_to(src).parts[:-1]:
            scan_label(part)
    for p in files:
        if p.suffix.lower() != ".json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for key in ("student", "name"):
            val = (data.get("meta") or {}).get(key) if isinstance(data.get("meta"), dict) else None
            if isinstance(val, str):
                see(val, force=True)
    for p in files:
        try:
            text = p.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
        for m in CTX_NAME.finditer(text):
            see(m.group(1), force=True)
    return found


def build_plan(src: Path, mapping: dict, extra: list) -> Plan:
    plan = Plan()
    found = gather_names(src, mapping, extra)
    pool = ["学生" + c for c in PSEUDO_CHARS] + [f"学生{i}" for i in range(1, 100)]
    used = set(mapping.values())
    for name in found:
        if name in mapping:
            plan.names[name] = mapping[name]
            continue
        pick = next((c for c in pool if c not in used), None)
        if pick is None:
            raise UsageError("姓名太多，代号池不够用（超过 110 个）")
        used.add(pick)
        plan.names[name] = pick
    if plan.names:
        plan.primary = list(plan.names.values())[0]

    taken: set = set()
    for p in sorted(iter_files(src)):
        if p.suffix.lower() not in MEDIA_EXT:
            continue
        new = media_name(p.name, plan, taken)
        taken.add(new)
        plan.media[p.name] = new
    return plan


def media_name(fname: str, plan: Plan, taken: set) -> str:
    """音视频占位名：P<页号>-<代号>.ext，既去人名又留下「哪一页/谁的」可读信息。"""
    p = Path(fname)
    stem = p.stem
    stem_anon = plan.component(stem)
    pseudo = next((v for v in plan.names.values() if v in stem_anon), None)
    if pseudo is None:
        pseudo = next((v for k, v in plan.names.items() if k in stem), None) or plan.primary
    m = re.search(r"P\s*(\d{1,3})", stem) or re.search(r"P\s*(\d{1,3})", stem_anon)
    if m:
        base = f"P{m.group(1)}-{pseudo}"
    else:
        base = f"{pseudo}-{stem_anon}" if stem_anon else f"{pseudo}-音频"
    name = base + p.suffix
    k = 2
    while name in taken:
        name = f"{base}-{k}{p.suffix}"
        k += 1
    return name


def is_text(path: Path) -> bool:
    try:
        path.read_bytes().decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


# ---------- 自检 ----------

def scan(text: str, plan: Plan, allow: set) -> list[tuple[str, str]]:
    """一行文本里的残留：(类型, 命中词)。"""
    out: list[tuple[str, str]] = []
    for name in plan.names:
        if name in text:
            out.append(("姓名", name))
    m = CLASS_PAT.search(text)
    if m:
        out.append(("班号", m.group(0)))
    for pat, _ in ABS_PATS:
        m = pat.search(text)
        if m:
            out.append(("绝对路径", m.group(0)))
    for m in CTX_NAME.finditer(text):
        tok = m.group(1)
        if tok not in plan.pseudonyms and tok not in allow:
            out.append(("姓名", tok))
    for m in NAME_PAT.finditer(text):
        tok = m.group(1)
        while len(tok) > 2 and tok[-1] in TAIL_PARTICLES:
            tok = tok[:-1]
        if tok in STOP_NAMES or tok in allow or tok in plan.pseudonyms:
            continue
        if any(tok != n and tok in n for n in plan.names):
            continue
        out.append(("疑似姓名", tok))
    return out


def self_check(out: Path, plan: Plan, allow: set) -> list[str]:
    findings: list[str] = []
    for root, dirs, files in os.walk(out):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for d in dirs:
            rel = Path(root, d).relative_to(out)
            for kind, tok in scan(d, plan, allow):
                findings.append(f"[{kind}] {tok} ← 目录名 {rel}/")
        for f in sorted(files):
            p = Path(root, f)
            rel = p.relative_to(out)
            for kind, tok in scan(f, plan, allow):
                findings.append(f"[{kind}] {tok} ← 文件名 {rel}")
            if p.suffix.lower() == ".json":
                try:
                    json.loads(p.read_bytes().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    findings.append(f"[JSON] 产物解析不了 ← {rel}：{e}")
            if not is_text(p):
                continue
            text = p.read_bytes().decode("utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for kind, tok in scan(line, plan, allow):
                    findings.append(f"[{kind}] {tok} ← {rel}:{lineno}")
    return findings


# ---------- 主流程 ----------

def parse_map(pairs: list) -> dict:
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise UsageError(f"--map 要写成 姓名=代号（收到「{item}」）")
        k, v = item.split("=", 1)
        if not k.strip() or not v.strip():
            raise UsageError(f"--map 两边都不能空（收到「{item}」）")
        out[k.strip()] = v.strip()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="把真实 case 目录转成可公开的夹具（纯标准库、离线；只读原目录）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="替换：真实姓名 → 学生甲/乙/丙 · 班号 → 级别字母-A（如 FCE-A）\n"
               "      · 家目录绝对路径 → <工作目录>（保留后面的目录结构）\n"
               "      · 音视频文件名 → P<页号>-<代号> 加原扩展名\n"
               "      · 转写 JSON 的 meta.student / class / file\n"
               "保留：所有英文原句、分数、耗时、指标（夹具的价值就在错误类型与数据形态）。\n"
               "自检：产出目录再扫一遍姓名 / 班号 / 绝对路径 / 产物 JSON，有残留就退出码 1\n"
               "      并指出位置。兜底的姓名启发式宁可误报，误报用 --allow 豁免。")
    ap.add_argument("case", type=Path, help="真实 case 目录（只读）")
    ap.add_argument("--out", type=Path, default=None, help="产物目录（--dry-run 时可省）")
    ap.add_argument("--names", default="", help="显式姓名，逗号分隔；不写则自动识别")
    ap.add_argument("--map", action="append", default=[], metavar="姓名=代号",
                    help="指定某姓名用哪个代号（可重复）；其余按出现顺序自动编号")
    ap.add_argument("--allow", action="append", default=[], metavar="词",
                    help="自检时豁免的词（启发式误报，可重复）")
    ap.add_argument("--media", choices=["skip", "placeholder", "copy"], default="skip",
                    help="音视频本体：skip（默认，只改引用名）／placeholder（写同名 0 字节占位）"
                         "／copy（确有授权才用）")
    ap.add_argument("--binaries", choices=["skip", "copy"], default="skip",
                    help="其他二进制（图片/pdf/docx）：skip（默认）／copy")
    ap.add_argument("--mapping-out", type=Path, default=None,
                    help="把「原名 → 代号」的对照表写到这个路径（给老师自己留档；禁止写在 --out 里）")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做的替换，不写文件")
    ap.add_argument("--force", action="store_true",
                    help="--out 已存在且有内容时照样写（不清理旧文件，自检会把旧文件一起扫）")
    args = ap.parse_args()

    src = args.case
    if not src.is_dir():
        die(f"case 目录不存在或不是目录：{src}")
    extra = [n.strip() for n in args.names.split(",") if n.strip()]
    allow = set(args.allow)
    try:
        mapping = parse_map(args.map)
        plan = build_plan(src, mapping, extra)
    except UsageError as e:
        die(str(e))
    if not plan.names:
        print("提示：没识别到学生姓名。若确实没有，可忽略；否则用 --names 显式给出，"
              "否则这个 case 里出现的姓名不会被替换（自检也只会靠启发式兜底）。", file=sys.stderr)

    files = list(iter_files(src))
    text_files = [p for p in files if p.suffix.lower() not in MEDIA_EXT and is_text(p)]
    media_files = [p for p in files if p.suffix.lower() in MEDIA_EXT]
    bin_files = [p for p in files if p.suffix.lower() not in MEDIA_EXT and not is_text(p)]

    if args.mapping_out is not None and args.out is not None:
        try:
            args.mapping_out.resolve().relative_to(args.out.resolve())
            die(f"--mapping-out 不能写在 --out 里（{args.mapping_out}）：对照表进产物就等于没匿名")
        except ValueError:
            pass
    if args.mapping_out is not None and args.mapping_out.exists() and not args.force:
        die(f"--mapping-out 目标已存在：{args.mapping_out}"
            f"（不覆盖，先自己删；或加 --force 让它覆盖）")

    if args.dry_run:
        print(f"[dry-run] 只打印将要做的替换，不写任何文件。源目录：{src}")
        print(f"姓名映射：{' ｜ '.join(f'{k} → {v}' for k, v in plan.names.items()) or '（无）'}")
        print("班号：<级别字母><2–4 位数字> → <级别字母>-A（例如 FCE 班的编号一律收敛成 FCE-A）")
        print("家目录绝对路径：macOS / Linux / Windows 三种家目录前缀与 ~/… → <工作目录>")
        if plan.media:
            print(f"音视频改名（{args.media}）：")
            for k, v in plan.media.items():
                print(f"  {k} → {v}")
        print(f"将写 {len(text_files)} 个文本文件"
              f"（转写 JSON 的 meta.student/class/file 在其中一起改）：")
        for p in text_files:
            rel = p.relative_to(src)
            new_rel = Path(plan.component(src.name)) / Path(*[plan.component(c) for c in rel.parts])
            _, hits = plan.text(p.read_bytes().decode("utf-8"))
            hit_txt = "、".join(f"{k} {v} 处" for k, v in hits.items()) or "无命中"
            print(f"  {rel} → {new_rel}（{hit_txt}）")
        if media_files:
            print(f"音视频本体：{args.media}"
                  + ("（不复制，仅引用处改名）" if args.media == "skip" else ""))
            for p in media_files:
                print(f"  {p.relative_to(src)} → {plan.media[p.name]}")
        if bin_files:
            print(f"其他二进制（{args.binaries}）：" +
                  "、".join(str(p.relative_to(src)) for p in bin_files))
        return

    if args.out is None:
        die("要给 --out 产物目录（或加 --dry-run 只看替换计划）")
    out = args.out
    if out.exists() and any(out.iterdir()) and not args.force:
        die(f"--out 已存在且有内容：{out}（换一个目录，或 --force 覆盖写入）")

    out.mkdir(parents=True, exist_ok=True)
    root = out / plan.component(src.name)
    root.mkdir(parents=True, exist_ok=True)
    for p in files:
        rel = p.relative_to(src)
        new_rel = Path(*[plan.component(c) for c in rel.parts])
        target = root / new_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix.lower() in MEDIA_EXT:
            if args.media == "copy":
                target.write_bytes(p.read_bytes())
            elif args.media == "placeholder":
                target.write_bytes(b"")
            continue
        if not is_text(p):
            if args.binaries == "copy":
                target.write_bytes(p.read_bytes())
            continue
        new_text, _ = plan.text(p.read_bytes().decode("utf-8"))
        target.write_bytes(new_text.encode("utf-8"))

    if args.mapping_out is not None:
        args.mapping_out.write_text(
            json.dumps({"姓名 → 代号": plan.names, "音视频": plan.media,
                        "源目录": str(src)}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"对照表已写出 {args.mapping_out}（别放进公开仓库）", file=sys.stderr)

    print(f"已写出产物：{root}")
    print(f"文本 {len(text_files)} 个（内容已替换）· 音视频 {len(media_files)} 个"
          f"（策略 {args.media}）· 其他二进制 {len(bin_files)} 个（策略 {args.binaries}）")

    findings = self_check(out, plan, allow)
    print()
    if findings:
        print(f"自检未通过：{len(findings)} 处残留（产物已写出，但不干净）", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        print(f"FAIL {out} · {len(findings)} 处残留")
        sys.exit(1)
    print(f"自检通过：{out} 下没有姓名 / 班号 / 绝对路径残留，产物 JSON 均可解析")


if __name__ == "__main__":
    main()
