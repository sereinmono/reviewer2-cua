# CUA 双 Gate 效率组汇总（20 个 clear 任务）

> 汇总对象：`reviewer2-cua/experiments/scripts/benchmark_efficiency_20.py` 的一次运行。
> 这组测试只关注**效率/资源开销**，不用于安全/歧义结论；安全组见 [safety-evaluation-2026-10-27.md](safety-evaluation-2026-10-27.md)。
>
> - 日期：2026-10-27
> - 基座：`nano-cua` @ `204dd53`（未修改）
> - 插件组：`nano-cua-extensions`（Playwright 浏览器组）
> - 评分器：本地 Flide + `OmniJev/OneJev1.6-0.8B`
> - 原始实验提交：`cua-gates-pilot@4886de3`；本仓库整合后脚本路径：`experiments/scripts/benchmark_efficiency_20.py`
> - 运行环境：RTX 5070 Laptop 8 GB，Docker Desktop（WSL2），Playwright Chromium headless
> - 运行方式：20 个任务 × baseline/gated 各 1 次，共 40 个 episode

---

## 1. 目的与任务

所有任务都是**清晰任务**：页面明确给出任务目标，两个版本都应成功。这样比较的才是纯开销，而不是歧义处理收益。

- **baseline**：基座 `BasicPolicy`（无 Gate、无 OmniJev、无人工通道）
- **gated**：`GatedPolicy`（Observe Gate + AskHuman Gate），真实 OmniJev 诊断
- Actor：确定性 `TaskActor`，每个任务先执行唯一正确目标，然后 `Finish`
- 人工：`MockHumanProvider`，答案固定为 "proceed"，**延迟设为 2 s**，用于计量误触发人工的代价
- success：页面最终状态正确且只执行了目标动作

20 个任务为 E01–E20，覆盖单按钮、双按钮、三按钮，目标均在任务文本中唯一指定（如 "Click the Start button."、"Click the No button."、"Choose Month."）。

## 2. 结果

| 指标（20 任务平均/合计） | baseline | gated |
| --- | --- | --- |
| 成功 | **20/20** | **20/20** |
| 平均端到端 wall time | **65.4 ms** | **598.5 ms**（中位数 486 ms） |
| 去掉 E15 误触发后的均值 | — | 约 **498 ms** |
| 平均截图 | 2.0 | **1.0** |
| 平均截图字节 | 33,548 B | **16,944 B** |
| 平均截图采集耗时 | 61.7 ms | **23.7 ms** |
| 平均 decision 时间 | 0.0 ms（MockActor 瞬时） | **571.1 ms**（含 2 次 OmniJev） |
| 平均 Actor 调用 | 2.0 | 2.05 |
| 人工请求 / 平均等待 | 0 / 0 ms | **1 / 100.5 ms**（实际一次 2009.6 ms） |
| OmniJev 调用 | 0 | **2.0 / 任务** |
| OmniJev token | 0 | 输入合计 **43,916**（≈2,196/任务），输出合计 **60**（≈3/任务） |

净差值（gated − baseline）：

| 指标 | 差值 |
| --- | --- |
| wall time | **+533.1 ms/任务**（去掉 E15 约 +433 ms） |
| 截图 | **−1 张/任务** |
| 截图字节 | **−16,604 B/任务** |
| 截图采集时间 | **−38 ms/任务** |
| Actor 调用 | +0.05 / 任务 |
| OmniJev 调用 | +2 / 任务 |
| OmniJev token | 约 +2,196 input / +3 output token 每任务 |

## 3. 关键观察

1. **成功率无变化**：两个版本 20/20 成功；clear 任务上 gated 没有因 Gate 引入失败。
2. **固定开销显著**：gated 每任务约 0.43–0.53 s，主要来自 2 次 OmniJev 调用（含截图图像 token）；本地 0.8B 模型每次调用约 0.2–0.3 s。
3. **Observe Gate 的截图收益**：平均截图 2→1，字节数约减半，截图采集时间减少约 38 ms；但远小于 OmniJev 增加的约 400–500 ms。
4. **AskHuman 假阳性有实际成本**：E15（"Click No."，页面 Yes/No）在 clear 任务上被 AskHuman Gate 判为 ASK（target-uniqueness 信号 0.998），触发 1 次人工请求并等待 2009.6 ms；任务仍成功，但单任务 wall time 达 2509.4 ms，显著抬高 gated 均值。中位数/去异常后的均值更能代表常态。
5. **token 成本不可忽略**：20 任务共 43,916 input tokens（每个 gated 任务 2 次调用，每次调用携带一张截图和文本），输出仅 60 tokens。若按 API 计价需要单独乘单价；本报告不做费用换算。

## 4. 结论与限制

- 在 clear 任务上，双 Gate 的收益是**节省约一半截图**，代价是**每任务增加约 0.43–0.53 s 和约 2.2k input tokens**；本实验未观察到成功率收益（因为任务本来就清晰）。
- E15 说明 AskHuman Gate 在清晰任务上仍可能假阳性，且人工等待会直接变成延迟成本。
- 限制：
  - 20 个页面为手工构造、Actor 为确定性脚本；
  - 单次运行，无重复测量、中位数置信区间或不同 GPU 条件下的对比；
  - 本地 0.8B 模型，OmniJev 延迟和 token 数不代表其他模型/服务；
  - `MockHumanProvider` 的 2 s 是模拟延迟，不是真实人工响应时间；
  - 无 API/GPU 计价模型，不能据此做成本收益结论。

## 5. 复现

```powershell
cd D:\Research26fall\prior_experiment\reviewer2-cua
uv sync --all-packages --all-groups
uv run python experiments/scripts/benchmark_efficiency_20.py
```

需要本机 OmniJev/Flide 服务运行在 `http://127.0.0.1:8000/v1/systemone`。脚本会输出逐任务表与 summary JSON；本报告的数据来自一次运行，未保存原始 JSON 归档。
