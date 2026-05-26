# TierMem

AstrBot 插件 —— 无需rag的记忆系统。

---

## 记忆机制

TierMem 采用 **三层记忆 + 近期对话缓存** 的架构，让 Bot 既能记住用户的长期画像，也能感知近期上下文。

### 三层记忆

| 层级 | 作用 | 注入策略 |
|------|------|----------|
| **Important** | 核心画像、关键偏好、长期任务 | 始终注入 |
| **General** | 普通事实、常规信息 | 按需注入 |
| **Fleeting** | 临时内容、短期事件 | 按需注入，T+3 轮自动淘汰 |

- 每层有独立的容量上限（默认 50 条），超出时按配置策略处理（**浓缩** 或 **淘汰**）。

### FIFO 对话缓存

- 针对每个用户维护一个 **固定长度的对话轮次队列**（默认 10 轮）。
- **群聊**：N 轮近期对话注入到系统提示词中，让 Bot 回忆起近期与当前用户的对话。
- **私聊**：不注入 FIFO，避免重复。
- 当 FIFO 达到阈值时，**自动触发异步总结**：将 N 轮对话 + 现有三层记忆发送给 LLM，生成更新后的记忆，然后清空 FIFO。
- 总结在后台执行，**不阻塞 Bot 的正常对话响应**。
- 总结失败时自动 trim FIFO 保留核心数据，连续 3 次失败清空 FIFO 防止死循环。

### 双轨记录

1. **自动总结（主轨道）**：系统自动沉淀对话为结构化记忆。
2. **工具调用（辅轨道）**：LLM 拥有 `memory_add` / `memory_update` / `memory_delete` 工具，用于即时记录关键信息。提示词中会提醒 LLM 谨慎使用，避免与自动总结冲突。

### 溢出处理策略

| 策略 | 说明 |
|------|------|
| **evict**（默认） | 按 `(importance 升序, updated_at 升序)` 硬淘汰最不重要、最旧的记忆 |
| **condense** | 调用 LLM 合并语义相似的记忆，目标 **削减 35% 总字符数**，允许剔除低重要性记忆。最多 3 轮重试，不达标回退到淘汰 |

### 跨用户记忆

总结时支持两阶段调用：

1. **T1 检测**：LLM 分析对话是否提及其他用户并需要更新其记忆
2. **T2 写入**：读取目标用户现有记忆后，同时输出对当前用户和其他用户的增删改操作

跨用户记忆引用使用 `{{uid:用户ID}}` 占位符，在注入主 LLM 时自动替换为当前昵称，避免用户改名后记忆失效。

### 备份与回滚

- 每次总结前自动创建 SQLite VACUUM 备份，保留最近 5 个。
- 通过 `/memory rollback` 命令可回滚到最新备份（自动关闭并重建数据库连接）。

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

重启 AstrBot，插件会自动安装依赖并初始化 SQLite 数据库（WAL 模式）。

---

## 配置

在 AstrBot 管理面板的插件配置中，可调整以下项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `memory_mode` | 记忆模式：`global`（群独立）/ `shared`（跨群共享） | `global` |
| `fifo_size` | FIFO 缓存对话轮数阈值 | `10` |
| `max_memory_per_layer` | 每层记忆最大条数 | `50` |
| `memory_overflow_policy` | 超限处理：`evict`（淘汰）/ `condense`（浓缩） | `evict` |
| `fleeting_ttl_rounds` | fleeting 记忆存活轮数 | `3` |
| `max_concurrent_summaries` | 最大并发总结任务数 | `2` |
| `summary_provider_id` | 总结用 LLM Provider ID，留空使用主模型 | `""` |
| `summary_system_prompt` | 总结任务的额外系统提示词 | `""` |
| `summary_search_replace_prompt` | search_replace 模式提示词模板（留空用默认） | `""` |
| `summary_full_replace_prompt` | full_replace 模式提示词模板（留空用默认） | `""` |
| `inject_fifo_in_group` | 群聊时是否注入 FIFO 对话 | `true` |
| `inject_memory_in_private` | 私聊时是否注入三层记忆 | `true` |
| `inject_layers_in_group` | 群聊时注入的记忆层 | `important_only` |
| `enable_auto_summary` | 是否启用 FIFO 满自动总结 | `true` |
| `enable_manual_summary` | 是否允许手动触发总结 | `true` |
| `enable_llm_tools` | 是否向 LLM 暴露记忆工具 | `true` |
| `tool_caution_in_prompt` | 是否在提示词中附加工具使用警告 | `true` |

---

## 命令

| 命令 | 说明 |
|------|------|
| `/memory sum` | 立即手动触发总结 |
| `/memory check [layer]` | 查看自己的记忆，`layer` 可选 `important` / `general` / `fleeting` |
| `/memory check @user_id [layer]` | 管理员查看指定用户的记忆 |
| `/memory status` | 查看 FIFO 和三层记忆的统计状态 |
| `/memory fifo` | 查看当前用户的 FIFO 对话缓存内容 |
| `/memory condense` | 手动触发记忆浓缩（对 important / general 层） |
| `/memory clear` | 清除自己的所有记忆和对话缓存 |
| `/memory rollback` | 回滚到上次备份 |
| `/memory admin_clear <user_id\|all>` | 管理员清除指定用户或所有记忆 |
| `/memory help` | 显示帮助 |

---

## 项目结构

```
astrbot_TierMem/
├── main.py                  # 插件入口，事件钩子 + 命令 + 工具注册
├── core/
│   ├── config.py            # 配置项定义
│   ├── models.py            # 数据模型（MemoryEntry, MemoryState, SummaryResult 等）
│   └── exceptions.py        # 异常体系
├── storage/
│   ├── database.py          # SQLite 连接管理（WAL 模式 + 事务支持）
│   ├── memory_repo.py       # 记忆 CRUD（含事务性 replace）
│   ├── fifo_repo.py         # FIFO 对话缓存 CRUD
│   └── migration.py         # global ↔ shared 模式迁移
├── service/
│   ├── summarizer.py        # 总结引擎（两阶段多轮 + 重试 + 浓缩）
│   ├── injector.py          # Prompt 注入器（含 {{uid:xxx}} 昵称替换）
│   └── backup.py            # 备份与恢复服务
├── api/
│   ├── commands.py          # 命令处理器
│   └── tools.py             # LLM 工具方法（memory_add/update/delete/read_user）
├── utils/
│   ├── id_gen.py            # ID 生成
│   ├── json_helper.py       # JSON 安全解析（含 json-repair）
│   └── subject.py           # subject_id 统一构造
├── prompts/                 # 提示词扩展目录
├── _conf_schema.json        # WebUI 配置 schema
├── metadata.yaml            # 插件元信息
├── requirements.txt         # 依赖（aiosqlite, json-repair）
├── PROMPTS.md               # 提示词汇总文档
└── README.md
```

---

## 技术栈

- **Python 3.10+**
- **SQLite**（aiosqlite）— WAL 模式，异步持久化
- **AstrBot** 插件框架
- **json-repair** — LLM 输出 JSON 修复

---

## License

MIT
