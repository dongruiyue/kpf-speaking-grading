#!/usr/bin/env python3
"""KPF 口语批改 skill · 发布闸门（脱敏扫描）

纯标准库 · 离线 · 只读：扫全仓文本，命中一条就打一行 `文件:行号: 原因`，最后 `sys.exit(1)`。
不联网、不装包、不改任何文件。

检查项
  1. 真实学生姓名 · 真实班号  黑名单一行一条（`#` 注释）。**文件名与文件内容都扫**。黑名单查找顺序：
                             ① `~/.kpf-speaking/publishable-denylist.txt`（私有，住仓库外）
                             ② `scripts/publishable-denylist.example.txt`（仓库内示例）
  2. 个人绝对路径与用户名    家目录绝对路径（macOS 那种 `/`+`Users`+`/` 开头）、本机用户名
  3. 凭证形态               以 `gsk` 下划线开头且后接 ≥16 位密钥体的 Groq key、
                            `api_key` / `api_secret` 后接**像密钥的**非空值、40+ 位 hex、
                            PEM 私钥头
  4. 音视频文件             ① **候选文件自己的扩展名**是 .m4a / .mp3 / .wav / .mp4 / .mov
                            （**对每一个会被提交的文件都查，包括读不出 UTF-8 的**）；
                            ② 文本里写出了音视频文件名（`P179页自问自答。.m4a` 这种）
  5. `.venv` 是否被 git 跟踪  `git ls-files`；**没有 git 仓库时跳过这一项并说明，不算命中**
  6. 私有黑名单是否被误提交   仓库里不该有叫 `publishable-denylist.txt` 的文件——
                            它装的就是要拦的真实姓名/班号，提交出去等于没脱敏

用法：
  check_publishable.py [--root <目录>] [--denylist <文件>] [-v]
退出码：0 = 可发布；1 = 有命中；2 = 用法错（路径不存在等）。

三条自我约束（否则这道闸门会永远红）
  - 黑名单文件本身不扫：它的内容就是要拦的词。
  - **私有黑名单只住 `~/.kpf-speaking/`**：仓库里那份只装占位示例，扫了也无害。
  - 本脚本源码里不写连续的目标字面量（家目录前缀、本机用户名、密钥前缀、PEM 头都是拼出来的），
    这样"扫描器自己也能过闸门"，而不是把扫描器塞进豁免名单。

两条刻意收窄（避免把工具链自己写死成永远红）：
  - 凭证值要**像密钥**（无空格、无花括号/CJK、不是 your_key_here 这类占位符）才算命中；
    `f'api_key="{api_key}", algorithm="..."'` 这种拼 Header 的代码不该被当成泄漏。
  - 音视频名要求**名字部分完整**（§4 不匹配代码里拼出来的后缀片段）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints"}
# 私有黑名单住在仓库外（与 config.json 同目录），仓库里只留 .example.txt 兜底
PRIVATE_DENYLIST = Path.home() / ".kpf-speaking" / "publishable-denylist.txt"
EXAMPLE_DENYLIST = Path(__file__).resolve().parent / "publishable-denylist.example.txt"
REPO_DENYLIST_NAME = "publishable-denylist.txt"


def default_denylist() -> Path:
    """私有名单优先；没有就用仓库内的示例（单纯克隆仓库的人什么都不用配）。"""
    return PRIVATE_DENYLIST if PRIVATE_DENYLIST.is_file() else EXAMPLE_DENYLIST

# 本脚本自己必须能过闸门：下面这些目标串一律拼出来，源码里不出现连续形态。
HOME_MARK = "/" + "Users" + "/"
USERNAME = "steven" + "dong"
GROQ_RE = re.compile("gsk" + r"_[A-Za-z0-9_\-]{16,}")
# 值必须"像密钥"：无空格、无花括号/CJK、不是占位符；否则 f'api_key="{api_key}"' 这类
# 拼请求头的代码会被误判成泄漏。
SECRET_NAME_RE = re.compile(r"(?i)(?:api[_-]?key|api[_-]?secret)\s*[=:]\s*"
                            r"(?:\"([^\"\n]{8,})\"|'([^'\n]{8,})'|([A-Za-z0-9_\-.]{16,}))")
SECRET_VALUE_RE = re.compile(r"[A-Za-z0-9_\-+/=.]{8,}")
PLACEHOLDER_VALUE_RE = re.compile(
    r"(?i)(your|example|sample|placeholder|change[_-]?me|x{3,}|todo|fake|dummy|"
    r"redacted|hidden|none|null|key|secret|token)")
HEX40_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])")
PEM_MARK = "-" * 5 + "BEGIN"
AV_EXTS = ("m4a", "mp3", "wav", "mp4", "mov")
# 判定"文件名出现"的两个约束：名字前面必须是分隔符/引号/空白（行首按补了空格处理），
# 且名字不能以点开头。这样代码里拼出来的后缀片段不算泄漏，裸文件名仍然算。
AV_BOUNDARY = r"""[\s"'`(/\\~=:;,\[（）【「『〔，。、：；！？]"""
AV_NAME_RE = re.compile("(?<=" + AV_BOUNDARY + r")[A-Za-z0-9_\-\u4e00-\u9fff]"
                        r"[A-Za-z0-9_\-.\u4e00-\u9fff]{1,}\.(?:"
                        + "|".join(AV_EXTS) + r")(?![A-Za-z0-9])", re.I)


def looks_like_credential(value: str) -> bool:
    """值像不像真密钥：字符集对 + 不是 your_key_here 这类占位符。"""
    if PLACEHOLDER_VALUE_RE.search(value):
        return False
    return SECRET_VALUE_RE.fullmatch(value) is not None


def load_denylist(path: Path) -> list[str]:
    if not path.is_file():
        fail(f"黑名单文件不存在：{path}")
    words: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line not in words:
            words.append(line)
    if not words:
        fail(f"黑名单是空的：{path}（至少要有姓名与班号，否则第 1 项检查没有覆盖面）")
    return words


def gitignored_top_dirs(root: Path) -> set[str]:
    """退路用：从 `.gitignore` 里取**顶层目录名**（`work/`、`calibration/` 这类）。

    只认「不含斜杠、不含通配符」的行 —— 那种一眼就能看出是目录名的；不解释通配符语法，
    宁可少跳过也不乱跳过（真正的依据是 git 自己给的清单，这条只在没有 git 时兜底）。
    """
    ignore = root / ".gitignore"
    if not ignore.is_file():
        return set()
    names: set[str] = set()
    for line in ignore.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        name = line.rstrip("/")
        if "/" not in name and not any(c in name for c in "*?["):
            names.add(name)
    return names


def iter_candidate_files(root: Path, skip: set[Path]):
    """列出**会被提交的文件**，并分出其中能按 UTF-8 读的那些。

    返回 (全部候选, 不可读的候选, 取文件方式的说明)。

    **候选 = 全部会被提交的文件，不只是能解码的那些**：第 4 项（音视频不许进仓库）与
    第 1–3 项不同，它看的是**文件名**，所以必须对每一个候选都跑。旧实现把"读不出 UTF-8"
    的文件当成二进制直接跳过，于是一个被跟踪的 `.wav` 从头到尾没被任何检查看过，闸门照样
    报"可发布"（外审 2026-10-04 复现：临时仓库 `git add -f` 一个 .wav，exit 0）。

    为什么不能无差别走文件树：这道闸门要拦的是"跟着仓库公开出去的东西"，而 `work/`
    （批改工作目录）与 `calibration/`（私有校准目录）按 `.gitignore` **永远不会提交** ——
    学生的真实姓名、音视频与转写就住在那里。旧实现无差别扫全树，于是**每次批改完作业，
    闸门都会被那些"正确地待在 gitignore 里"的工作文件点红**，真命中被假警报淹掉
    （2026-10-03 实测：一批真实姓名全在 `work/` 下，闸门报「不可发布：6 处命中」）。
    """
    git_list: list[str] | None = None
    try:
        proc = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"],
                              cwd=str(root), capture_output=True, text=True, check=True)
        git_list = proc.stdout.split("\0")
    except (OSError, subprocess.CalledProcessError):
        pass

    if git_list is None:
        ignored = gitignored_top_dirs(root)
        source = ("文件树（没有可用的 git → 退回遍历目录，"
                  f"并跳过 .gitignore 的顶层目录：{'、'.join(sorted(ignored)) or '（无）'}）")
        candidates = [p for p in sorted(root.rglob("*")) if p.is_file()
                      and not any(part in SKIP_DIRS or part in ignored
                                  for part in p.relative_to(root).parts[:-1])]
    else:
        source = "git 跟踪的文件 + 未被忽略的新文件（git ls-files -co --exclude-standard）"
        candidates = [root / rel for rel in git_list if rel]

    files: list[Path] = []
    unreadable: list[Path] = []
    for path in sorted(set(candidates)):
        rel = path.relative_to(root)
        if path in skip or any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            unreadable.append(path)
            continue
        files.append(path)
    return files, unreadable, source


def check_path_name(rel: str, denylist: list[str]) -> list[str]:
    """按**路径**判脱敏：黑名单词 / 个人绝对路径 / 本机用户名。

    为什么必须单列：内容扫描只看得到**文本文件里面**写了什么。一个叫
    `学生真名示例-作业.json` 的文件哪怕内容只有 `{}`，文件名也照样把姓名公开出去了
    （外审 2026-10-04 复现：该姓名在示例黑名单里，闸门仍 exit 0）。所以每一个待提交
    路径都要过一遍这三项，**包括读不出 UTF-8、进不了内容扫描的那些**。
    """
    hits: list[str] = []
    for word in denylist:
        if word in rel:
            hits.append(f"{rel}: 0: 文件名里出现黑名单词「{word}」："
                        f"真实姓名/班号，改成匿名代号（学生甲 / FCE-A）")
    if HOME_MARK in rel:
        hits.append(f"{rel}: 0: 路径里出现个人绝对路径「{HOME_MARK}」：换成相对路径或 ~ 占位")
    if USERNAME in rel:
        hits.append(f"{rel}: 0: 路径里出现本机用户名「{USERNAME}」：换成匿名占位")
    return hits


def check_av_name(path: Path, rel: str) -> list[str]:
    """第 4 项里**按文件名**判的那一半：候选文件自己的扩展名是音视频就算命中。

    它不看内容能不能解码，所以对 `.wav` / `.m4a` / `.mp4` 这些必然二进制的东西同样有效
    （`scan_file` 里那条 `AV_NAME_RE` 查的是"文本里提到了某个音视频文件名"，用途不同：
    它拦的是"报告里写出 `P179页自问自答。.m4a` 这种原始文件名"）。
    """
    if path.suffix.lower().lstrip(".") in AV_EXTS:
        return [f"{rel}: 0: 这个文件本身就是音频/视频（.{path.suffix.lower().lstrip('.')}）："
                f"一律不进仓库（`.gitignore` 已挡，别再 `git add -f` 硬塞）"]
    return []


def scan_file(path: Path, rel: str, denylist: list[str]) -> list[str]:
    hits: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:  # noqa: BLE001 - 读不了要显式说，不许静默跳过
        return [f"{rel}: 0: 读不了这个文件（{exc}），本轮第 1–4 项检查没有覆盖它"]

    def hit(no: int, why: str) -> None:
        entry = f"{rel}:{no}: {why}"
        if entry not in hits:
            hits.append(entry)

    for no, line in enumerate(lines, 1):
        for word in denylist:
            if word in line:
                hit(no, f"出现黑名单词「{word}」：真实姓名/班号，改成匿名代号（学生甲 / FCE-A）")
        if HOME_MARK in line:
            hit(no, f"出现个人绝对路径「{HOME_MARK}」：换成相对路径或 ~ 占位")
        if USERNAME in line:
            hit(no, f"出现本机用户名「{USERNAME}」：换成匿名占位")
        m = GROQ_RE.search(line)
        if m:
            hit(no, f"出现 Groq 密钥形态「{m.group(0)[:12]}…」：凭证绝不许进仓库")
        m = SECRET_NAME_RE.search(line)
        if m:
            value = next(g for g in m.groups() if g is not None)
            if looks_like_credential(value):
                hit(no, f"出现「api_key / api_secret」后接密钥值「{value[:8]}…」："
                        f"凭证绝不许进仓库（示例值留空字符串）")
        m = HEX40_RE.search(line)
        if m:
            hit(no, f"出现 40+ 位 hex 串「{m.group(0)[:12]}…」：疑似 key/哈希，确认后脱敏或改示例")
        if PEM_MARK in line:
            hit(no, "出现私钥头：凭证绝不许进仓库")
        m = AV_NAME_RE.search(" " + line)
        if m:
            hit(no, f"出现音视频文件名「{m.group(0)}」：音频/视频一律不进仓库（.gitignore 已挡）")
    return hits


def check_repo_denylist(root: Path) -> list[str]:
    """仓库里不该有叫 `publishable-denylist.txt` 的文件。

    那份私有名单装的就是要拦的真实姓名/班号——它一进仓库，这次脱敏就白做了。
    单独查文件名，是因为"扫内容"只在私有名单存在时才抓得到，而私有名单可能恰好不在本机。
    """
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        if REPO_DENYLIST_NAME in filenames:
            rel = Path(dirpath).relative_to(root) / REPO_DENYLIST_NAME
            hits.append(f"{rel}: 0: 私有黑名单出现在仓库里（它装的是真实姓名/班号）："
                        f"移到 {PRIVATE_DENYLIST}，仓库里只留 "
                        f"{EXAMPLE_DENYLIST.name}；并把它加进 .gitignore")
    return hits


def check_venv_tracked(root: Path) -> tuple[list[str], str | None]:
    """`.venv/` 是否被 git 跟踪。返回 (命中列表, 说明)。没有 git 仓库 → 说明里讲清楚并跳过。"""
    try:
        probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                               capture_output=True, text=True)
    except OSError:
        return [], "跳过：这台机器上没有 git 命令，`.venv/` 跟踪检查没跑"
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return [], "跳过：这里还不是 git 仓库（git rev-parse 未通过），`.venv/` 跟踪检查没跑"
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "--", ".venv"],
                            capture_output=True, text=True)
    tracked = [ln for ln in listed.stdout.splitlines() if ln.strip()]
    if tracked:
        return [f".venv: 0: `.venv/` 被 git 跟踪（{len(tracked)} 个文件，如 {tracked[0]}）："
                f"真实虚拟环境不得进仓库，先 `git rm -r --cached .venv` 再确认 .gitignore"], None
    return [], "`.venv/` 没有被 git 跟踪（已确认）"


def fail(msg: str, code: int = 2) -> None:
    print(f"发布闸门无法执行：{msg}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser(description="KPF 口语批改 skill 发布闸门（脱敏扫描，纯标准库、离线）")
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="要扫的仓库根目录（默认：本脚本所在 skill 的根目录）")
    ap.add_argument("--denylist", type=Path, default=None,
                    help=f"黑名单文件（默认 {PRIVATE_DENYLIST}，其次仓库内 {EXAMPLE_DENYLIST.name}）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印扫了多少文件、跳过多少")
    args = ap.parse_args()

    root = args.root.resolve()
    if not root.is_dir():
        fail(f"--root 不是目录：{root}")
    denylist_path = (args.denylist or default_denylist()).resolve()
    if not denylist_path.is_file():
        fail(f"黑名单文件不存在：{denylist_path}")
    denylist = load_denylist(denylist_path)

    skip = {denylist_path}
    files, unreadable, source = iter_candidate_files(root, skip)
    if args.verbose:
        print(f"扫了 {len(files)} 个文本文件（另有 {len(unreadable)} 个读不出 UTF-8 的文件："
              f"它们**只做了文件名检查**；{source}；黑名单文件本身不扫）")

    hits: list[str] = []
    # 路径级检查（黑名单词 / 家目录路径 / 用户名 / 音视频扩展名）对**每一个**待提交文件都跑，
    # 包括读不出 UTF-8、进不了内容扫描的那些 —— 这些都不需要读内容就能判出来。
    for path in [*files, *unreadable]:
        rel = str(path.relative_to(root))
        hits.extend(check_path_name(rel, denylist))
        hits.extend(check_av_name(path, rel))
    for path in files:
        hits.extend(scan_file(path, str(path.relative_to(root)), denylist))
    hits.extend(check_repo_denylist(root))
    venv_hits, venv_note = check_venv_tracked(root)
    hits.extend(venv_hits)

    print(f"发布闸门 · {root}")
    print(f"黑名单 {denylist_path.name}（{len(denylist)} 条）· 命中就逐条列在下面")
    print()
    for entry in hits:
        print(entry)
    if venv_note:
        print(f"提示：{venv_note}")
    if hits:
        print()
        print(f"不可发布：{len(hits)} 处命中，逐条见上（exit 1）", file=sys.stderr)
        sys.exit(1)
    print()
    print(f"可发布：0 处命中（扫了 {len(files)} 个会被提交的文本文件"
          + (f"，另有 {len(unreadable)} 个非 UTF-8 文件只查了文件名与扩展名"
             if unreadable else "")
          + "）")
    sys.exit(0)


if __name__ == "__main__":
    main()
