# Local Agent Harness · 编程挑战 03

产品形态是 **CLI 客户端 + 本机常驻 Harness 服务**。CLI 通过带本地 Bearer token 的 loopback HTTP/SSE 请求服务；模型 API、ReAct 工具循环、插件执行和 SQLite 历史都在服务端。没有把浏览器或算术功能写进宿主，也不依赖 Hermes 进程或 Manus 会话来执行任务。

代码基于 Python 3.11，服务用 FastAPI/Uvicorn，出站模型用官方 OpenAI Python SDK，CLI 用 Typer/Rich。默认模型固定为 `qwen/qwen3-235b-a22b-2507`，默认上游为 `https://openrouter.ai/api/v1`，不做模型自动回退；配置校验拒绝 GPT-6 及以上、Claude Fable 5 及以上。

首次选择的 `openai/gpt-4.1-mini` 被 OpenRouter 返回 403 “This model is not available in your region”，而 key 认证检查为 200。因此在用户允许的型号范围内人工选择并停服配置了 Qwen 工具模型；这不是隐藏的运行时 fallback，也不是 API key 无效。

## 1. 现在即可运行

这是 Python CLI/服务项目，没有 npm build。先执行下面的构建/安装，再用项目内 `./harness`；无需激活虚拟环境，也不依赖全局 PATH 中存在 harness。验收本机的两插件已长期启用；首次 checkout 不包含 key 或运行数据，请按首次安装步骤初始化。API key 仅写入自己的私有运行目录。

```bash
cd /path/to/local-agent-harness
bash scripts/bootstrap.sh
./harness daemon start
./harness daemon status
./harness run '必须使用工具计算 (35 × 7 - 128) ÷ (12 + 8)，不要心算。' --plugin arithmetic
```

构建脚本采用 `uv sync --locked --no-editable --python 3.11 --link-mode copy --reinstall-package local-agent-harness`。普通 wheel 安装彻底避开 macOS 隐藏 `.pth` 被 Python 跳过导致的 `ModuleNotFoundError`；一次清除 hidden 标记不足以可靠修复，所以不再使用 editable。修改宿主源码后重新构建，运行中的服务需 stop/start 才会加载新代码。不要再用无 `--no-editable` 的 uv sync/uv run 把安装切回 editable。

下面这些命令可先做无模型费用的本地自检：

```bash
./harness tools call arithmetic_parse --args '{"expression":"(35 × 7 - 128) ÷ (12 + 8)"}' --plugin arithmetic --poll
.venv/bin/python -m pytest -q
```

安装到另一台机器或首次建立私有运行目录（已有 key/插件时不要重复添加）：

```bash
bash scripts/bootstrap.sh
./harness init
./harness credentials set                 # 隐藏输入；不要把 key 放在命令参数里
./harness plugin add ./plugins/arithmetic --trust
./harness plugin add ./plugins/browser-safety --trust
./harness plugin enable arithmetic --trust
./harness plugin enable browser-safety --trust
./harness daemon start
```

后续示例中的 `harness` 可以全部写作 `./harness`；若更喜欢裸命令，再 `source .venv/bin/activate`。

普通 shell 启动 Chrome 可以使用其自身沙箱。在 Manus 的受限终端中，原生 Chrome 可能因嵌套沙箱/进程权限不足崩溃；本次真实验收使用了允许原生浏览器启动与专用 PID 核验的权限，**没有添加 `--no-sandbox`**。不要把该环境问题误当成插件 JS 语法错误。

## 2. 前后台进程

```bash
harness serve                          # 前台模式，同 serve foreground
harness daemon start                   # 本机 detached，已有实例则复用
harness daemon status
harness daemon logs --lines 100
harness daemon stop
```

`daemon` 是 `serve` 命令组的别名。`start` 使用绝对 Python 解释器、独立 session/process group、DEVNULL stdin、文件日志；owner 持有私有 `flock`，controller 必须通过认证 status 核对 PID + instance UUID + owner 锁，才报告启动或发 shutdown。未知端口 owner 不会被杀，重复 start 不会另建进程。

`stop` 先停止 admission 并请求当前 run 取消，等实际 worker 返回后才释放锁。超出等待时间会报告 draining，而不是谎称停止。首版没有强杀 fallback 或 `--force` 命令；遇到永久卡住的 full-trust 代码须由操作者核验该实例后处理。默认不安装 LaunchAgent、不自动登录启动、不禁止休眠；这些是边界外的部署选项，不是假常驻必需条件。日志首版是有界 tail，不提供 follow/自动轮转。

默认监听 `127.0.0.1:8765`。不同本地实例用 `--home`/`HARNESS_HOME`，并在停服时配置不同端口。`--url` 只允许无凭据的 loopback HTTP origin，不把本地 token 发给任意 URL。

## 3. CLI 用法与一次插件选择

| 目标 | 命令 |
|---|---|
| 一次任务 | `harness run '任务描述'` |
| 完整覆盖本次插件集合 | `harness run '任务' --plugin arithmetic --plugin browser-safety` |
| 本次不提供工具 | `harness run '你好' --no-plugins` |
| 继续同一会话 | `harness run '下一轮' --session sess_... --plugin arithmetic` |
| 交互聊天 | `harness chat --plugin arithmetic`，`/exit` 仅退出客户端 |
| 只提交任务 | `harness run '任务' --detach` |
| 查看进度/事实 trace | `harness runs show run_...` / `harness runs events run_...` |
| 显式取消 | `harness runs cancel run_...` |
| 会话管理 | `harness session new/list/show/messages/close` |
| 插件管理 | `harness plugin add/list/show/enable/disable` |
| 修改插件配置 | `harness plugin config set browser-safety quota 3` |
| 读取插件配置 | `harness plugin config get arithmetic` |
| 工具列表 | `harness tools list --plugin arithmetic` |
| 直接工具调用，不调用模型 | `harness tools call arithmetic_parse --args '{"expression":"2 + 3"}' --plugin arithmetic` |

**不写 `--plugin` 使用长期 enabled 集合；写了就完整替换，不是追加。** 可显式选取已安装、已信任但默认 disabled 的插件，只影响该 run，不修改配置文件或 revision。`--no-plugins` 等于空集合。所有模型和工具执行都必须经过正在运行的服务，CLI 没有离线业务 fallback。

服务未启动时，CLI 可以在同一个 owner 写锁保护下管理配置/安装登记；离线 `add`、`enable` 不 import 插件 Python，工具的实际注册检查在启动/本次选择时由服务进行。离线不能列出实际注册后的工具。在线管理修改会在执行槽忙时拒绝；host/port、模型 route 和代码替换要求先停服。

## 4. 两款实际不同的插件

### 四则运算 `arithmetic`

工具为 `arithmetic_add`、`arithmetic_subtract`、`arithmetic_multiply`、`arithmetic_divide` 和 `arithmetic_parse`。四则参数为 `a/b`，可传数字或十进制字符串；字符串优先以保留输入精度。parse 参数为 `expression`，用有界 tokenizer + 递归下降处理括号、优先级、一元正负、空格、小数和 `×/÷`，**不使用 Python `eval/exec` 或浏览器 JS**。

```bash
harness tools call arithmetic_parse \
  --args '{"expression":"(35 × 7 - 128) ÷ (12 + 8)"}' --plugin arithmetic
```

正确结果字符串为 `"5.85"`，同时返回 AST、语法类型和 postorder steps；每步复用同一套四则函数。默认 Decimal 有效数字精度 50，必要时 ROUND_HALF_EVEN；这不是符号代数或无限精度计算。语法错误返回 `syntax_error` + 零基 Unicode 字符 `position`，除零返回 `division_by_zero`，非法 token、超长或过深输入有类型化错误。算式不支持函数、变量、科学记数法或任意代码。

工具描述明确要求模型算术任务调用此工具而非心算/`browser_eval`。真实验收检查服务 `tool.started/tool.result`，不能只以最终文本出现 5.85 推定调用过工具。

### 浏览器安全交互 `browser-safety`

启动有 UI 的 Google Chrome，使用插件私有 profile、随机 loopback CDP 端口、自己的 instance descriptor 和锁。只复用自己创建并核验了 PID/启动参数/profile 的浏览器，不采用用户日常 Chrome profile、不连接任意已有 CDP。

| 工具 | 语义 |
|---|---|
| `browser_open` | 确保单例有界面 Chrome，并用 Target.createTarget 创建本会话 owned 页；默认 URL about:blank |
| `browser_navigate` | 参数 `target_id/url`；在指定 owned tab 上 Page.navigate，不新建页 |
| `browser_eval` | 参数 `target_id/script`；Runtime.compileScript 再 Runtime.runScript，分别返回语法/运行错误 |
| `browser_extract` | 固定 Runtime.evaluate 脚本提取 title/url/body.innerText；max_chars 默认 4000、最大 8000 |
| `browser_close` | Target.closeTarget **仅 result.success is True** 才返回 closed:true、回收名额 |
| `browser_status` | Target.getTargets 全浏览器快照，并标注 self/other_session/foreign；缺页不退名额 |
| `browser_release` | 先持久标记该会话 stopping，再逐个关闭 owned 页，返回 unreclaimed；允许重试清理 |

`target_id` 就是题目中的 tab_id/CDP targetId，不是索引。只有该会话的页可以 navigate/eval/extract/close；默认每会话名额 3，未确认创建/关闭也占名额。识别到 owned openerId 链的弹窗会计入名额，可能已超额后拒绝进一步 open；noopener/opener-less 弹窗不能可靠归属，保守保持 foreign。**这不是 Chrome 页面数硬限制，也不是任意 JS/网络权限沙箱。** eval 可以修改页面、发网络请求、导航或弹窗；页面内容是不可信数据。

release 是单向 stopping；之后同 browser 会话拒绝 open/navigate/eval/extract，只允许 status/close/release 清理。direct 工具连续调用固定使用 `local-direct` ownership，release 后再次测试需新聊天会话；不要删除账本来“重置”未确认名额。`session close` 只关闭对话会话，不暗中代表 browser_release，清理需明确工具调用。

Chrome 崩溃后的陈旧 profile 锁也不会被插件自动删掉。只有核实保存的专用 PID、全部专用 profile 子进程和 CDP 端口都已消失，操作者才可归档自己的 stale traces。浏览器死亡不能作为 Target.closeTarget(success:true) 的替代证据，旧未确认账本不会凭空清零。

## 5. 插件开发协议（Hermes 风格的明确子集）

插件是独立目录，含 `plugin.yaml`（或 `.yml`）与 `__init__.py`。add 复制受限大小的普通文件，拒绝 symlink 和未支持扩展字段，记录版本、源路径、托管路径、内容 SHA-256；不安装依赖，不运行 setup 或 register。启用/显式选择时核对指纹并事务化注册，名字冲突/声明不符拒绝整张新 registry。

```yaml
name: demo
version: "1.0.0"
description: Independent demo capability
kind: standalone
manifest_version: 2
provides_tools: [demo_echo]
provides_hooks: []
requires_env: []
config_schema:
  prefix: {type: string, default: "", description: Optional prefix}
```

```python
import json

def register(ctx):
    prefix = ctx.get_config("prefix", "")  # 缺配置使用 caller default
    def echo(args, **kwargs):
        return json.dumps({"text": prefix + args["text"]})
    handle = ctx.register_tool(
        name="demo_echo", toolset="demo",
        schema={"name": "demo_echo", "description": "Echo the input",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                               "required": ["text"], "additionalProperties": False}},
        handler=echo, is_async=False)
    if handle is None:
        raise RuntimeError("Tool registration rejected")
```

`ctx.plugin_id/get_config/register_tool` 是主要协议；handler 第一参数为 args dict。宿主只在 handler 能接受时传 `session_id/task_id/user_task`，兼容只写 `handler(args)` 的窄签名；async handler 必须显式 `is_async=True`，函数形态不符拒绝。返回必须是 str，推荐 JSON 字符串；普通文本被规范为 `{"text":"..."}`；裸 dict/list/None 返回类型错误，结果限制 16 KiB，超限不会重新执行工具。

function schema 传内层 `{name,description,parameters}`，宿主包装 OpenAI `{type:"function",function:...}`。object 必须 `additionalProperties:false`；MVP 支持基本类型/有限数字/简单类型数组和常规约束，拒绝 `$ref`、复杂组合 schema、override、hook、宿主替换模块、自写配置和自动依赖安装。支持 standalone manifest 1/2 与常见配置类型别名；不承诺完整 Hermes 插件 ABI。

插件是 **当前 macOS 用户权限下的 full-trust Python**，不是安全隔离的第三方包。registry schema、allowlist 和 browser ownership 是能力分发/误操作约束，不能约束恶意 Python、挂死 GIL/native 的代码或直接访问文件的插件。顶层/register 必须快速且无副作用；生产隔离另需子进程或沙箱，本题不伪造该能力。

## 6. API 和 SDK

除 `/health` 外所有接口要求私有本地 token，拒绝浏览器 Origin 请求；本地 token 与 OpenRouter key 分开。`/v1` 是闭环 Harness Chat Completions 兼容子集，**不是透明代理**：模型只接受发布的 `harness-default` alias，工具由服务已批准的 registry 决定。未知字段如客户端 tools/temperature、非 n=1、多模态/Responses 等明确拒绝。

```python
from pathlib import Path
from openai import OpenAI

token = (Path.home() / ".harness/secrets/local-token").read_text().strip()
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key=token, max_retries=0)
response = client.chat.completions.with_raw_response.create(
    model="harness-default",
    messages=[{"role":"user","content":"使用工具计算 2 + 3"}],
    extra_body={"harness_plugins":["arithmetic"]},
    extra_headers={"Idempotency-Key":"unique-request-key"})
print(response.parse().choices[0].message.content)
session_id = response.headers["X-Harness-Session-Id"]
```

无 session header 的请求建立新会话；续会话用 `X-Harness-Session-Id` 且只发送 **一个新增 user**，旧历史由服务补入，不能反复上传客户端旧历史。标准流支持 `stream=True`、`stream_options.include_usage`、delta/finish_reason/[DONE]；实时工具事实看原生 events，不把工具 trace 混进标准文本 delta。

| 接口 | 作用 |
|---|---|
| `GET /health` / `/api/status` | 健康、实例、busy、config revision |
| `GET /v1/models` | 本地可用 alias |
| `POST /v1/chat/completions` | 等待最终结果或标准 SSE |
| `POST /api/runs` | 202 run/session receipt，异步运行 |
| `GET /api/runs[/{id}[/events]]` | 运行记录、结果、可重放 SSE 事实 |
| `POST /api/runs/{id}/cancel` | 只取消该 run，不取消“下一任务” |
| `/api/sessions` / `/{id}` / `/{id}/messages` / `/{id}/close` | 对话会话与历史 |
| `/api/plugins` / `/{id}` / `/{id}/enable|disable|settings` | 安装、状态、启停、配置 |
| `GET /api/tools` / `POST /api/tools/call` | 工具 inspection / 直接工具 run |
| `GET/PATCH /api/config` | 白名单配置、expected_revision 冲突 |
| `POST /api/shutdown` | drain 并停止新 admission |

默认每次最多 8 模型回合、12 次工具调用、1024 输出 tokens/模型调用、120 秒 run 软 deadline，同时执行槽只有 1。工具报错是 observation，不要求模型泄露隐藏思考。thread 的取消/超时不等于停止：保持 busy/draining，直到实际 worker 返回；断连只是离开观察者，不自动重跑。幂等 key 只避免宿主重复提交，不提供外部副作用 exactly-once。

SQLite 保存会话、消息、run、工具意图/started/result 和 events；重启不自动重放工具。已执行但缺返回的记录只能标 unknown，不能把进程退出当作无副作用。配置/密钥/数据目录均在 `~/.harness/`，目录 0700、文件 0600；源代码、压缩包和日志不含提供的 API key。

## 7. 验证与交付

```bash
bash scripts/bootstrap.sh
.venv/bin/python -m pytest -q           # 离线单元 / mock provider，不产生模型费用
.venv/bin/python scripts/verify_live.py --confirm-model-cost \
  --output-dir /tmp/harness-live-check   # 必须服务运行；会调用真实模型并启动/复用 headed Chrome
```

live 脚本用临时 loopback 测试页，不访问私人账户或公网表单；验证标准 SDK 非流/SSE、指定表达式、算式语法错误、混合两插件模型闭环、真实 CDP 窗口与截图、确认 close/release、CLI 直接工具调用、disable/单次覆盖不改配置和认证。它只读本地服务 token，OpenRouter key 留在服务端。

项目结构：`src/harness/` 宿主，`plugins/arithmetic/` 和 `plugins/browser-safety/` 两独立插件，`tests/` 离线测试，`scripts/verify_live.py` 可重复真实验收；交付报告保存实际结果与限制，不用本 README 的设计描述替代通过证据。

## 8. 切换为 DeepSeek

2026-11-18 已从当前本机网络，经 OpenRouter 对 `deepseek/deepseek-v4.1-flash-1017` 完成隔离的真实宿主 ReAct + arithmetic_parse 两回合检查，结果 5.85、费用约 $0.000121484。使用的是宿主现有 provider 参数，无 reasoning 特殊覆盖，未改变运行中服务的长期 Qwen 配置。这只证明当前出口/账号/路由可用，不保证所有大陆网络能直连 OpenRouter，也没有重复执行 DeepSeek 下完整浏览器回归。

[OpenRouter 官方模型页](https://openrouter.ai/deepseek/deepseek-v4.1-flash-1017) 和当前模型目录都有 tools/tool_choice；`deepseek/deepseek-v4.1-flash` 也在目录，推荐前者明确的 1017 快照。若希望切换：

```bash
./harness daemon stop
./harness config set model deepseek/deepseek-v4.1-flash-1017
./harness daemon start
./harness run '用工具计算 (35 × 7 - 128) ÷ (12 + 8)' --plugin arithmetic
```

若改为 DeepSeek 国内平台直连，[官方 API 文档](https://api-docs.deepseek.com/zh-cn/) 指定 `https://api.deepseek.com`、`deepseek-flash`（当前对应 V4.1 Flash 1017），需要单独 DeepSeek API key。OpenRouter key 不能直接用于该平台；当前 provider 发送 OpenRouter 专用路由参数，直连接入还需小幅适配，不应只改 base_url 就宣称已验收。

## 9. 公开交付与最终验收

代码仓库：https://github.com/jorschac/local-agent-harness 。本次最终回归实际为 **339 collected / 339 passed**，0失败、0跳过、1条Starlette弃用warning；不是399，未凑测试数量。完整逐项日志、JUnit、用户人工日志(已脱敏出口IP)和只读服务事件摘录在 [docs/acceptance](docs/acceptance)。

源码默认模型仍为Qwen；验收本机在2026-11-18用户操作后当前私有配置为 `deepseek/deepseek-v4.1-flash-1017`、revision9。历史run只保存本地model alias，不能由现配置倒推所有历史run上游型号。Google两次返回验证码页，浏览器调用/确认关闭成功，但查到天气这一业务目标没有成功。中国天气网实际由首页导航至北京预报页并提取，不是站内搜索框交互，最后browser_release返回unreclaimed空列表；核心数值与本次extract文本一致，不是重新气象核验。

首次从GitHub获取：

```bash
git clone https://github.com/jorschac/local-agent-harness.git
cd local-agent-harness
bash scripts/bootstrap.sh
# 然后按“首次建立私有运行目录”提供自己的key并安装/启用两插件
```

完成报告：[docs/任务完成报告.pdf](docs/任务完成报告.pdf)。公开仓库不包含任何真实key、本机Bearer token、运行数据库、Chrome profile/cookie、虚拟环境或缓存。
