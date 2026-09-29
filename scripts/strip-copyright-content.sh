#!/usr/bin/env bash
# KPF 口语批改 skill · 题库版权内容摘除
#
# 干什么（一条命令做完三件事）：
#   1. 删掉 questions/*.txt（保留 questions/README.md）
#   2. 把文档里指向这些文件的路径统一改成 questions/example.txt
#   3. 生成占位题库 questions/example.txt（已存在则不动）
#
# 为什么要有它：questions/ 下的题干出自 Cambridge《FCE Trainer》，版权归
# Cambridge University Press & Assessment。仓库里只作格式示例；要公开发布
# 又不想带真题时，跑这一条命令即可。
#
# 用法：
#   bash scripts/strip-copyright-content.sh              # 先打印计划，再动手
#   bash scripts/strip-copyright-content.sh --dry-run    # 只看计划，一个文件都不动
#   bash scripts/strip-copyright-content.sh --root <仓库根>   # 指定仓库根（默认脚本的上一级）
#
# 幂等：重复跑不会出错；第二次会告诉你"已是干净状态"。
# 退出码：0 = 已完成或本就干净；2 = 用法错（路径不存在、参数不对）。
#
# 注意：本脚本不碰 git 索引。删掉的文件如果已提交，需要你自己
# `git rm questions/*.txt` 或 `git add -A` 之后提交——脚本只改工作区。
set -euo pipefail

ROOT_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${ROOT_DEFAULT}"
DRY=0

usage() {
  cat <<'TXT'
KPF 口语批改 skill · 题库版权内容摘除

  bash scripts/strip-copyright-content.sh [--dry-run] [--root <仓库根>]

  --dry-run, -n   只打印将要改什么，不写任何文件
  --root <目录>   仓库根目录（默认：本脚本所在目录的上一级）
  -h, --help      显示这段帮助
TXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run|-n) DRY=1; shift ;;
    --root)
      [ $# -ge 2 ] || { echo "错误：--root 后面要跟目录" >&2; exit 2; }
      ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "错误：不认识的参数「$1」" >&2; usage >&2; exit 2 ;;
  esac
done

[ -d "${ROOT}/questions" ] || { echo "错误：找不到题库目录 ${ROOT}/questions" >&2; exit 2; }
[ -f "${ROOT}/Makefile" ] || { echo "错误：${ROOT} 看起来不是本 skill 的仓库根（没有 Makefile）" >&2; exit 2; }

# 替换与复扫都在 python3 里做：文件名含中文与点号，用 sed 容易踩转义坑。
python3 - "${ROOT}" "${DRY}" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
dry = sys.argv[2] == "1"
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".ipynb_checkpoints"}
QUESTIONS = root / "questions"
KEEP = QUESTIONS / "README.md"
HELPER = QUESTIONS / "example.txt"
HELPER_TEXT = """\
# 你自己的口语题目（占位文件）
#
# 一行一题，纯文本 UTF-8；不要写编号，不要 Markdown 标记。
# 文件命名建议 <级别>-<Part>-<主题>.txt，例如 FCE-P1-music.txt。
# 用法：--questions questions/example.txt（或改成你自己的文件名）
#
# 请把你自己的题目一行一题写在这里（优先用你自己教材里的题目，或你自己编写的题目）。
"""


def documents():
    """会被改写的文档：全部 .md，加 scripts/*.py 与 config.example.json 里的用法示例。"""
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts[0] in SKIP_DIRS or any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        name = rel.name
        if name.endswith(".md") \
                or (rel.parts[0] == "scripts" and name.endswith(".py")) \
                or name == "config.example.json":
            out.append(path)
    return out


def substitute(text, names):
    """questions/<真题名> → questions/example.txt；裸文件名 → example.txt。"""
    hits = 0
    for name in names:
        old, new = f"questions/{name}", "questions/example.txt"
        hits += text.count(old)
        text = text.replace(old, new)
    for name in names:
        hits += text.count(name)
        text = text.replace(name, "example.txt")
    return text, hits


qfiles = sorted(p for p in QUESTIONS.glob("*.txt")
                if p.is_file() and p != HELPER)
names = [p.name for p in qfiles]

plan = []
for path in documents():
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    _, hits = substitute(text, names)
    if hits:
        plan.append((path, hits))

total = sum(hits for _, hits in plan)
clean = not names and not plan and HELPER.is_file()

print("== 动手前，先看清要改什么")
print(f"仓库根：{root}")
print()
print("1) 删除题库文件（保留 questions/README.md；如果你自己写的题目也放在这里，请先 --dry-run 确认）：")
if names:
    for path in qfiles:
        print(f"   - {path.relative_to(root)}")
else:
    print("   （没有可删的 .txt，可能已经跑过）")
print()
print("2) 把文档里的题库路径统一改成 questions/example.txt：")
if plan:
    for path, hits in plan:
        print(f"   - {path.relative_to(root)}（{hits} 处）")
    print(f"   合计 {total} 处，分布在 {len(plan)} 个文件")
else:
    print("   （没有需要改的引用）")
print()
print("3) 生成占位题库 questions/example.txt：")
print("   （已存在，保持不动）" if HELPER.is_file() else "   （将新建，内容写明「请把你自己的题目一行一题写在这里」）")
print()

if dry:
    print("（--dry-run：上面只是计划，一个文件都没动）")
    sys.exit(0)

if clean:
    print("== 已是干净状态：questions/ 下没有真题 .txt，文档里也没有指向它们的路径。无事可做。")
    sys.exit(0)

# 1) 生成占位题库（已存在则不动，幂等）
if not HELPER.is_file():
    HELPER.write_text(HELPER_TEXT, encoding="utf-8")
# 2) 改写文档
rewritten = 0
for path, _ in plan:
    text = path.read_text(encoding="utf-8")
    new_text, _ = substitute(text, names)
    if new_text != text:
        path.write_text(new_text, encoding="utf-8")
        rewritten += 1
# 3) 删除真题
removed = 0
for path in qfiles:
    path.unlink()
    removed += 1

print("== 做完了")
print(f"删掉 {removed} 个题库文件 · 改写 {rewritten} 个文档（共 {total} 处引用）· "
      f"占位题库 {HELPER.relative_to(root)}")

# 复扫：全仓还有没有残留的真题文件名
stale = []
for path in sorted(root.rglob("*")):
    if not path.is_file():
        continue
    rel = path.relative_to(root)
    if rel.parts[0] in SKIP_DIRS or any(part in SKIP_DIRS for part in rel.parts[:-1]):
        continue
    if path == HELPER:
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    for name in names:
        if name in text:
            stale.append(f"{rel}（还提到 {name}）")

print()
print("== 还需你手工检查的地方")
if stale:
    print("下列文件仍在文字里提到被删掉的真题文件名（脚本只改引用路径，不改叙述）：")
    for entry in stale:
        print(f"   - {entry}")
else:
    print("- 全仓已无真题文件名的残留引用（本次已复扫确认）。")
print("- SKILL.md / README.md / references/ 里的示例命令现在都指向 questions/example.txt，")
print("  请按你自己的题库改成实际文件名：脚本只改路径，不改命令里的其他参数。")
print("- scripts/*.py 的用法注释与 config.example.json 里的 questions/<页号>.txt 是占位写法，不用改。")
print("- 机械替换会在少数叙述句里留下重复，例如「已有 example.txt、example.txt」（原文是两条不同")
print("  真题）——读一遍顺手改掉即可。")
print("- 跑一次 `make check`：一致性校验的第 4 项（引用完整性）会确认 questions/example.txt 存在。")
print("- 被删掉的文件如果已提交过，脚本不会碰 git 索引，需要你自己 `git rm questions/*.txt` 后提交。")
PY

echo
echo "提示：make check 里的引用完整性断言会确认 questions/example.txt 存在；"
echo "      要核验整体没被改坏，跑一次：make check"
