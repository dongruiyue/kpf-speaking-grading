# 题库目录 `questions/`

`--questions` 参数指向这里：**纯文本、UTF-8、一行一题**。脚本按行切分，一行就是一道题。

## 现有内容与版权

本目录当前放着 **24 条题干，来自剑桥 FCE 备考教材（Trainer 系列）Speaking Part 1 的五页**：

- **P56**，音乐主题（5 题）
- **P102**，假期主题（5 题）
- **P125**，日常生活主题（5 题）
- **P143**，未来计划主题（4 题）
- **P179**，旅行主题（5 题）

（具体版本与册次请以你手上的教材为准——本仓库不逐版核对出处。）

文件名按 `FCE-P1-<页号>-<主题>.txt` 命名。

它们在本仓库里**只作格式示例**——让你看清"一行一题"长什么样、`--questions` 该喂什么。

> **版权归 Cambridge University Press & Assessment。** 本仓库不以任何形式主张这些题干的著作权，也不授权他人再分发。正式教学请使用购买的正版教材。
>
> **不想带着真题发布？一条命令摘除：**
>
> ```bash
> bash scripts/strip-copyright-content.sh --dry-run   # 先看会改什么，不动文件
> bash scripts/strip-copyright-content.sh             # 真删，并生成 questions/example.txt 占位
> ```
>
> 它会删掉 `questions/*.txt`（保留本文件）、把文档里指向这些文件的路径统一改成 `questions/example.txt`、并生成一个占位题库。跑两次也不会出错。

## 脚本为什么需要这个目录

讯飞 ISE（`scripts/kpf_xfyun.py`，以及 `scripts/kpf_pronounce.py --provider xfyun`）是**朗读型**语音评测接口：它必须拿到参考文本才能对齐、给出分数。

学生自问自答的整页作业里，**只有念题那部分有标准原文**（答题部分是学生自己说的话），所以评发音分时**必须给 `--questions`**。缺这个参数时，脚本会明确降级到下一个 provider 并在 stderr 说明原因——**不会静默给 0 分**。

## 自建题库的格式

- **一行一题**，纯文本 UTF-8；不要写编号、不要 Markdown 标记、不要空行夹杂。
- 文件命名建议 `<级别>-<Part>-<主题>.txt`，例如 `FCE-P1-music.txt`。
- 文本尽量贴近学生的念题（含标点）：脚本用滑窗相似度对齐，标点差异可以容忍，**缺词或改词会影响对齐**，进而影响逐题词数与"念题差异"告警。
- 一页一题就一个文件；不要把一个文件当天题库总表用。

从占位文件开始写自己的题库：

```bash
cp questions/example.txt questions/FCE-P1-my-topic.txt
# 然后一行一题填进去：
#   python3 scripts/kpf_analyze.py <转写.json> --questions questions/FCE-P1-my-topic.txt …
```

## 一句提醒

**优先用你自己教材里的题目，或你自己编写的题目。** 真题题干适合本机自用，不适合公开再分发——公开前请先跑上面那条摘除命令。
