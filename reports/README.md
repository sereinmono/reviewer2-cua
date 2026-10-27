# 试验记录索引

本目录保存 `reviewer2-cua` 的评测记录。所有记录都是**功能级对比**，不是正式 benchmark、统计检验或安全保证。

| 记录 | 内容 | 来源脚本 |
| --- | --- | --- |
| [safety-evaluation-2026-10-27.md](safety-evaluation-2026-10-27.md) | 20 组任务（10 clear + 10 ambiguous）的 baseline vs gated 安全/歧义行为；AskHuman prompt 诊断与修复；ASK 召回、误触发、A5 漏触发、E15 跨测试假阳性 | `experiments/scripts/benchmark_20.py`、`experiments/scripts/omnijev_playwright_check.py` |
| [efficiency-evaluation-2026-10-27.md](efficiency-evaluation-2026-10-27.md) | 20 个 clear 任务的效率组：成功率、耗时、截图、OmniJev token、人工等待、固定开销 | `experiments/scripts/benchmark_efficiency_20.py` |

## 数据来源与注意事项

- 安全组数据：20 任务 × 2 模式，共 40 个 episode，各 1 次；
- 效率组数据：20 任务 × 2 模式，共 40 个 episode，各 1 次；
- 使用确定性 `MockActor` / `TaskActor`、`MockHumanProvider`、本地手工构造页面、本地 OmniJev OneJev1.6-0.8B；
- 单次运行、无重复测量、无统计显著性检验；
- 不包含模型权重、Docker 镜像或部署配置；OmniJev/Flide 部署不在本仓库内。

## 复现环境

```powershell
cd reviewer2-cua
uv sync --all-packages --all-groups
uv run playwright install chromium
uv run pytest -q
# 以下命令需要本机 OmniJev/Flide 服务:
uv run python experiments/scripts/benchmark_20.py
uv run python experiments/scripts/benchmark_efficiency_20.py
uv run python experiments/scripts/omnijev_playwright_check.py
```

细节、逐任务表和限制见两份报告。
