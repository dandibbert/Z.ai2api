<div align=center>
<img width="100" src="https://wsrv.nl/?url=https%3a%2f%2fz-cdn.chatglm.cn%2fz-ai%2fstatic%2flogo.svg&w=300&output=webp" />
<h1>Z.ai2api</h1>
<p>将 Z.ai 代理为 OpenAI Compatible 格式，支持免令牌、智能处理思考链、图片上传（登录后）等功能</p>
<p>基于 https://github.com/kbykb/OpenAI-Compatible-API-Proxy-for-Z 使用 AI 辅助重构</p>
</div>

## 功能
- 支持根据官网 /api/models 生成模型列表，并自动选择合适的模型名称。
- （登录后）支持上传图片，使用 GLM 识图系列模型。
- 支持智能识别思考链，完美转换多种格式。
- 通过模型别名（如 `GLM-4.5-Thinking`、`GLM-4.5-Search`）即可开启思考或搜索功能。

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
## 要求
![Python 3.12+](https://img.shields.io/badge/3.12%2B-blue?style=for-the-badge&logo=python&label=python)
![.env](https://img.shields.io/badge/.env-%23555?style=for-the-badge&logo=.env)

## 环境
使用 `.env` 文件进行配置。
### `BASE`
  - 上游 API 基础域名
  - 默认值：`https://chat.z.ai`
### `PORT`
  - 服务端口
  - 默认值：`8080`
### `MODEL`
  - 备选模型，在未传入模型时调用
  - 默认值：`GLM-4.5`
### `TOKEN`
  - 访问令牌
  - 如果启用了 `ANONYMOUS_MODE` 可不填
### `TOKEN_POOL`
  - 令牌池，支持逗号或换行分隔的多个令牌
  - 会在非匿名模式下按轮询方式自动切换
### `TOKEN_POOL_FAILURE_THRESHOLD`
  - 同一令牌连续失败多少次后暂时标记为不可用
  - 默认值：`3`
### `TOKEN_POOL_RESET_FAILURES`
  - 令牌在失败后多久（秒）重新尝试
  - 默认值：`1800`
### `ANONYMOUS_MODE`
  - 访客模式，启用后将获取随机令牌
  - 默认值：`true`
  - 访客模式下不支持上传文件调用视觉模型
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
### `DEBUG_MODE`
  - 显示调试信息，启用后将显示一些调试信息
  - 默认值：`false`

## 使用
```
git clone https://github.com/hmjz100/Z.ai2api.git
cd Z.ai2api
pip install -r requirements.txt
python app.py
```