# 03 · 使用方法：从零到第一份成品

> 上一篇：`02-mechanism.md`（机制）· 下一篇：`04-vs-human.md`（与人工批改对比）
> 本篇是"怎么用"的最短路径；完整命令与参数以 `README.md` 与 `SKILL.md` 为准。

## 一、谁适合用

**适合**：手上有剑桥五级（KET / PET / FCE）口语作业的音频或视频，要转写 → 按官方口径诊断 → 出「内部记录 / 家长版 / 学生版」三份反馈，并且希望**每次口径一致**。

**不适合**（先把话说清楚，别硬套流程）：

| 情况 | 为什么不行 | 换什么 |
|---|---|---|
| 要批写作 / 笔试作业 | 这四项维度只对口语有效 | 另找作文批改流程 |
| 要评互动交际，但录音里只有学生一个人 | 没有对手方就没有证据 | 标 `N/A` 或「待补」；要真评另排一次两两对话 |
| 只有文字稿、没有音频 | 发音维度连疑点定位都做不了 | 只评语法与词汇、话语组织；发音标「待补」并说明原因 |
| 要预测官方考试成绩 | 口语只占考试一部分，原始分推不出总分 | 明说无法预测；只在覆盖全部 Part 的完整模拟里给参考判定 |
| 要判断是不是本人说的 | 不做声纹 / 身份判断 | 说明做不到，交老师处理 |

## 二、三步上手

```bash
# ① 装环境（一次性；需要 uv，脚本会建 .venv 并预下载模型，首次约 1.6GB）
cd <repo>
bash scripts/setup.sh

# ② 配凭证（可跳过——不配也能跑完整流程，只是发音分要老师手填）
mkdir -p ~/.kpf-speaking
cp config.example.json ~/.kpf-speaking/config.json   # 编辑填空
make doctor        # 离线自检：凭证齐不齐、发音分会从哪来、下一步跑什么（不联网、不花额度）

# ③ 试跑：不需要音频、不需要任何 key，验证闸门是活的
make check
```

`make doctor` 是排错的第一站：它会告诉你三件事——发音分的来源（讯飞 / 本机疑点 / 老师手填）、题库与环境是否就绪、下一步该跑哪条命令。它**不联网**，所以"服务是否已在控制台开通"这个前提它验不了，需要按 `docs/xfyun-setup.md` 用 1 句试一次（消耗 1 次额度）。

## 三、一次完整批改（六步）

```bash
# ① 转写（本地引擎：免费无限，音频不出本机）
.venv/bin/python scripts/kpf_asr.py transcribe <文件或文件夹> \
    --student <姓名> --class <班级> --level FCE --engine local --out work/<日期>-<学生>/
# → work/<日期>-<学生>/<文件名>--local.json

# ①′ 可选：交叉验证发音疑点（crosscheck 只接受恰好 2 个 json，多给会报参数错误）
.venv/bin/python scripts/kpf_asr.py transcribe <文件> --engine groq --out work/<日期>-<学生>/
.venv/bin/python scripts/kpf_asr.py crosscheck \
    work/<日期>-<学生>/<文件名>--local.json work/<日期>-<学生>/<文件名>--groq.json \
    --out work/<日期>-<学生>/交叉验证.md

# ② 切问答 + 算硬指标 + 出批改底稿
.venv/bin/python scripts/kpf_analyze.py work/<日期>-<学生>/<文件名>--local.json \
    --questions questions/FCE-P1-P102-holidays.txt \
    --student <姓名> --class <班级> --level FCE \
    --out work/<日期>-<学生>/<学生>-底稿.md

# ③ 发音评分（auto = 讯飞 → 本机疑点 → 老师手填，任一成功即用）
.venv/bin/python scripts/kpf_pronounce.py work/<日期>-<学生>/<文件名>--local.json \
    --provider auto --questions questions/FCE-P1-P102-holidays.txt \
    --out work/<日期>-<学生>/<学生>-发音.md

# ④ 生三份成品（三份都要跑一遍，--form 决定互动交际的措辞）
.venv/bin/python scripts/kpf_report.py <底稿.md> --kind 作业记录 --form 自问自答 \
    --homework "<作业名>" --date <YYYY-MM-DD> --out work/<日期>-<学生>/<学生>-作业记录.md
.venv/bin/python scripts/kpf_report.py <底稿.md> --kind 家长 --form 自问自答 \
    --homework "<作业名>" --date <YYYY-MM-DD> --out work/<日期>-<学生>/<学生>-家长.md
.venv/bin/python scripts/kpf_report.py <底稿.md> --kind 学生 --form 自问自答 \
    --homework "<作业名>" --date <YYYY-MM-DD> --out work/<日期>-<学生>/<学生>-学生.md

# ⑤ 归档：三份成品 + 底稿 + 发音报告 + 转写 JSON 都留在同一个作业目录；
#    （可选）把成品归进自己的笔记库，见 references/05-output-and-vault.md 第二节

# ⑥ 交付前必须跑，三份都跑（有 error 就重做报告，不是改校验器）
python3 scripts/kpf_validate.py work/<日期>-<学生>/<学生>-家长.md   --kind 家长     --form 单篇 --level FCE
python3 scripts/kpf_validate.py work/<日期>-<学生>/<学生>-学生.md   --kind 学生     --form 单篇 --level FCE
python3 scripts/kpf_validate.py work/<日期>-<学生>/<学生>-作业记录.md --kind 作业记录 --form 单篇 --level FCE

# ⑥′ 防编造：把转写稿一起给进去，报告里引用的学生原句必须找得到
python3 scripts/kpf_validate.py work/<日期>-<学生>/<学生>-家长.md --kind 家长 --form 单篇 --level FCE \
    --transcript work/<日期>-<学生>/<文件名>--local.json
```

> 校验器只用 Python 标准库，**系统 `python3` 直接跑**，不必进 `.venv`；离线、不联网、不用凭证。
> 有 error → 退出码 1；用法错误（如给家长版加 `--draft`）→ 退出码 2。

## 四、零配置最小路径

一个 key 都不配，这套流程也能跑完：

- 转写走本机 whisper（免费、隐私）；
- 发音那一维降级为"本机疑点表"（**只有疑点，没有分**）；
- 家长版里的发音分**由老师自己填**，填完照旧过校验器。

也就是说：**配置决定的是"发音那一项谁给分"，不决定流程能不能跑。**

## 五、讯飞：唯一必须去控制台动手的部分

完整指引（逐步截图级说明、报错对照表、额度怎么省）在 `docs/xfyun-setup.md`。要点三条：

1. 注册 → 实名认证 → 建应用。
2. **在控制台先"开通"语音评测服务**——不开通，接口会直接报错。这一步是最常见的失败点。
3. 把 `appid / api_key / api_secret` 抄进 `~/.kpf-speaking/config.json` 的 `xfyun` 块。

另外两条口径（容易踩）：

- 评测的是**念题部分**——朗读型接口必须有参考文本，所以跑的时候**必须给 `--questions`**，一行一题。
- 额度：一次 = 一句，一页 5 题 = 5 次。
- 有两个讯飞引擎，**分工不同、绝不可混用出分**：`suntone`（声通）口径与剑桥一致（重韵律、语调、可理解性，可限定英式），作**主评分**；"流式版 ISE"独有乱读拒识与音频质量诊断，但按"无中式口音"打分、**与剑桥理念不符、会系统性压低分数**，只作**质检**。同一段录音两者总分可差 18 分之多。

## 六、每次批改的操作顺序（分工清单）

**机器先跑（第 1–4 步）**，然后人做下面五件事，最后过闸门：

1. **判断作业形态**：自问自答 / 独白 / 含对手方的对话——它决定哪几项标 `N/A`。判断不清就先问，别猜。
2. **图片类题型先索要图片**（描述照片、两图对比、看图说话）。拿到图之前，**不得**评"描述是否完整 / 内容是否切题 / 有没有说错"，只能评语言层面。
3. **改措辞**：骨架给的是证据和结构，最后一段"接下来重点 / 回家怎么配合"必须自己写。
4. **决定互动交际**：有对手方才有这一项；独白一律 `待补` 并写明原因。
5. **过闸门**：三份都跑校验器，有 error 就回去改报告。

## 七、常见坑

- **模型必须 `large-v3-turbo` 或更大**：`tiny` 对学习者语音的识别质量不足以支撑题目对齐（对齐度 0%）。
- **网络两坑**：系统代理指向未启动的代理程序会让模型下载超时；国内直连模型站点也会超时。脚本已默认绕过代理并走镜像，可用环境变量覆盖。
- **A2 Key 记错维度**：A2 只有语法与词汇、发音、互动交际三项，**没有话语组织**；硬造一项就是错的（校验器会拦）。
- **别把"没展开"和"答错"混为一谈**：答得短是话语组织问题，答非所问是切题问题，扣分位置不同。
- **念题差异告警与单题词数不得单独下结论**：交界处的"多读 X"多数是上一题答案的尾部；要判定朗读错误，回到音频听一遍。
- **跨批次对比必须同引擎**：同一段录音，换引擎答题词数能差 8%、停顿数差 67%。
- **改正版逐项核验**：用同一套脚本重跑，逐条看"上次反馈是否落地"，价值高于新批一份。

## 八、两种用法

- **当 agent skill 用**：把本仓库放进 skills 目录，之后直接丢音频说"批改口语作业"，由 agent 按 `SKILL.md` 驱动脚本。
- **当纯命令行工具链用**：不用 agent，自己按上面第三节逐条敲；所有脚本都能独立运行，产物是纯 markdown 与 JSON。

两种用法共用同一套脚本、同一套规则、同一组闸门——**换调度者不换口径**。

下一步建议：读 `04-vs-human.md` 看它和人工批改各自负责什么。
