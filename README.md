> [!IMPORTANT]
> 您正在查看的是 Canary 分支，此分支包含众多 Bug，建议谨慎使用。

<div align=center>
<img width="100" src="https://wsrv.nl/?url=https%3a%2f%2fz-cdn.chatglm.cn%2fz-ai%2fstatic%2flogo.svg&w=300&output=webp" />
<h1>Z.ai2api</h1>
<p>将 Z.ai 代理为 OpenAI/Anthropic Compatible 格式，支持免令牌、智能处理思考链、图片上传（登录后）等功能</p>
</div>

## 功能
- OpenAI/Anthropic Compatible 接口
  - 智能识别思考链，完美转换为多种格式
  - 智能模型标识映射（`glm-4.6` -> `GLM-4-6-API-V1`）
  - （登录后）支持上传图片，使用 GLM 识图系列模型
  - 支持通过模型别名（如 `GLM-4.5-Thinking`、`GLM-4.5-Search`）快速开启推理或联网搜索特性
- Anthropic Compatible 接口
  - 智能识别工具块，转换为工具调用
  - 支持工具调用与多段内容输出
- Models 接口
  - 支持根据官网 /api/models 生成模型列表
  - 智能选择或生成合适的模型信息返回，示例：

    | 原始 | 结果 |
    |------|------|
    | id: `GLM-4-6-API-V1`<br>name: `GLM-4.6` | id: `glm-4.6`<br>name: `GLM-4.6` |
    | id: `deep-research`<br>name: `Z1-Rumination` | id: `z1-rumination`<br>name: `Z1-Rumination` |
    | id: `glm-4-flash`<br>name: `任务专用` | id: `glm-4-flash`<br>name: `GLM-4-Flash` |
    | id: `0808-360B-DR`<br>name: `0808-360B-DR` | id: `glm-0808-360b-dr`<br>name: `GLM-0808-360b-Dr` |
  - 特别适配 Open WebUI（下述内容为默认设置，后续可在 OWB 中更改）
    - 模型默认设为公开
    - 模型 meta profile_image_url 设为 Z.ai 的 data: Logo
    - 模型根据官网 hidden 设置 hidden 属性
    - 模型根据官网 suggestion_prompts 添加 suggestion_prompts
- 仪表盘与状态页
  - 内置 `/dashboard` 监控面板，实时查看请求统计并管理令牌池
  - 根路径（`/`）提供实时状态页，便于确认服务可用性
  - Token 池支持磁盘持久化，可在 Dashboard 中一次性粘贴多个令牌进行批量管理
  - Dashboard 及监控接口仅展示脱敏后的令牌标识，避免未授权访问时泄露完整 Token

## 要求
![Python 3.12+](https://img.shields.io/badge/3.12%2B-blue?style=for-the-badge&logo=python&label=python)
![.env](https://img.shields.io/badge/.env-%23555?style=for-the-badge&logo=.env)

## 使用
```
git clone https://github.com/hmjz100/Z.ai2api.git
cd Z.ai2api
pip install -r requirements.txt
python app.py
```

## 环境
使用 `.env` 文件进行配置。

### `PROTOCOL`
  - 上游 API 基础协议
  - 默认值：`https`

### `BASE`
  - 上游 API 基础域名
  - 默认值：`chat.z.ai`

### `TOKEN`
  - 提供给上游 API 的访问令牌
  - 如果启用了 `ANONYMOUS_MODE` 可不填

### `PORT`
  - 服务对外端口
  - 默认值：`8080`

### `THINK_TAGS_MODE`
  - 思考链格式化模式
  - 默认值：`reasoning`
  - 可选 `reasoning` `think` `strip` `details`，效果如下
    - "reasoning"
      - reasoning_content: `嗯，用户……`
      - content: `你好！`
    - "think"
      - content: `<think>\n\n嗯，用户……\n\n</think>\n\n你好！`
    - "strip"
      - content: `> 嗯，用户……\n\n你好！`
    - "details"
      - content: `<details type="reasoning" open><div>\n\n嗯，用户……\n\n</div><summary>Thought for 1 seconds</summary></details>\n\n你好！`

### `ANONYMOUS_MODE`
  - 访客模式，启用后将获取随机令牌
  - 默认值：`true`
  - 访客模式下不支持上传文件调用视觉模型

### `MODEL`
  - 备选模型，在未传入模型时调用
  - 默认值：`GLM-4.5`

### `DEBUG`
  - 启用调试模式，启用后将使用 Flash 自带的开发服务器运行，否则将使用 pywsgi 运行
  - 默认值：`false`

### `DEBUG_MSG`
  - 显示调试信息，启用后将显示调试信息
  - 默认值：`false`

### `AUTH_TOKEN`
  - 仪表盘与 API 鉴权口令
  - 设置后 Dashboard 登录、API Bearer Token 及 `?token=`/`?auth_token=` 参数均需匹配该值

## 模型别名与变体

项目会为常见模型提供统一别名，并在此基础上扩展 `-Thinking` 与 `-Search` 变体：

| 上游 ID | 别名 |
| --- | --- |
| `0727-360B-API` | `GLM-4.5` |
| `glm-4.5v` | `GLM-4.5V` |
| `0727-106B-API` | `GLM-4.5-Air` |
| `0808-360B-DR` | `0808-360b-Dr` |
| `deep-research` | `Z1-Rumination` |
| `GLM-4-6-API-V1` | `GLM-4.6` |
| `glm-4-flash` | `GLM-4-Flash` |
| `GLM-4.1V-Thinking-FlashX` | `GLM-4.1V-Thinking-FlashX` |
| `main_chat` | `GLM-4-32B` |
| `zero` | `Z1-32B` |

使用 `-Thinking` 变体会强制开启推理内容输出，`-Search` 变体会自动注入联网搜索所需的特性与 MCP 配置；未带后缀的别名保持上游默认能力。

## 监控面板与接口

- 访问 `http://<host>:<port>/` 可查看轻量状态页。
- 访问 `http://<host>:<port>/dashboard` 即可使用内置监控面板。
- 如果已配置 `AUTH_TOKEN`，可以在地址后追加 `?token=<AUTH_TOKEN>`（或 `?auth_token=`）快速完成一次性登录。
- 若设置了 `AUTH_TOKEN`，同样的口令将作为面板登录密码以及 API 访问的 Bearer Token。
- 面板提供：
  - 请求统计卡片（总请求数、成功/失败次数、平均响应时长）。
  - 最近 100 条请求明细（时间、方法、路径、状态码、耗时、客户端 IP、使用的令牌）。
  - 令牌池管理（新增 / 删除令牌实时生效、支持逗号或换行批量粘贴、查看禁用状态与冷却时间、统计成功/失败次数）。
  - 自动刷新（默认 5 秒，可在界面关闭）。
- 登录成功后才可访问 `/dashboard/api/*` 接口，如遇 401 可重新打开 `/dashboard` 输入密码或在请求 URL 中添加 `auth_token=<AUTH_TOKEN>`/`token=<AUTH_TOKEN>`。
- `/token-pool/status` 返回 JSON 快照，可用于外部监控程序。
  - 返回的 `token_id` 为不可逆的哈希标识，仪表盘中也只会显示 `token:xxxxxxxx` 形式的脱敏文本；若需删除令牌可在 Dashboard 中操作或调用 `/dashboard/api/tokens` 并传入对应 `token_id`。

## 说明
初始版本基于 https://github.com/kbykb/OpenAI-Compatible-API-Proxy-for-Z 使用 AI 辅助重构
