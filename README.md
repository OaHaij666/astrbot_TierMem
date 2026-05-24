# TierMem

AstrBot 插件 —— 主动总结 + 工具辅助的双轨记忆系统。

---

## 记忆机制

TierMem 采用 **三层记忆 + FIFO 对话缓存** 的架构，让 Bot 既能记住用户的长期画像，也能感知近期上下文。

### 三层记忆

| 层级 | 作用 | 注入策略 |
|------|------|----------|
| **Important** | 核心画像、关键偏好、长期任务 | 始终注入 |
| **General** | 普通事实、常规信息 | 按需注入 |
| **Fleeting** | 临时内容、短期事件 | 仅当需要时注入 |

- 每层有独立的容量上限，超出时按 **重要性 + 更新时间** 自动淘汰旧记忆。
- 支持两种总结模式：
  - `search_replace`：精准定位已有记忆进行增删改，保留未变动内容。
  - `full_replace`：全量重新生成三层记忆，适合彻底刷新。

### FIFO 对话缓存

- 针对每个用户维护一个 **固定长度的对话轮次队列**（默认 10 轮）。
- **群聊环境**下，这 N 轮近期对话会注入到系统提示词中，让 Bot 感知当前话题上下文。
- **私聊环境**下不注入 FIFO，避免重复冗余。
- 当 FIFO 达到阈值时，**自动触发异步总结**：将 N 轮对话 + 现有三层记忆发送给 LLM，生成更新后的记忆，然后清空 FIFO。
- 总结过程在后台执行，**不阻塞 Bot 的正常对话响应**。

### 双轨记录

1. **自动总结（主轨道）**：系统定期自动沉淀对话为结构化记忆，无需 LLM 干预。
2. **工具调用（辅轨道）**：LLM 仍拥有 `memory_add` / `memory_update` / `memory_delete` 工具，用于即时记录关键信息。提示词中会提醒 LLM **谨慎使用**，避免滥用。

### 全局 / 共享模式

- **Global（全局）**：用户在不同群聊中拥有**完全独立**的三层记忆和 FIFO 缓存。
- **Shared（共享）**：用户跨群聊**共享同一套**记忆，实现记忆一致性。
- 模式切换时，系统会自动进行**数据迁移与备份**，避免数据丢失。

---

## 安装

将本仓库克隆到 AstrBot 的 `plugins/` 目录下：

```bash
cd /path/to/astrbot/plugins
git clone https://github.com/OaHaij666/astrbot_TierMem.git
```

重启 AstrBot，插件会自动安装依赖并初始化 SQLite 数据库。

---

## 配置

在 AstrBot 管理面板的插件配置中，可调整以下项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `memory_mode` | 记忆模式：`global`（群独立）/ `shared`（跨群共享） | `global` |
| `fifo_size` | FIFO 缓存对话轮数阈值 | `10` |
| `summary_mode` | 总结模式：`search_replace` / `full_replace` | `search_replace` |
| `summary_provider_id` | 总结用 LLM Provider ID，**留空则使用主对话模型** | `""` |
| `summary_system_prompt` | 总结任务的额外系统提示词 | `""` |
| `inject_fifo_in_group` | 群聊时是否注入 FIFO 对话 | `true` |
| `inject_memory_in_private` | 私聊时是否注入三层记忆 | `true` |
| `inject_layers_in_group` | 群聊时注入哪些记忆层 | `important_only` |
| `max_memory_per_layer` | 每层记忆最大条数 | `50` |
| `enable_auto_summary` | 是否启用 FIFO 满自动总结 | `true` |
| `enable_manual_summary` | 是否允许手动触发总结 | `true` |
| `enable_llm_tools` | 是否向 LLM 暴露记忆工具 | `true` |
| `tool_caution_in_prompt` | 是否在提示词中附加工具使用警告 | `true` |

---

## 命令

| 命令 | 说明 |
|------|------|
| `/memory sum` | 立即手动触发总结 |
| `/memory check [layer]` | 查看当前记忆，`layer` 可选 `important` / `general` / `fleeting` |
| `/memory status` | 查看 FIFO 和三层记忆的统计状态 |
| `/memory rollback` | 回滚到上次备份 |
| `/memory help` | 显示帮助 |

---

## 技术栈

- **Python 3.10+**
- **SQLite**（aiosqlite）异步持久化
- **AstrBot** 插件框架

---

## License

MIT
