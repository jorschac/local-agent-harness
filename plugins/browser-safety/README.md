# browser-safety

独立 `manifest_version: 2` 插件，仅使用 `register(ctx)`、`ctx.get_config` 和 `ctx.register_tool`；不导入宿主业务。所有 handler 为同步函数，返回 JSON 字符串，业务错误为 `{error:{code,message,execution}}`。

## 工具

`browser_open(url="about:blank")` 返回 `target_id`。其余页面工具使用这个键：`browser_navigate(target_id,url)`、`browser_eval(target_id,script)`、`browser_extract(target_id,max_chars=4000)`、`browser_close(target_id)`。`browser_status()` 返回全浏览器 `targets` 快照和当前 session 的 `owned`、`pending`、`quota_used`；`browser_release()` 持久设置 session 为 `stopping`，并返回 `closed` 与 `unreclaimed`。

宿主通过 handler 的 `session_id` kwargs 传递所有权上下文；缺省使用 `local-direct`，对应 CLI 连续直接调用。`task_id`、`user_task` 不被当作所有权主体。停止中的 session 不能再建页、导航或执行用户脚本，关闭和释放可重试。浏览器进程不会被 release 关闭，其他 session 和 foreign 页不会被清理。

## 运行时与账本

默认状态目录为 `$HARNESS_HOME/plugin-data/browser-safety`（未配置 HARNESS_HOME 时 `~/.harness`），可通过 `state_dir` 配置覆盖。目录 0700，账本、descriptor、锁和日志 0600；数据通过原子替换与 fsync 持久写入，线程锁及 flock 防止并行重复建页。headed Chrome 使用独立 `chrome-profile`，CDP 仅 `127.0.0.1`，启动命令不含 headless。只会复用 descriptor 与 PID、启动标记、私密 profile 及 Chrome 写入的 DevToolsActivePort 同时匹配的浏览器，绝不接受任意外部 CDP endpoint。

创建请求发送前先记持久 reservation。请求失败、超时、缺 targetId 或状态写入不确定时，名额不凭快照释放。关闭请求发送前标记 closing；只有 `Target.closeTarget` 的 `result.success is True` 才删除 owned 并返还名额，false、异常、页面在 getTargets 中消失都不算成功。上一次 runtime 的未确认页也不能借新 runtime 推断回收。未知创建结果保留 operation_id，无法安全关联具体 target 时不提供强制忽略/删除账本工具；人工排障需先独立核实浏览器进程与状态。

## 安全边界

这不是 JavaScript 或网络权限沙箱。`browser_eval` 经 `Runtime.compileScript` 再 `Runtime.runScript` 执行，可修改页面、请求网络、导航及打开弹窗，也不禁止访问本地 HTTP 服务。只限制直接 open/navigate 为 http(s) 与 about:blank，拒绝 file/data/javascript URL，并不阻止页面自身跳转或 JS 副作用。算术任务必须调用 arithmetic 工具，不能用 browser_eval 替代。

全浏览器快照用于识别已知 owned `openerId` 链中的 page 子孙，计入原 session 名额，即使弹窗已把名额推到上限以上也保留并阻止进一步 open。JS 能创建多个页且存在异步竞态，故 quota 不是 Chrome 的物理硬限制；opener-less/noopener 弹窗不能可靠归属，必须视为 foreign，不误认或关闭。新 Chrome 的启动 about:blank 为 foreign 启动页，不占 session 名额。extract 仅使用固定 Runtime.evaluate 脚本，返回 title、URL、可见 body.innerText，最大 8000 字符（默认 4000）。

## 测试注入

`BrowserService(state_dir, quota=3, runtime=fake_runtime, transport_factory=factory)` 不需启动浏览器。fake runtime 提供 `ensure()->{instance_id,endpoint}`；factory(endpoint) 返回支持 context manager 与 `call(method,params,target_id=None)` 的 transport。`ChromeRuntime` 的 launcher、probe、sleeper、clock 也可注入，所有启动测试均使用临时文件和 fake process。实际 CDP 使用 `websockets.sync.client.connect`，flatten attach 后发送 page commands；每个操作连接独立关闭。
