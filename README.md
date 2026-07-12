# TierMem v2

TierMem 是一个面向 AstrBot 的长期记忆插件，使用“多层原子记忆 + 关系知识图谱”保存用户信息。

它不依赖 Embedding 或向量数据库，只使用 SQLite、FTS5 全文检索和受约束的图查询完成记忆召回。

## 基本原理

TierMem 将长期信息拆成两部分：

- **单用户记忆**：用户自身的身份、偏好、经历和短期事项，以独立原子保存。
- **关系知识图谱**：用户与其他用户、群聊、项目、组织、话题之间的关系。

一次完整的记忆循环如下：

```text
近期对话进入 FIFO
        ↓
达到条数阈值或等待时间上限
        ↓
LLM 提取原子记忆、实体、关系和证据
        ↓
写入 SQLite，并从写入时开始衰减
        ↓
新消息先召回相关原子，再由原子进入知识图
        ↓
相关记忆与关系注入主模型提示词
```

## 四层原子记忆

每条记忆只表达一个事实，并根据用途进入不同层级：

| 层级 | 内容 | 默认半衰期 |
|---|---|---:|
| `core` | 身份、长期目标、稳定偏好 | 不自动衰减 |
| `semantic` | 一般事实和长期知识 | 180 天 |
| `episodic` | 带时间背景的经历与关系证据 | 45 天 |
| `working` | 当前任务和短期事项 | 7 天 |

记忆和关系都保存强度、置信度、稳定性与最后确认时间。读取时动态计算有效强度：

```text
有效强度 = 原始强度 × exp(-衰减率 × 稳定性系数 × 经过天数)
```

普通召回不会重置衰减。只有同一事实再次出现、被明确确认或强化时，才会更新衰减锚点。

## 无 Embedding 召回

TierMem 不会直接用关键词在知识图上盲目游走，而是先检索记忆原子，再把原子作为图入口：

```text
当前消息
  → FTS5 trigram 检索当前用户原子
  → 无结果时使用 LIKE 子串匹配
  → 仍无结果时使用少量重要原子兜底
  → 原子定位相关实体和证据关系
  → 合并 UID、@ 和实体别名精确匹配
  → 扩展一至两跳可见关系
  → 按文本相关性、衰减、重要度、置信度和跳数排序
```

中文检索优先使用 SQLite FTS5 `trigram` tokenizer。如果当前 SQLite 不支持该功能，插件会自动降级到 LIKE，不会影响启动。

所有原子检索都限定当前用户；群聊记忆和关系还会继续按照会话上下文隔离。

## FIFO 总结

近期对话按“用户 + 会话上下文”分别进入 FIFO。满足任一条件便会触发后台总结：

- 对话数量达到 `fifo_size`。
- 最老一轮等待超过 `fifo_max_wait_minutes`。

总结模型只输出结构化操作，由插件在同一个事务中更新记忆、实体、关系和证据。关系证据也会自动保存为可衰减的 `episodic/relation` 原子。

## 被动群聊观察

TierMem 可以旁听没有直接唤醒 Bot 的普通群消息，并在后台形成群公共记忆。该功能：

- 默认关闭。
- 启用后默认使用白名单；白名单为空时不会捕获任何群。
- 可以切换为黑名单模式；黑名单为空表示允许所有群，插件会输出明确警告，需要谨慎使用。
- 不会唤醒 Bot，也不会阻塞正常消息处理。
- 跳过 Bot 自己的消息、命令、过短消息和重复消息。
- 每个群拥有独立缓冲区，达到消息数量或等待时间上限后自主总结，无需等待 Bot 回复。
- 长期摘要归属于 `group:群ID`，不会写入某个成员的私人记忆。
- 群聊召回会同时检索当前用户原子和当前群原子；群记忆不会在私聊或其他群中泄漏。

在控制台“运行配置 → 被动群聊观察”中可以设置名单模式、群 ID、总结阈值、最大等待时间、缓存上限和近期消息注入数。

实现上，AstrBot 过滤器只复制一份不可变消息快照并立即返回；后续名单判断、SQLite 写入、条数触发、截止时间调度和失败重试均由 TierMem 自己的 `GroupObserver` 在后台完成。

## 可视化控制台

AstrBot 新版可以自动发现 `pages/tiermem-console/index.html`。控制台提供：

- 记忆、实体、关系和 FTS5 状态总览。
- 可交互知识图谱及关系表格。
- 原子记忆查询与衰减后强度展示。
- 可解释召回实验室，逐层显示“查询 → 原子 → 实体/证据 → 图关系”。
- FIFO、召回、衰减和功能开关配置。

## 安装

```bash
cd /path/to/astrbot/plugins
git clone https://github.com/OaHaij666/astrbot_plugin_TierMem.git
```

依赖 Python 3.10+、`aiosqlite` 和 `json-repair`。重启 AstrBot 后插件会自动创建 SQLite 数据库。

`v2.0.1` 起内部模块使用插件包命名空间，可在 AstrBot 热更新时避开旧版顶层模块缓存。

v2 不迁移旧格式数据，检测到旧数据库结构时会重新建库。

## 测试

离线测试不调用模型，也不需要 API key：

```bash
python scripts/run_tests.py
```

真实集成测试会调用 DeepSeek，并覆盖“模型连接 → FIFO 对话总结 → 原子和关系写入 → FTS5 召回 → 知识图扩展”的完整流程：

```bash
# Windows PowerShell
Copy-Item .env.example .env

# Linux / macOS
cp .env.example .env

# 编辑 .env，填写 DEEPSEEK_API_KEY 和 ASTRBOT_ROOT
python scripts/run_tests.py --live
```

`.env` 已被 Git 忽略，仓库只提供不含密钥的 `.env.example`。真实测试默认关闭，必须显式使用 `--live` 才会调用模型。

## 许可

TierMem 使用 [MIT License](LICENSE)。

### 使用旧版

需要继续使用重构前版本的用户，可以安装 `v1-legacy` 分支：

```bash
git clone -b v1-legacy https://github.com/OaHaij666/astrbot_plugin_TierMem.git
```

## 常用命令

| 命令 | 说明 |
|---|---|
| `/memory sum` | 立即总结当前 FIFO |
| `/memory check [layer]` | 查看当前用户的原子记忆 |
| `/memory graph` | 查看当前用户的关系 |
| `/memory fifo` | 查看尚未总结的近期对话 |
| `/memory status` | 查看记忆与关系统计 |
| `/memory clear` | 清除当前用户的数据 |
| `/memory rollback` | 管理员回滚数据库备份 |
| `/memory help` | 查看命令帮助 |
