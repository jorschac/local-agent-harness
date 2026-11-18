# 来源与实现声明

本题研究了用户 GitHub 账号下的 [hermes-agent 固定快照](https://github.com/jorschac/hermes-agent/tree/67e4d22d83b242ecfb2807ed39e9696ba1b1b0d5)，提交 `67e4d22d83b242ecfb2807ed39e9696ba1b1b0d5`。实现借鉴独立 manifest、`register(ctx)`、`get_config`、`register_tool` 与工具反馈循环，按本题限制实现 standalone 协议子集；不依赖 Hermes 进程，也不声称完整 Hermes 插件 ABI。来源/裁剪说明见 [docs/来源与协议.md](docs/来源与协议.md)。

Hermes 上游为 MIT License，版权归 Nous Research。为保留研究和可复用接口/实现思路的归属，原许可全文收录于 [third_party/hermes-agent-LICENSE](third_party/hermes-agent-LICENSE)。该文件是第三方许可，不自动将本仓库全部原创代码另行授予 MIT；本项目许可证由权利人另行决定。

关键公开协议：Chrome DevTools Protocol Runtime 与 Target、OpenAI Chat Completions、OpenRouter OpenAI SDK。浏览器和算术两插件均独立目录交付。模型页、价格及连接可用性均按验收日期描述，不保证后续所有地区/账号可用。

真实 API key、本地 Bearer token、数据库、浏览器 profile/cookie 不属于源代码或公开素材。公开材料已脱敏本机用户名路径和人工日志出口 IP；合成测试凭据、loopback、测试关联 ID 与依赖哈希不是实际密钥。未撤销本机私有 key，也未将其上传。
