# 贡献指南

这是一套给英语老师用的口语批改流程，加上一整套把"口径"钉死的脚本。改规则、改阈值、改校验器之前，请先读这一页。

## 一、规则改动的纪律（最重要的一条）

**禁止"追加新章节 + 声明效力更高"式打补丁。**

这个项目曾经被这种写法搞出过 **14 处自相矛盾**：同一个规则在 `SKILL.md`、`references/02-rubric.md`、`references/04-feedback.md` 里各有一套说法，新章节声明"以本节为准"，旧段落却还留在文件里；后来的人读到哪一段，就按哪一段执行。

所以：

1. **物理删除旧段落**。不允许用"取代此前"、"效力高于"、"优先于本文件"、"已作废"这类声明把矛盾留在仓库里——`scripts/check_consistency.py` 的第 5 项断言会扫这几个词，命中就红。
2. **节号重排连续**。`references/*.md` 的一级节号必须是 `## 一、## 二、## 三…` 连续（第 6 项断言）。删掉一节就重排后面的序号，不要留空号。
3. **一个数值只有一个文本落点**。官方分制与「单篇 / 完整模拟」口径以 `references/02-rubric.md` 为跨文件冲突的最终裁定；校验器数值的唯一落点是 `references/06-verification.md` §8.4 的锚点表。
4. **门槛必须逐级别写**。`references/02-rubric.md` 的档位表是**按级别分的**（A2 / B1 / B2 三张量表本就不同），所以**说明文字里不许出现跨级别的门槛结论**——像"回答超出短句就是 3 档""只把简单句说好，顶多到 N 档"这种一句话概括，**必然对某一级是错的**（这个坑连续踩了四轮，每轮都是同一形态：表格改对了，说明文字没跟着分）。写法只有两种：**逐级别写明**（`B1：…；B2：…`），或者**删掉那句、只留"按上面表格逐项对照"**。注意 `check_consistency.py` 第 10 项断言**只锁表格结构，锁不住说明文字**——这一条只能靠人守。

**写/改任何门槛句之前，逐项特征回表核对级别**：把这句话断成"特征 × 级别"逐个查对应的档位表（references/02-rubric.md 各维度的分级表）——凡出现一个"该特征其实只属于某一级（或某一维度在某级根本不存在）"的，就必须拆成按级别写，或删掉只留"按上面表格对照"。这一步专门堵"用一句话概括四个维度的 5 档要求"这类错（例：把 B1/B2 的"尝试复杂结构、成段、主动发起推进"当成**通用**的 5 档条件写，会误压 A2——A2 没有话语组织这一维度、A2 的 5 档也不要求复杂结构与主动发起）。机械断言（`check_consistency.py` 第 10 项）只锁表格结构，**锁不住说明文字，这一步只能靠人做**。

## 二、改了任何阈值 / 数值，必须同时改锚点表

`references/06-verification.md` §8.4 是**校验器数值的唯一文本落点**：`scripts/check_consistency.py` 会从那张表**反解数值**，再去比对代码里的常量与文档里的说法。

因此改任一项，必须在**同一次提交**里改三处：

1. **代码常量**——例如 `scripts/kpf_xfyun.py` 的 `BANDS`、`scripts/kpf_validate.py` 的 `QUOTE_ERROR` / `QUOTE_PASS` / `TECH_PARENT_HARD` / `TECH_PARENT_COND` / `DIM_ALIASES`、`scripts/kpf_report.py` 的 `DIMENSIONS`；
2. **§8.4 锚点表的对应行**；
3. **引用该数值的文档**——`references/02-rubric.md`、`references/04-feedback.md`、`references/checklist.md`。

漏掉第 2 步就是"漂移"：`make check` 会打印两边取值并 exit 1。

## 三、提交前 `make check` 必须全绿

四道闸门：

```bash
make check      # py_compile + check_consistency.py + 夹具回归 + check_publishable.py
```

新行为或行为变更，**必须补一个匿名夹具**：

1. 在 `tests/fixtures/` 加一个静态 `.md`（正例、反例都行）；
2. 更新 `tests/expected.json`（`kind` / `form` / `level` / `transcript` / `draft` / `exit` / `errors` / `warnings`）；
3. **期望值必须实跑后回填**——先跑 `python3 scripts/kpf_validate.py <夹具> …`，把真实输出抄进期望表。不许猜，也不许"按理说应该是 0"。

`tests/run_fixtures.py` 会把每个夹具的退出码与 error/warning 计数跟期望表逐条比对，对不上就是 FAIL。

## 四、夹具的匿名纪律

- 夹具里**不许出现真实学生姓名、班级、音频或视频**。
- 用 `scripts/kpf_anonymize.py` 生成：

  ```bash
  python3 scripts/kpf_anonymize.py <真实case目录> --out <产物目录> --dry-run   # 先看会替换什么
  python3 scripts/kpf_anonymize.py <真实case目录> --out <产物目录>
  ```

  它替换姓名 / 班号 / 绝对路径 / 音视频文件名，**保留英文原句与分数**（夹具的价值就在错误类型与数据形态），写完自动复扫一遍，有残留就 exit 1。
- 生成后自己再读一遍产物。`--allow` 只用来豁免启发式的误报，**不要拿它压掉真残留**。
- 脱敏规则与具名检查见 `scripts/check_publishable.py`；黑名单在 `scripts/publishable-denylist.txt`（该文件本身不被扫描——它装的就是要拦的词）。

## 五、本地真实数据的存放约定

真实学生数据**永远不进任何 git 仓库**，固定放在仓库外：

| 内容 | 位置 | 是否提交 |
|---|---|---|
| 一次批改的中间产物与成品 | `work/<日期>-<学生>/` | ❌（`.gitignore` 已挡） |
| 校准语料（真实 case） | `~/Documents/口语批改工作区/_calibration/<日期>-<学生代号>/` | ❌（仓库外，见 `docs/calibration.md`） |
| 音频 / 视频 | 同上，`audio/` 子目录 | ❌ |
| 转写 JSON | `work/<日期>-<学生>/<文件名>--<引擎>.json`（另见 `.gitignore` 的 `*.local.json`） | ❌（整个 `work/` 被忽略） |
| 凭证 | `~/.kpf-speaking/config.json` | ❌（`.gitignore` 已挡） |

兜底有两道：`.gitignore` 与 `make check` 里的发布闸门（`scripts/check_publishable.py`）。`git add` 之前先跑一次 `make check`。

## 六、提交信息建议

```text
<类型>: <一句话说明改了什么>

类型：规则 / 校验 / 脚本 / 文档 / 夹具 / 校准

若动了阈值或数值，正文里写明：
  依据：<哪几份真实 case / 哪条实测输出>
  一致率：因 N 个真实样本，±1 档一致率 X% → Y%（若适用）
  同步：代码常量 + references/06-verification.md §8.4 锚点表 + 相关文档
```

示例：`规则: 收紧语法与词汇 1 档锚点（因 5 个真实样本，±1 档一致率 72% → 86%）`

## 七、校准闭环

改"判定口径"的正确姿势是**先攒真实 case、跑出一份一致率报告、归纳出一个系统性偏差**，再改锚点——**不要逐案打补丁**。完整流程（语料怎么攒、指标怎么读、报告怎么用、真实 case 怎么变成可公开夹具）见 `docs/calibration.md`。
