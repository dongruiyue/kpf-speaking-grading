# KPF 口语批改 skill · 常用命令
#
# `make check` 是发布前必跑的那一条：py_compile + 规则一致性 + 脚本级回归 + 夹具回归 +
# 发布闸门，五条里任意一条红就红。全程离线、不需要任何 API key、秒级跑完（夹具是静态文本，
# 校验器只用标准库），所以 push 到 GitHub 上跑 CI 也不用装 faster-whisper 之类重依赖。

PY ?= python3
PY_FILES := $(wildcard scripts/*.py tests/*.py)

.PHONY: check py-compile consistency script-tests fixtures publish-check doctor setup clean

check: py-compile consistency script-tests fixtures publish-check  ## 发布前必跑（任一红就红）

py-compile:  ## 全部 Python 文件能编译（语法级）
	$(PY) -m py_compile $(PY_FILES)

consistency:  ## 代码与 references 不漂移（分制/阈值/维度/引用路径）
	$(PY) scripts/check_consistency.py

script-tests:  ## 脚本级回归：闸门拦音频 / 互动门槛 / 对齐不吞词 / 转写 schema
	$(PY) tests/test_scripts.py

fixtures:  ## 匿名夹具回归：退出码 + error/warning 计数对齐 tests/expected.json
	$(PY) tests/run_fixtures.py

publish-check:  ## 发布闸门：真实姓名/班号/个人路径/凭证/音视频名
	$(PY) scripts/check_publishable.py

doctor:  ## 环境与凭证自检（离线、不花额度）：发音分会从哪来、还差什么、下一步跑什么
	$(PY) scripts/kpf_doctor.py

setup:  ## 装运行环境（faster-whisper 等，只有本地跑转写/发音才需要）
	bash scripts/setup.sh

clean:  ## 清掉构建残留（不碰 .venv/）
	rm -rf scripts/__pycache__ tests/__pycache__
