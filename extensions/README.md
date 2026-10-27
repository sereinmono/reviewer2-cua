# nano-cua-extensions

nano-cua 基座的**外部兼容层**，是 `nano-cua-lab` 统一仓库中的**插件组**。基座本身只包含 ports + Runner + BasicPolicy + MockActor；这里存放具体的观察/动作/模型兼容实现，因此它们不会进入 nano-cua 仓库，也不需要修改基座代码。

本目录当前包含两组：

| 模块 | 角色 | 内容 |
| --- | --- | --- |
| `nano_cua_extensions.playwright_browser` | 观察 + 动作兼容组（Playwright/Chromium） | 截图 `Observer`、浏览器 `ActionExecutor`（click/type_text/press_key）、动作白名单与参数校验、模型 system prompt、模型输出 parser；`PlaywrightIO` 一个对象同时实现两个端口，可作为“组”直接注入 |
| `nano_cua_extensions.vlm` | Actor（模型决策） | 通用 OpenAI-compatible `VLMActor`；动作空间无关，system prompt 与 proposal parser 由动作组注入 |
| `nano_cua_extensions.demo_cli` | 组合根 / demo | 把基座、Playwright 组和 Mock/VLM Actor 接起来跑一个本地页面任务 |

依赖：`nano-cua`（统一仓库同级目录 `../../nano-cua`，通过 `[tool.uv.sources]` 指向 editable 本地路径；由根 workspace 统一同步）、`playwright`、`httpx`。

## 快速开始

需要先有同级目录的基座仓库：

```text
prior_experiment/
├── nano-cua/               # 基座（零运行时依赖，独立仓库）
├── nano-cua-lab/           # 本整合仓库
│   ├── extensions/         # 插件组（本目录）
│   └── experiments/        # 双 Gate 试验
└── omnijev-deploy/         # OmniJev 部署（独立目录）
```

```bash
cd nano-cua-lab
uv sync --all-packages --all-groups   # 根 workspace：同步所有成员 + dev group
uv run playwright install chromium     # 首次安装浏览器
uv run nano-cua-demo                   # MockActor 本地页面 demo，无需 API Key
uv run pytest -q extensions/tests      # 插件组单元测试 + 真实 Chromium 冒烟测试
```

接入真实 VLM（OpenAI-compatible）：

```bash
export NANO_CUA_API_BASE="https://your-host/v1"
export NANO_CUA_MODEL="your-model"
export NANO_CUA_API_KEY="..."
uv run nano-cua-demo --actor vlm
```

CLI 参数与原基座 demo 一致：`--headed`、`--url`、`--task`、`--width/--height`、`--max-steps`、`--max-observations`、`--browser-channel`、`--save-screenshot`、`--json-mode`。

## 作为“组”注入基座

组 = 一个同时实现 `Observer` 和 `ActionExecutor` 的对象（或一对 observer/executor）；基座 Runner 只要求这两个端口，因此组可以整体替换：

```python
from nano_cua import BasicPolicy, Runner
from nano_cua_extensions.playwright_browser import (
    BROWSER_SYSTEM_PROMPT,
    PlaywrightIO,
    browser_proposal_parser,
)
from nano_cua_extensions.vlm import VLMActor

group = await PlaywrightIO.launch(headless=True, url="file:///demo.html")
actor = VLMActor(
    base_url="http://localhost:8000/v1",
    model="my-vlm",
    system_prompt=BROWSER_SYSTEM_PROMPT,      # 由动作组提供
    proposal_parser=browser_proposal_parser,  # 由动作组提供
)
runner = Runner(policy=BasicPolicy(actor), observer=group, executor=group, task="...")
result = await runner.run()
await group.close()
```

## 新增其他兼容组（OSWorld / 桌面鼠标键盘 / bash 等）

不修改基座，新增一个模块即可：

1. 实现 `async def observe(request) -> Observation`（截图/无障碍树/文本都可放进 `payload` + `metadata`）；
2. 实现 `async def execute(action: Action) -> ActionResult`（把通用 `Action(space, kind, args)` 翻译成该环境的动作）；
3. 为该动作空间提供模型的 system prompt 和 `proposal_parser(payload, observation) -> Action | Finish`；
4. 组合时把组对象传给 `observer=` 与 `executor=`（或分开传 observer/executor），Actor 注入对应的 prompt/parser。

因为 `Action`/`Observation` 是通用消息、Runner 只解释 `Observe/Execute/Finish`，任何环境只要遵守这两个协议即可接入。

## 测试

```bash
uv run pytest -q
```

覆盖：浏览器动作校验、VLM 请求/严格解析（`httpx.MockTransport`，无需 Key）、CLI 组合逻辑、真实 Chromium 冒烟测试（无浏览器时 skip）。
