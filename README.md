# TierMem v3

AstrBot 的用户原子记忆与关系知识图谱插件。

## 核心模型

TierMem 把两类信息明确分开：

- **个人记忆**：只描述当前用户自身，以独立原子事实保存。
- **知识图谱**：描述用户、群聊、项目、组织、话题等实体之间的关系。

个人记忆分为四层：

| 层级 | 用途 | 默认半衰期 |
|---|---|---:|
| `core` | 身份、长期目标、稳定偏好 | 不自动衰减 |
| `semantic` | 一般事实与知识 | 180 天 |
| `episodic` | 有时间背景的事件 | 45 天 |
| `working` | 当前任务与短期事项 | 7 天 |

每条记忆和关系都包含置信度、强度、稳定性、最后确认时间和衰减率。系统不会定时改写所有分数，而是在检索时计算当前有效强度：

```text
effective_strength = strength × exp(-decay_rate × stability_factor × age_days)
```

同一事实再次出现时会强化已有记录，而不是重复插入。

## 工作流程

1. 聊天前使用 FTS5 trigram 检索当前用户的相关原子记忆。
2. 原子检索无结果时依次降级到 LIKE 子串匹配和重要原子 Top-N。
3. 由原子的实体映射和证据映射进入知识图，再做受约束的一至两跳扩展。
4. 将检索结果与群聊中尚未总结的 FIFO 对话注入系统提示词。
5. 对话积累到阈值后，后台调用总结模型。
6. 总结模型分别输出 `memory_operations` 和 `relation_operations`。
7. 插件在一个数据库事务中更新记忆、实体、关系及关系证据原子。

除了条数阈值，FIFO 还有最大等待时间（默认 30 分钟）。最老一轮超过该时间后，
即使队列尚未达到 `fifo_size` 也会触发总结；插件启动后会继续扫描数据库中的积压队列。

关系只允许通过当前用户的一跳邻域写入。个人记忆工具也只能修改当前用户的数据。

## 无向量图谱召回

TierMem 不依赖 embedding。召回顺序为：

```text
query → FTS5 原子 → LIKE 降级 → 重要原子兜底
      → 原子关联实体/证据边 → 精确实体并行匹配
      → 一至两跳受约束扩展 → 可解释排序
```

中文索引优先使用 SQLite FTS5 `trigram` tokenizer；当前 SQLite 不支持时自动切换为
LIKE，不会阻断插件启动。关系评分综合原子文本名次、原子有效强度、重要度、置信度、
关系有效强度、图跳数、精确实体与关系意图。普通读取不会重置衰减锚点。

## WebUI 控制台

AstrBot 新版会自动发现 `pages/tiermem-console/index.html`。控制台包含系统总览、
知识图谱网络图与关系表、原子记忆列表、可解释召回实验室和运行配置编辑器。
页面通过官方 `AstrBotPluginPage` bridge 调用插件后端 API，并跟随 Dashboard 亮暗主题。

## 数据表

- `memories`：原子记忆与衰减参数
- `entities`：用户、群聊、项目等实体
- `relations`：带有效期和可见范围的关系边
- `relation_evidence`：关系对应的原始对话证据
- `memory_entity_mentions`：原子到图实体的入口映射
- `memories_fts`：由触发器同步的中文 trigram 全文索引
- `fifo_buffer`：等待总结的近期对话

当前 schema v4 不迁移旧数据库；检测到其他 schema 时会直接重建。数据库保存在
AstrBot 数据目录下的 `tiermem/tiermem.db`。

## 命令

| 命令 | 说明 |
|---|---|
| `/memory sum` | 手动触发总结 |
| `/memory check [layer]` | 查看当前用户原子记忆 |
| `/memory graph` | 查看当前用户一跳关系 |
| `/memory fifo` | 查看待总结对话 |
| `/memory status` | 查看各层数量和关系数量 |
| `/memory clear` | 清除自己的记忆、FIFO 和关系 |
| `/memory rollback` | 管理员回滚整个数据库 |
| `/memory help` | 查看帮助 |

## 安装

```bash
cd /path/to/astrbot/plugins
git clone https://github.com/OaHaij666/astrbot_TierMem.git
```

依赖：Python 3.10+、`aiosqlite`、`json-repair`。
