# reviewer2-cua

`nano-cua` 的**插件组 + 试验记录整合仓库**。

这个仓库只承载两层内容：

1. **插件组（plugin groups）**：`extensions/`，即 `nano-cua-extensions`；
2. **试验与试验记录（experiments / records）**：`experiments/`（双 Gate 评分器）与 `reports/`（实测汇总）。

CUA 基座 `nano-cua` 本身**不在本仓库内**，仍保持独立 Git 仓库和零运行时依赖；本仓库通过 uv workspace 指向同级目录的 `../nano-cua`。OmniJev 的模型权重/Flide 部署不在本仓库内，不随本仓库分发。

## 目录结构

```text
reviewer2-cua/
├── pyproject.toml              # uv workspace（根，virtual）
├── uv.lock
├── README.md
├── extensions/                 # 插件组：nano-cua-extensions
│   ├── pyproject.toml
│   ├── src/nano_cua_extensions/
│   │   ├── playwright_browser/ # Playwright 观察+执行组
│   │   ├── vlm/                # 通用 OpenAI-compatible VLMActor
│   │   └── demo_cli.py         # nano-cua-demo 组合入口
│   └── tests/
├── experiments/                # 试验：双 Gate / 评分器
│   ├── pyproject.toml
│   ├── src/cua_gates_pilot/
│   ├── scripts/
│   │   ├── benchmark_20.py             # 20 组安全/歧义行为
│   │   ├── benchmark_efficiency_20.py  # 20 组效率
│   │   └── omnijev_playwright_check.py # clear + ambiguous 断言
│   └── tests/
└── reports/                    # 试验记录
    ├── README.md
    ├── safety-evaluation-2026-10-27.md
    └── efficiency-evaluation-2026-10-27.md
```

## 依赖边界

| 组件 | 位置 | 与本仓库关系 |
| --- | --- | --- |
| CUA 基座 `nano-cua` | `../nano-cua`（独立 Git 仓库） | 只作为 uv path 依赖；**本仓库不修改基座** |
| 插件组 | `extensions/` | 可替换的 Observer/Actor/Executor 兼容层 |
| 双 Gate 试验 | `experiments/` | 只通过公开 `AgentPolicy` 接口插入，不改基座 |
| OmniJev/Flide 部署 | 独立部署目录（不随仓库分发） | 本仓库只用 HTTP 访问其 `/v1/systemone` |
| 试验记录 | `reports/` | 评测方法、结果表、限制与复现命令 |

## 快速开始

要求：Python 3.12+、[uv](https://docs.astral.sh/uv/)、同级目录存在基座 `../nano-cua`。

```bash
cd reviewer2-cua

# 根是 virtual workspace：必须显式同步所有成员与 dev group
uv sync --all-packages --all-groups

# 首次使用 Playwright 需要安装 Chromium
uv run playwright install chromium

# 全仓库测试（插件组 + 试验包）
uv run pytest -q
```

本仓库当前验证结果：**114 passed**（`extensions` 83 + `experiments` 31）。

## 插件组

`extensions/` 保留原来的 `nano-cua-extensions` 包名与导入路径：

- `nano_cua_extensions.playwright_browser`：`PlaywrightIO` 同时实现 `Observer` + `ActionExecutor`，包含浏览器动作白名单、参数校验、模型 system prompt 与输出 parser；
- `nano_cua_extensions.vlm`：动作空间无关的 `VLMActor`，system prompt/parser 由动作组注入；
- `nano-cua-demo`：MockActor 本地 demo；也可注入真实 VLM。

```bash
uv run nano-cua-demo                        # 本地页面 demo，无需 API Key
uv run nano-cua-demo --actor vlm            # 需要配置 OpenAI-compatible VLM
uv run pytest -q extensions/tests           # 只测插件组
```

## 试验（双 Gate 评分器）

`experiments/` 保留原来的 `cua-gates-pilot` 包名与导入路径。核心设计：

```text
初始 observations 为空 -> 强制 Observe
循环：
  Observe Gate      REUSE  -> Actor
                    OBSERVE-> Observe 后 Actor
  Actor             Finish -> 结束
  AskHuman Gate     PASS   -> 执行原候选
                    ASK    -> 人工澄清 -> 同一 Actor 重规划 -> 执行新候选
```

固定、非神经网络阈值：

```text
observe_score = 0.6 * info_missing + 0.4 * possibly_stale
OBSERVE       = observe_score >= 0.60

AskHuman 信号:
  intent_ambiguity = 1 - P(instruction identifies unique target)
ASK           = intent_ambiguity >= 0.65
```

`experiments/` 同时包含真实 OmniJev 适配器 `omnijev.py`（调用本机 `http://127.0.0.1:8000/v1/systemone`，无 OmniJev SDK 依赖）。

```bash
uv run cua-gates-pilot --mode gated                # 本地 MockOmniJev + MockActor demo
uv run cua-gates-pilot --mode gated --ask-gate off # 独立关闭 AskHuman Gate
uv run python experiments/scripts/benchmark_20.py             # 20 组安全/歧义行为对比
uv run python experiments/scripts/benchmark_efficiency_20.py  # 20 组效率对比
uv run python experiments/scripts/omnijev_playwright_check.py # 真实 OmniJev 的 clear/ambiguous 断言
uv run pytest -q experiments/tests                 # 只测试验包
```

> 真实 OmniJev 脚本要求本机 Flide 服务已启动；未启动时离线测试和 Mock demo 仍可运行。

## 试验记录

详见 [reports/README.md](reports/README.md)：

- [safety-evaluation-2026-10-27.md](reports/safety-evaluation-2026-10-27.md)
  - 20 组任务（10 clear + 10 ambiguous）的 baseline vs gated 安全/歧义行为；
  - AskHuman prompt 诊断与修复过程；
  - ASK 召回 9/10、clear 误 ASK 0/10（该组）、A5 漏触发；
  - 跨测试 E15 clear 假阳性。
- [efficiency-evaluation-2026-10-27.md](reports/efficiency-evaluation-2026-10-27.md)
  - 20 组 clear 任务的效率组；
  - 成功率、耗时、截图、OmniJev token、人工等待；
  - 固定开销结论与 E15 误触发成本。

这些记录均为**功能级对比**，不是正式 benchmark/安全保证；具体限制见各报告。

## 项目来源

本仓库整合了插件组与双 Gate 评分器试验的当前实现；包名、模块导入路径与 CLI 名称保持不变。模型权重、部署配置和实验原始日志不随仓库分发；复现所需的本地服务、结果与限制见各文档及 `reports/`。

## 明确不做

不训练/微调模型；不把模型权重、Docker 镜像或 OmniJev 部署放进本仓库；不做通用安全保证；不做正式多场景统计评测；不修改 CUA 基座。
