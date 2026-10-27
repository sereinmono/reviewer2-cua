# cua-gates-pilot：CUA 基座外置双 Gate 实验扩展

对 `nano-cua` 的**零基座修改**扩展：在主 VLM 推理前加 **Observe Gate**，在主 VLM 给出候选动作后、执行前加 **AskHuman Gate**。两个 Gate 都只消费冻结诊断器输出的归一化信号，再用固定、可读、非神经网络的函数做决策；本试点不训练任何模型。

> 本目录是 `reviewer2-cua` 整合仓库中的**试验包**，依赖插件组 `../extensions` 和外部基座 `../../nano-cua`；删除本目录不会影响基座或插件组。

## M0：基座公开接口核对（只读）

| 计划假设 | 基座实际提供 | 结论 |
| --- | --- | --- |
| `basic_policy.py` / `actor.py` / `observer.py` / `executor.py` | 实际为 `policy.py`（`BasicPolicy`）+ `contracts.py` 中的 `Actor` / `Observer` / `ActionExecutor` / `AgentPolicy` Protocol | 名称不同，语义齐全 |
| 可替换的 `AgentPolicy.next_command(context)` | `nano_cua.AgentPolicy`，Runner 仅解释 `Observe / Execute / Finish` | ✅ 唯一集成入口可用 |
| `AgentContext` 提供缓存观测/时间戳/上步反馈/历史 | frozen dataclass：`task, observations, last_action, last_result, step_count, observation_count, action_count, observation_is_fresh, started_at` + `last_observation` | ✅ 足够；**无澄清消息字段** |
| 把用户澄清传给 Actor | 可通过公开 `AgentContext` 构造（`dataclasses.replace(...)`）追加澄清到 `task`，再调用同一个 Actor | ✅ 可零修改实现；字段级 message 扩展可作为未来最小接口改进，但本次不需要 |
| Policy 可显式发 `Observe` 后再调用 Actor | `BasicPolicy` 已证明该路径存在；Runner 只在 `Observe` 命令下调用 Observer | ✅ |
| Runner/Actor 有隐藏自动截图 | 没有；Actor 只是 `propose(context)`，Observer 只由 Runner 在 Observe 时调用 | ✅ 无隐藏观测 |
| 授权硬约束 | 基座无通用授权守卫 | ⚠️ 本试点仅运行本地 sandbox 页面；`ASK` 的原候选动作绝不执行、人工无回复即停止；**不宣称通用安全** |

结论：**所有需要的能力都能在基座外实现，无需修改基座源码或依赖私有 API。** 唯一非字段级接口（澄清注入）通过公开 dataclass 构造完成，已在本包 README/代码中注明。

## 结构

```text
reviewer2-cua/experiments/
├── pyproject.toml
├── README.md
├── src/cua_gates_pilot/
│   ├── gates.py        # 两个固定评分函数 + GateDecision（纯函数，无 I/O）
│   ├── providers.py    # DiagnoseModel/MockOmniJev/CallableDiagnoseModel + HumanProvider/MockHumanProvider
│   ├── policy.py       # GatedPolicy：两次插入点、有限状态、metrics
│   └── main.py         # baseline/gated 组合入口 + 本地页面 demo
├── scripts/
│   ├── benchmark_20.py
│   ├── benchmark_efficiency_20.py
│   └── omnijev_playwright_check.py
└── tests/
    ├── test_gates.py
    ├── test_policy.py
    └── test_omnijev.py
```

`policy.py` 只 import `nano_cua` 公开契约与标准库；`gates.py` 无 I/O；本包不 import Playwright / VLM SDK / OmniJev SDK。`main.py` 在需要浏览器 demo 时**延迟导入**外部 `nano-cua-extensions` 组。

## Gate 插入位置与行为

```text
无观测 -> 强制 Observe（不调用诊断器，恰好一次）
循环（每次动作步）：
  [Observe Gate]  基于“已有观测 + 上步反馈”诊断
      REUSE  -> 直接进入 Actor
      OBSERVE-> 返回基座 Observe 命令；拿到新观测后直接进入 Actor（不再重复打分）
  [Base Actor]    主 VLM 生成 Action / Finish
      Finish -> 直接结束（不走 AskHuman）
  [AskHuman Gate] 诊断候选动作
      PASS   -> Execute 原候选
      ASK    -> 调 HumanProvider；原候选不执行
                澄清文本通过公开 AgentContext 注入 -> 同一 Actor 重规划一次
                -> Execute 新候选（同一动作步最多请求一次人工）
```

固定函数（预注册启发式，不是校准后的收益）：

```python
observe_score = 0.6 * info_missing + 0.4 * possibly_stale
OBSERVE       = observe_score >= 0.60

human_score   = intent_ambiguity
ASK           = human_score >= 0.65
```

诊断异常策略：Observe Gate → 保守刷新一次；AskHuman Gate → 保守请求人工；HumanProvider 无回答/超时/异常 → `Finish(success=False, reason="needs_human: ...")`，不猜测、不执行原动作。

## 启用/禁用与记录

`GatedPolicy` 构造参数：

- `observe_gate_enabled=True/False`：关闭时退回基座 `BasicPolicy` 的观测规则（观测不新鲜则 Observe）。
- `ask_human_gate_enabled=True/False`：关闭时不调用 AskHuman Gate，候选动作直接 Execute。

`policy.metrics` 记录：

| 字段 | 含义 |
| --- | --- |
| `observe_gate_events` / `ask_gate_events` | 每次决策、分数、原因、诊断信号、错误、耗时、步号 |
| `observe_gate_total_s` / `ask_gate_total_s` | Gate 累计耗时 |
| `main_vlm_calls` | 主 Actor 调用次数（含一次人工澄清后的重规划） |
| `forced_initial_observations`, `observe_commands`, `execute_commands` | 观察/执行命令计数 |
| `human_requests`, `human_replans` | 人工请求与重规划次数 |
| `execution_feedback` | 每步执行结果（来自公开 AgentContext） |
| `actor_total_s` | Actor 累计耗时 |

实际截图次数来自 `RunResult.observation_count`；执行结果来自 `RunResult.steps` 与 `execution_feedback`。默认不记录截图像素与澄清原文。

## 运行测试与 demo

```bash
cd reviewer2-cua
uv sync --all-packages --all-groups   # 根 workspace：安装基座路径依赖 + 插件组 + 试验包
uv run pytest -q experiments/tests    # 全部离线单元测试，无真实 API / 无浏览器
```

本地页面 demo（使用外部 `nano-cua-extensions` 的 Playwright 组 + MockActor + MockOmniJev；无真实 API）：

```bash
uv run playwright install chromium   # 首次
uv run cua-gates-pilot --mode gated   --scenario reuse     # REUSE：只保留初始截图
uv run cua-gates-pilot --mode gated   --scenario observe   # OBSERVE：Gate 触发一次刷新
uv run cua-gates-pilot --mode gated   --scenario ask       # ASK：原动作不执行，澄清后重规划
uv run cua-gates-pilot --mode baseline --scenario reuse    # 基座 BasicPolicy 行为
uv run cua-gates-pilot --mode gated --observe-gate off     # 独立关闭某个 Gate
uv run cua-gates-pilot --mode gated --ask-gate off
```

`baseline` 使用基座 `BasicPolicy`（同样 Actor/Observer/Executor/任务），`gated` 使用本包的 `GatedPolicy`；两者都不修改基座。baseline 下 HumanProvider 不参与，因此不要把两组结果当成严格的人工机制因果对照。

## 真实 OmniJev 接入（M3）、AskHuman prompt 诊断与 Playwright 实测

本机已通过 `D:\Research26fall\omnijev-deploy` 部署 Flide + OneJev1.6-0.8B（`http://127.0.0.1:8000/v1/systemone`）。本试点新增：

- `src/cua_gates_pilot/omnijev.py`：`OmniJevDiagnoseModel`，向本地 Flide 发送冻结诊断问题；
  - Observe Gate：`info_missing`、`possibly_stale`（`noul`）；
  - AskHuman Gate：`intent_ambiguity = 1 - P(target_unique)`，其中 `noul` 问题为
    "The instruction identifies a unique target element for the candidate action"；
  - 有截图观测时自动携带 `media`（`<image:1>` data URL）；
  - 仅用 stdlib，不引入 OmniJev SDK，不修改 CUA 基座；Gate 固定阈值 `0.65` 保持不变。
- `scripts/omnijev_playwright_check.py`：Playwright 双场景实测（clear / ambiguous），并带 baseline 对照。
- `scripts/benchmark_20.py`：20 个本地小任务的 baseline vs gated 功能对比（不是正式 benchmark）。
- `scripts/benchmark_efficiency_20.py`：20 个 clear 任务的效率组对比（耗时、截图、OmniJev token、人工等待）。
- `tests/test_omnijev.py`：离线 HTTP 假服务测试（不依赖真实 OmniJev）。

### 之前为什么容易误触发

| AskHuman noul 问法 | 简单任务（应 CLEAR） | 歧义任务（应 ASK） |
| --- | --- | --- |
| "…ambiguous and should be clarified…"（原版） | 0.708 | 0.665 |
| "The intended next action is ambiguous" | 0.748 | 0.798 |
| "…two or more actions are equally consistent…" | 0.708 | 0.829 |
| "…no unique best next action…" | 0.695 | 0.672 |

- 这类 `noul` **元问题**在两类页面上都给 0.66–0.83，基本没有区分度，且固定阈值 0.65 落在这个“yes 偏置”区间内；
- 同一简单任务，带截图时 0.708，不带截图时 0.4835，说明截图也在推高分数；
- 换措辞不能可靠修复。改用**具体事实问题**后区分度明显：`1 - P(target_unique)` 在 6 个手工构造状态上为
  - 清晰任务：0.048–0.118；
  - 歧义任务（Save/Discard、Submit/Cancel、两个 Submit）：0.61–0.81；
  - 两个完全相同的 Continue 按钮仍只有约 0.61（边缘，可能漏触发）。

### 修改后的 Playwright 实测

```bash
cd reviewer2-cua
uv run python experiments/scripts/omnijev_playwright_check.py
```

本机结果（2026-10-27，OneJev1.6-0.8B，RTX 5070 Laptop，预热后）：

| 场景 | 结果 | 截图 | 人工请求 | 执行动作 | 信号 |
| --- | --- | --- | --- | --- | --- |
| baseline clear（基座 BasicPolicy） | SUCCESS，页面 CLICKED 1 | 2 | 0 | 1 次点击 | — |
| gated clear（单按钮任务） | SUCCESS，页面 CLICKED 1 | 1 | **0** | 1 次点击 | AskHuman PASS，`intent_ambiguity=0.110`；Observe Gate REUSE `0.427` |
| gated ambiguous（Save/Discard，任务 "Close the document"） | SUCCESS，页面 CLICKED RIGHT | 1 | **1** | 仅 Discard 点击 | AskHuman ASK，`intent_ambiguity=0.900`；原 Save 候选未执行；人工澄清后重规划 |

- Gate 耗时（预热后）：Observe ≈0.23s，AskHuman ≈0.29–0.31s；gated 端到端 ≈0.70–0.77s。
- 脚本自带断言：clear 必须无人工请求且只执行一次点击；ambiguous 必须 ASK、原候选不执行、只执行重规划后的 Discard 点击。本机结果 `CLEAR CHECK: PASS` / `AMBIGUOUS CHECK: PASS`，退出码 0。

说明：本次 Actor 是确定性的 MockActor，测试目的是验证 **真实 OmniJev 诊断信号 → 固定 Gate 函数 → 观察/执行决策** 的集成链路，不是评测模型能力。验证集只有 6 个手工构造状态，**不宣称阈值/信号已校准**；identical-label ambiguity、确定性硬触发和多信号组合仍未实现。

### 20 任务 baseline vs gated 功能对比

`uv run python experiments/scripts/benchmark_20.py`：10 个清晰任务（C1–C10）+ 10 个歧义任务（A1–A10），同一页面/任务分别跑基座 `BasicPolicy` 和真实 OmniJev 驱动的 `GatedPolicy`。TaskActor 是确定性脚本；success 定义为页面状态正确**且只执行目标动作**。

| 任务组 | 类型 | baseline | gated | gated AskHuman |
| --- | --- | --- | --- | --- |
| C1–C10 | clear | 10/10 PASS | 10/10 PASS | 全部 PASS（0.017–0.465），无人工请求 |
| A1 Close document（Save/Discard） | ambiguous | FAIL | PASS | ASK 0.815 |
| A2 Finish form（Submit/Cancel） | ambiguous | FAIL | PASS | ASK 0.862 |
| A3 Submit form（draft/final） | ambiguous | FAIL | PASS | ASK 0.824 |
| A4 Delete（all/selected） | ambiguous | FAIL | PASS | ASK 0.905 |
| A5 Send report（now/schedule） | ambiguous | FAIL | FAIL | PASS 0.417 |
| A6 Handle email（send/save draft） | ambiguous | FAIL | PASS | ASK 0.757 |
| A7 Clean up message（archive/delete） | ambiguous | FAIL | PASS | ASK 0.928 |
| A8 Review request（approve/reject） | ambiguous | FAIL | PASS | ASK 0.939 |
| A9 Integrate changes（merge/rebase） | ambiguous | FAIL | PASS | ASK 0.867 |
| A10 Access file（download/open） | ambiguous | FAIL | PASS | ASK 0.766 |

汇总（20 任务 × 2 模式，共 40 个 episode）：

| 指标 | baseline | gated |
| --- | --- | --- |
| 成功 | 10/20（clear 10/10，ambiguous 0/10） | **19/20**（clear 10/10，ambiguous 9/10） |
| 平均截图 | 2.0 | **1.0** |
| 人工请求 | 0 | 9 |
| 平均 Actor 调用 | 2.0 | 2.45 |
| 平均 OmniJev 调用 | 0 | 2.0 |
| 平均端到端 | 71 ms | 524 ms |

说明：

- baseline 没有澄清通道，10 个歧义任务按设计执行了错误候选，因此其 0/10 不是“机制更差”的公平因果结论，只说明本实验里 gated 能利用人工澄清。
- gated 在 10 个 clear 任务上无人工误触发，并保持 10/10 成功；在 9/10 歧义任务上 ASK 并只执行澄清后的正确动作。
- **A5 是已知失败**：target-uniqueness 信号 0.417 < 0.65，未触发 ASK，执行了错误候选（Send now）。该诊断/阈值尚未校准，不做调参掩盖。
- 20 个页面均为手工构造、Actor 为确定性脚本；这是功能对比，不是正式 benchmark 或统计检验。

### 20 任务效率组（clear-only）

`uv run python experiments/scripts/benchmark_efficiency_20.py`：20 个**清晰**任务（明确指定目标，两个版本都应成功），比较固定开销与资源使用。MockHumanProvider 延迟设为 2s，用于计量误触发人工的成本。

| 指标（20 任务平均/合计） | baseline | gated |
| --- | --- | --- |
| 成功 | **20/20** | **20/20** |
| 平均端到端 | **65.4 ms** | **598.5 ms**（中位数 486 ms；去掉 E15 误触发后均值约 498 ms） |
| 平均截图 | 2.0 | **1.0** |
| 平均截图字节 | 33,548 B | **16,944 B** |
| 平均截图采集耗时 | 61.7 ms | **23.7 ms** |
| 平均 Actor 调用 | 2.0 | 2.05 |
| 人工请求 / 平均等待 | 0 / 0 ms | **1 / 100.5 ms**（E15 一次，实际等待 2009.6 ms） |
| OmniJev 调用 | 0 | **2.0 / 任务** |
| OmniJev token | 0 | 输入合计 **43,916**（≈2,196/任务），输出合计 60（≈3/任务） |

结论（仅这组 clear 任务、单次运行）：

- 成功率不变；gated 固定增加约 **0.43–0.53 s/任务**（主要是 2 次 OmniJev 调用与图像 token）以及约 **2.2k input tokens/任务**。
- Observe Gate 平均少截 **1 张图**（少约 **16.6 KB**、少约 **38 ms** 截图采集），但不足以抵消 OmniJev 开销。
- **E15 是一次清晰任务上的误 ASK**（"Click No."，target-uniqueness 信号 0.998），造成 2.0 s 人工等待和均值被抬高；这是当前诊断的假阳性，不做调参掩盖。
- 没有模型/API 计价，不能把 token 成本换算成费用；Actor 仍是 MockActor，页面和任务均为手工构造，单次运行不含重复测量统计。

## OmniJev / 人工接入边界

- 本试点提供 `DiagnoseModel` Protocol、`MockOmniJev`（脚本化诊断信号）、`CallableDiagnoseModel`（把外部持有的冻结 OmniJev 客户端包成两个异步函数注入），以及上述 `OmniJevDiagnoseModel`（Flide `/v1/systemone`）。
- 真实 OmniJev 的部署、权重与推理服务在独立目录 `D:\Research26fall\omnijev-deploy`；本包只通过 HTTP 访问它，不引入 pytorch/Flide 依赖，也不修改 CUA 基座。
- `MockHumanProvider` 只返回任务预设的澄清文本，不接触页面真值或正确点击坐标。

## 明确不做（本试点范围）

不训练/微调模型；不做缓存优化、DVC、反事实回放、正式多场景评测、统计校准、并行推理、多轮人工会话、插件注册中心、数据库、Web UI、多 Agent。
