# edu-cs-bot

## 项目简介

多租户教育平台 AI 客服机器人后端系统：学员/家长通过 WebSocket 接入，机器人识别意图后路由到
知识问答（RAG）、财务查询、平台指令执行、日程提醒、转人工五类能力，全链路异步解耦（网关只管
接入、worker 异步消费队列），具备限流、熔断、降级、幂等、审计、多租户隔离等生产级工程能力。
完整需求见 `docs/REQUIREMENTS.md`，各阶段任务拆解见 `docs/PHASE1.md`~`docs/PHASE5.md`，已知
问题和后续规划见 [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md)。

**运行环境**：开发和测试机器为 i7-12650H（10 核 16 线程）、16GB 内存、512GB NVMe 固态硬盘，
满足题目硬件建议；系统是 Windows 11 + Docker Desktop，本仓库只在这个组合上实测过，Linux/macOS
理论上兼容（Compose 文件和脚本都没有 Windows 专属写法）但未实测；Docker Desktop 默认分给
容器的资源约 16 CPU / 7.6GB 内存，压测报告（见下）是在这个资源上限下跑出来的；项目不使用 GPU。

## 架构概要

完整的架构说明、消息完整路线（去程+回程逐服务逐文件）、关键设计取舍（每条做了什么/为什么/
代价，附代码位置）见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)（`docs/PHASE5.md` 5.5，
本文档写这段时还没开始）。这里先放整体架构图：

```mermaid
graph LR
    Client["浏览器 / mock-im"] -- "WebSocket" --> Gateway["gateway"]
    Gateway -- "鉴权 / 去重 / 发布" --> MQ[("RabbitMQ<br/>inbound.messages")]
    MQ -- "消费（prefetch=32）" --> Worker["worker"]
    Worker -- "流式调用" --> LLM["mock-llm"]
    Worker -- "读写会话/消息" --> DB[("PostgreSQL")]
    Worker -- "PUBLISH im:out:tenant:user" --> Redis[("Redis")]
    Redis -- "SUBSCRIBE" --> Gateway
    Gateway -- "reply_chunk / reply_end" --> Client
    Gateway -. "健康检查" .-> DB
    Gateway -. "健康检查" .-> Redis
    Gateway -. "健康检查" .-> MQ
```

gateway 只做接入层的事（鉴权、校验、去重、投递、回推），不调用 LLM；业务逻辑全在 worker 里。
gateway 和 worker 之间没有直接调用关系，完全靠 RabbitMQ（上行）和 Redis pub/sub（下行）解耦，
这样 worker 可以水平扩展多实例，gateway 也可以独立重启不影响正在处理中的消息。

## 设计假设

gateway 直接作为 IM 长连接入口；mock-im 扮演 IM 客户端（阶段三升级成演示控制台，见下面
`http://localhost:8080` 相关说明），用于演示和手工测试。现实场景里 gateway 前面通常还有一层
真实的 IM/网关接入层，这里为了在阶段一跑通完整链路先简化掉。

演示控制台的 `GET /api/token` 是演示专用接口，模拟"平台登录系统签发 token"这件事——真实环境里
token 应该由平台自己的登录系统签发，gateway 只负责验签（`app.common.auth.decode_access_token`），
不负责签发，签发和验证是两个独立的职责，不应该耦合在同一个服务里。mock-im 之所以要读 JWT 签名
密钥、连数据库查用户角色，是因为它在演示环境里临时扮演的是"签发方"这个角色，仅限这一个用途；
接真实平台之后，`/api/token` 这个接口和 mock-im 这一层都可以整个去掉，gateway 的验签逻辑不用动。

## 意图路由

worker 收到一条消息后，先过 `app/worker/graph/classify.py` 判出意图，再由
`app/worker/graph/graph.py` 路由到对应的业务节点。判定顺序是"规则先行，LLM 兜底"——前面的规则
命中就不再往后走，命中不了的最后交给 LLM function calling；LLM 挂了再降级成 worker 自己的一套
关键词兜底规则（`route_source=rule_fallback`）：

1. 当前会话有未过期（或者刚在有效期内执行完/取消）的待确认操作，且这句话是"确认……"/"取消/算了/
   不用了"这类短句 → `confirm_action` / `cancel_action`（规则）
2. 转人工关键词（转人工、人工客服、找人工、真人） → `handoff`，`trigger=keyword`（规则）
3. 不满意关键词（没用、不对、答非所问、听不懂、不满意）：命中就给会话的 `dissatisfied_count` 加
   1，其它消息清零；第 1 次命中 → `dissatisfied_first`（道歉引导），累计到第 2 次 →
   `handoff`，`trigger=dissatisfied`，并清零计数器（规则）
4. 敏感操作关键词（注销账号、改密码、换绑手机、改银行卡等） → `high_risk` → `sensitive`
   固定拒绝话术（规则）
5. 以上都没命中，交给 LLM function calling：LLM 选中的工具决定意图
   （`search_knowledge`→`knowledge_qa`、`query_finance`→`finance_query`、
   `platform_command`→`platform_command`（高风险动作会被代码改判成 `high_risk`，不是 LLM 自己说
   高风险）、`manage_reminder`→`reminder`、`transfer_to_human`→`handoff`，`trigger=llm`）；
   没选任何工具 → `chitchat`
6. 例外：短句 + 会话里有未过期的待确认操作 + LLM 判出来的结果是 `chitchat`（也就是没判出任何
   真实意图）→ 覆盖成 `confirm_ambiguous`，提醒用户要回复确切的确认短语（比如"对/是的"不算数）；
   这个覆盖故意放在 LLM 分类**之后**，避免把"发票多久能开"这类短句正常问题误判成模糊确认

意图 → 节点的完整映射见 `app/worker/graph/graph.py` 的 `_INTENT_TO_NODE`；`high_risk` 再按
`risk_flags` 是否有 `sensitive_request` 分流到 `sensitive`（固定拒绝）或 `request_confirmation`
（生成待确认操作）。

## 端口（宿主机映射，均可在 `.env` 里改）

| 服务 | 宿主机端口 | 变量 | 备注 |
|---|---|---|---|
| PostgreSQL | 5432 | `POSTGRES_HOST_PORT` | |
| Redis | **6380** | `REDIS_HOST_PORT` | 特意没用默认的 6379——本机很可能已经跑着一个原生安装的 Redis 占用 6379，脚本连 `localhost:6379` 时会悄悄连到那个而不是容器里的这个，排查起来很晕，所以容器对外映射改成 6380（容器内部还是标准的 6379，服务间互联不受影响） |
| RabbitMQ | 5672 | `RABBITMQ_HOST_PORT` | |
| RabbitMQ 管理界面 | 15672 | `RABBITMQ_MANAGEMENT_HOST_PORT` | 浏览器打开 `http://localhost:15672`，账号密码见 `.env` 的 `RABBITMQ_USER`/`RABBITMQ_PASSWORD` |
| gateway | 8000 | `GATEWAY_HOST_PORT` | `/health` `/ready` `/metrics` `/ws` |
| worker 健康检查 | 8011（可扩到 8011-8019） | `WORKER_HEALTH_HOST_PORT` | `/health` `/metrics`；业务逻辑跑在同一进程里，这个端口只做探活；写成范围是为了 `docker compose up -d --scale worker=N` 时每个副本都能各自映射到宿主机，不会因为抢同一个端口启动失败（容器内部固定还是 8001） |
| scheduler 健康检查 | 8002（可扩到 8002-8009） | `SCHEDULER_HEALTH_HOST_PORT` | `/health` `/metrics`；同样是范围端口，理由跟 worker 一致（容器内部固定是 8002） |
| mock-im | 8080 | `MOCK_IM_HOST_PORT` | 演示控制台，浏览器打开 `http://localhost:8080`：顶部切身份（自动签发 token，不用再手工粘贴），左边聊天窗口 + 10 个 E2E 场景快捷键，右边 `reply_end.meta` 透视面板 |
| mock-llm | 8100 | `MOCK_LLM_HOST_PORT` | OpenAI 兼容接口 + `/admin/config`；工具调用走确定性规则（`mocks/mock_llm/rules.py`） |
| mock-knowledge | 8101 | `MOCK_KNOWLEDGE_HOST_PORT` | `/search`（可选检索后端，见下）+ `/admin/config` |
| mock-platform | 8102 | `MOCK_PLATFORM_HOST_PORT` | `/commands`（幂等）、`/users/{id}/subscriptions`、`/admin/commands`、`/agents/status` + `/admin/config` |
| mock-finance | 8103 | `MOCK_FINANCE_HOST_PORT` | `/orders` `/bills` `/invoices` `/refunds` `/balance` + `/admin/config` |

## 快速开始

```bash
cp .env.example .env    # 按需修改，尤其是密码类变量
make up                  # 起基础设施 + gateway/worker/scheduler + 5 个 mock 服务，等全部 healthy
                         # 后自动跑数据库迁移 + 种子数据（两个租户、七个用户）+ 建知识库索引，
                         # 一条命令到位，不用再手动跑 migrate/seed
make demo                # 生成 token -> 发消息看三个耗时 -> 同 message_id 重发看 duplicate
```

`make up` 里的迁移和种子数据都是幂等的：重复执行不会产生重复数据，也不会覆盖已经改过的机构配置
（比如压测/演示中调整过的 `daily_token_budget`）。`make migrate`/`make seed` 仍然保留成独立目标，
只是不需要在启动步骤里手动敲了；改了 `data/knowledge/` 下的文档之后单独重建索引用这个（不用重新
seed，也不用整个重新 `make up`）：

```bash
make reindex             # 等价于 docker compose run --rm tools python scripts/reindex.py
```

### 同一台机器同时跑两份代码

`docker-compose.yml` 顶层写死了 `name: edu-cs-bot`，这个字段的优先级高于目录名——同一台机器上
如果有两份 checkout（比如再 clone 一份到别的目录做对比测试），直接在两边分别 `make up` 会共用
同一套容器名、网络、数据卷，互相覆盖而不会报错。需要同时跑两份时，给其中一份设置
`COMPOSE_PROJECT_NAME` 环境变量隔离：

```bash
COMPOSE_PROJECT_NAME=edu-cs-bot-fresh make up
```

这样这一份用的容器名、网络名、数据卷名都会带 `edu-cs-bot-fresh` 前缀，跟另一份互不干扰；`make
down`/`make test`/`make demo` 等其它目标也要带着同样的环境变量才会操作到对应的那一份。

跑起来之后可以：
- 浏览器打开 `http://localhost:15672`（RabbitMQ 管理界面），看 `inbound.messages`/`inbound.dead` 两个队列
- `make logs` 看所有服务的结构化 JSON 日志
- `docker compose stop mock-llm` 之后再发消息，验证 worker 会推送降级回复且不崩
- `docker compose run --rm tools python scripts/phase2_smoke.py` 把阶段二的 9 个 E2E 场景串起来跑一遍，打印 PASS/FAIL
- `docker compose run --rm tools python scripts/phase3_smoke.py` 把阶段三的场景（提醒推送/修改/取消、上下文摘要、限流、熔断、预算降级）串起来跑一遍
- 浏览器打开 `http://localhost:8011/metrics`（端口以 `docker compose port worker 8001` 实际输出为准）看 worker 的 Prometheus 指标

## 演示控制台（`http://localhost:8080`，mock-im）

- **身份切换**：顶部下拉框选一个身份直接连，token 由控制台后端现场签发（`app.common.auth.
  create_access_token`，`role` 现查数据库，不是前端猜的），不用再手工跑 `gen_token.py` 粘贴。
  切身份会断开旧连接、清空聊天窗口和右侧面板，同一个身份再切回来复用同一个 `conversation_id`
  （历史/摘要/不满意计数接得上）。
- **快捷按钮**：左下角按当前身份分组显示，点一下按顺序发送场景原句，跟 `scripts/
  phase2_smoke.py`/`phase3_smoke.py` 的断言用的是同一批原句。**带 `*` 的按钮**（比如"场景7
  财务超时"、"场景8 LLM非法JSON"）只是发送原句，触发对应降级效果之前要先用 `scripts/
  mockctl.py` 把对应 mock 服务切到故障模式（比如 `mockctl.py finance mode=timeout`），点完
  验证完记得 `mockctl.py <服务> reset` 恢复，否则会影响后面其它场景/压测。**"跨机构政策
  对比"类按钮**（t_a/t_b 各一个，问同一句"退费政策是什么"）需要手动切到另一机构的身份再问
  一次同一句话，才能看出两家机构条款隔离、答案不同——控制台不会自动帮你切身份问第二遍。
- **透视面板**（右侧"处理流程回放"）：点左侧任意一条机器人回复重新播放，展示这条消息实际
  经过的 `app/worker/graph/graph.py` StateGraph 节点、各节点耗时、LLM 耗时、意图/判定来源、
  工具名和结果、知识来源和分数、`worker_id`、熔断/预算/风险标记，字段来源见下面"`reply_end`
  的 `meta` 字段"一节。命中异常判定时顶部会出现红色横条，没有异常不显示。
- **架构路线图**（右侧"处理流程回放"区右上角"架构路线"按钮，阶段五新增，纯前端，只在
  `mocks/mock_im/templates/index.html`）：点开一张覆盖右侧面板的弹层，画的是"浏览器→gateway
  鉴权→Redis限流→Redis去重→RabbitMQ投递→回ACK→RabbitMQ队列→worker取消息→数据库去重→
  worker业务流程→回复写入数据库→Redis推送→gateway→浏览器"这条完整链路（外加 worker 业务
  流程下面 mock-llm/mock-finance/mock-platform/知识库 四个下游图标，以及 scheduler 的提醒
  推送线），跟内部流程图是两张独立的图。每条回复结束后回放一次，圆点固定节拍走一遍真实路径，
  出问题的地方停下标红/标黄（限流/重复/投递失败/连接被拒/Redis degraded 各有专门的判定，
  弹层下方还有一块跟内部流程图完全一致的耗时/工具/知识来源摘要）。点弹层里的"worker 业务
  流程"框或右上角"关闭"都能退出，回到内部流程图。局限见 `docs/KNOWN_ISSUES.md`。
- **记忆区块**：右侧透视面板下方，展示这条会话当前带入的历史原文条数、有没有摘要、摘要内容
  （现查 `conversation_summaries` 表，是"当前最新状态"，不是这条回复发生那一刻的历史快照）。
- **坐席工作台**：切到坐席身份（`u_a_1003`/`u_b_1003`）会顶替聊天窗口，显示本机构的转接
  工单列表和审计日志（财务查询/指令执行），跨机构、非坐席角色一律 403。

## 水平扩展演示

```bash
docker compose up -d --scale worker=3   # 水平扩展 3 个 worker 副本
```

worker 无状态（业务状态全在 PostgreSQL/Redis/RabbitMQ 里），扩容不需要额外配置；每个副本会
分到 `WORKER_HEALTH_HOST_PORT` 起始的一个不同宿主机端口（见上面"端口"一节），`docker compose
ps` 能看到 3 个 `worker` 容器都 `healthy`。验证消息被分派到不同副本：发几条消息后看
`reply_end.meta.worker_id`（取的是容器 hostname），应该会看到不同的值轮流出现。验证完恢复：

```bash
docker compose up -d --scale worker=1
```

## 命令行工具（`scripts/`）

都跑在一次性的 `tools` 容器里（`docker compose run --rm tools python scripts/xxx.py ...`）：

| 脚本 | 用途 |
|---|---|
| `chat.py --tenant t_a --user u_a_1001 --conv c1 "你好"` | 命令行多轮对话：生成 token、连 gateway、发一条消息、打印 ack/流式回复/`reply_end` 的 `meta`。同一个 `--conv` 标签会延续同一个会话（内部用 `uuid5(tenant:user:标签)` 映射成固定的 `conversation_id`）；加 `--message-id <上次的id>` 重发同一条消息可以测试去重 |
| `mockctl.py <llm\|finance\|platform> show` | 打印某个 mock 服务当前的 `/admin/config` |
| `mockctl.py <llm\|finance\|platform> reset` | 把某个 mock 服务恢复成默认配置 |
| `mockctl.py <llm\|finance\|platform> key=value ...` | 改某个 mock 服务的运行时行为，比如 `mockctl.py llm mode=invalid_json`、`mockctl.py finance mode=timeout`、`mockctl.py platform mode=slow_commit`、`mockctl.py platform agents_online=false` |
| `mockctl.py platform show-commands` | 打印 mock-platform 的 `/admin/commands`（已执行过的平台指令，按幂等键去重后的结果），用来验证"同一个幂等键只真正执行一次" |
| `mockctl.py all reset` | 一次性重置 llm/finance/platform 三个 mock，验证脚本跑之前和跑完都应该调这个 |
| `sql.py "select ..."` | 只读地跑一条 SQL 查询并打印结果表格（写操作会被拒绝），用来验证数据库里的最终状态 |
| `finance_probe.py --tenant t_a --acting u_a_1001 --target u_a_1004 --kind invoices` | 绕过 worker，直接以某人身份请求 mock-finance，证明 mock-finance 自己也会独立拒绝越权 |
| `reindex.py` | 增量重建知识库索引（按文件内容哈希判断要不要重算），`--force` 全量重建 |
| `phase2_smoke.py` | 阶段二收尾冒烟测试，串起 9 个 E2E 场景 |
| `phase3_smoke.py` | 阶段三冒烟测试：提醒推送/修改/取消、上下文摘要、限流、熔断降级、预算降级 |
| `rate_limit_burst.py --tenant t_a --user u_a_1001` | 10 秒内连发 N 条消息，打印每条 ack 状态，验证限流阈值 |
| `dlq_replay.py` | 把 `inbound.dead` 里的消息逐条取出、`x-retry-count` 清零后重新投回 `inbound.messages`（根因修好之后找回死信） |
| `seed.py` | 种子数据（可重复跑） |
| `gen_token.py --tenant t_a --user u_a_1001` | 生成一个 JWT；演示控制台自己会现场签发 token，这个脚本主要给 `chat.py`/`sql.py` 之外的手工排查用 |

## 知识库文件格式（`data/knowledge/{tenant_id}/*.md`）

每个租户一个目录，目录下每个 `.md` 文件是一篇文档。格式固定，由 `app/common/knowledge_parser.py`
解析：开头是 YAML front matter（`doc_id`/`title`/`tenant_id`/`tenant_name`/`version`/`updated_at`
都必填，`tenant_id` 必须和所在目录一致，不一致整份文档解析失败），往后 `## ` 表示章、`### ` 表示
条款——条款标题行的第一个词就是条款号（如 `4.2`、`Q3`），后面是条款标题，一个条款切成一个检索块：

```markdown
---
doc_id: service_agreement
title: 课程服务协议
tenant_id: t_a
tenant_name: 星辰教育
version: 2026.09
updated_at: 2026-09-01
---

# 星辰教育《课程服务协议》

## 第二章 课程与课时

### 2.2 排课与课程表
每期课程表在开课前 7 天公布……
```

改完文档跑 `make reindex`（或 `docker compose run --rm tools python scripts/reindex.py`）：按文件
内容哈希判断要不要重算，没变的文档跳过；目录里被删掉的文档，连同它的检索块一起删除；`--force`
参数忽略哈希强制全量重建。

## `reply_end` 的 `meta` 字段

worker 处理完一条消息，最后一帧 `reply_end` 会带一个 `meta` 对象，供客户端调试/前端展示用（字段
定义见 `app/worker/graph/graph.py` 的 `_build_meta`）：

| 字段 | 说明 |
|---|---|
| `intent` | 这轮判出来的意图，见上面"意图路由"一节的取值 |
| `route_source` | 意图是怎么判出来的：`rule`（worker 规则命中）、`llm`（LLM function calling）、`rule_fallback`（LLM 调用失败，降级成 worker 自己的关键词规则） |
| `tools` | 这轮尝试解析/调用过的工具，`[{"name": ..., "status": ...}]`；`status` 常见取值：`ok`/`forbidden`/`invalid_json`/`schema_error`/`upstream_error`/`not_found`/`pending_confirmation`/`need_clarification`/`no_op` 等，具体含义看各业务节点 |
| `citations` | 知识问答命中的检索结果（`doc_title`/`clause_no`/`score`），非知识问答场景是空数组 |
| `pending_action_id` | 这轮涉及的待确认操作 id（生成/确认/取消/模糊提醒都会带），不涉及则为 `null` |
| `handoff_ticket_id` | 这轮生成的转人工记录 id，不涉及则为 `null` |
| `guard.dropped_sentences` | OutputGuard 因为出处核对不通过而整句丢弃的句子数（只有知识问答场景会 >0） |
| `guard.banned_phrases_removed` | OutputGuard 删掉的禁用套话（"作为AI"之类）出现次数 |
| `risk_flags` | 这轮命中的风险标记，如 `sensitive_request`、`prompt_injection_suspected`；转人工记录里还会额外汇总 `finance_forbidden_attempt`、`repeated_dissatisfaction`、`high_risk_pending`（见 `app/worker/graph/handoff.py`） |
| `context.history_messages` / `context.has_summary` | 阶段三第 3 步：这轮 LLM 请求带了几条原文历史、有没有附带历史摘要 |
| `circuit_breaker` | 阶段三第 5 步：这轮因为熔断打开被跳过、没真的发请求的服务名，如 `["llm"]`、`["finance"]`，没有就是空数组 |
| `budget_exceeded` | 阶段三第 6 步：机构今日 token 预算是否已经用完导致这轮跳过了 LLM 调用 |

## 目录说明

```
app/
  common/      # gateway/worker/scheduler 共用：config/logging/db/redis/mq/auth/schemas/models/
               # masking/permissions/tools/platform_client/finance_client/retrieval/embedding/...
  gateway/     # WebSocket 接入层：鉴权、去重、投递 MQ、ACK、Redis 推送转发（不调用 LLM）
  worker/      # 消费队列：LangGraph 编排（意图路由 + 业务节点）、调 LLM、写回复、推流式分片
    graph/     # classify/knowledge/finance/command/handoff/guard/style 等节点实现
  scheduler/   # 独立服务：每秒扫一次到期提醒，FOR UPDATE SKIP LOCKED 取批、先推送再提交
mocks/
  mock_im/         # 演示控制台：身份切换（现场签发 token）、聊天窗口、场景快捷键、meta 透视面板
  mock_llm/        # OpenAI 兼容的假 LLM，工具调用走确定性规则（rules.py），行为可通过 /admin/config 调整
  mock_knowledge/  # 假检索后端（可选，RETRIEVER=mock_knowledge 时启用）
  mock_platform/   # 假平台指令系统：低风险/高风险指令、幂等键、故障模式
  mock_finance/    # 假财务系统：订单/账单/发票/退费/余额，自己也做一层越权校验
docker/
  app.Dockerfile   # gateway / worker / scheduler / tools 共用
  mocks.Dockerfile # 5 个 mock 服务共用；镜像里也拷贝了 app/（阶段三第 7 步加的），
                   # 但只有 mock-im 用得到（现场签发 token 要用 app.common.auth/db），
                   # 其余 4 个 mock 服务不引用这些模块
migrations/    # Alembic 迁移
scripts/       # 命令行工具，见上面"命令行工具"一节
loadtest/      # k6 压测脚本 + lib/（长连接客户端、token 加载）
eval/          # LLM 质量评测（阶段五 5.3/5.4）：cases.jsonl（50 条测试集）、SCORING.md（打分口径）、
               # scoring.py（纯规则打分函数）、db_checks.py（数据库/mock-platform 验证）、
               # run_eval.py（评测脚本）、output/（每题明细，.gitignore 排除）
tests/unit/    # 纯函数和轻量假对象单测（pytest + pytest-asyncio），不连真实数据库；
               # 涉及数据库/mock 服务的验证走 docker compose 起真实服务手工/脚本验证，见各步骤文档
docs/          # REQUIREMENTS.md（需求原文）、PHASE*.md（各阶段任务拆解）、phase2_threshold.md（检索阈值标定）、
               # LOADTEST.md/FAULT_INJECTION.md/KNOWN_ISSUES.md（阶段四/五产出的报告类文档）
```

## Makefile 目标

| 目标 | 作用 |
|---|---|
| `make up` | 启动基础设施 + gateway/worker/scheduler + 5 个 mock 服务（不含 `tools`，见下），等全部 healthy 后自动跑 `migrate` + `seed`，幂等，可重复执行 |
| `make down` | 停止所有服务 |
| `make logs` | 跟着看所有服务日志 |
| `make ps` | 看各服务状态 |
| `make migrate` | 跑 Alembic 迁移 |
| `make seed` | 种子数据（可重复跑）+ 建知识库索引 |
| `make reindex` | 只重建知识库索引，不重新种子数据 |
| `make demo` | 见上面"启动步骤" |
| `make test` | 单测（`tools` 镜像）+ mock-llm 专属单测（`mocks-tools` 镜像，见下）+ 集成 + e2e，共四层 |
| `make loadtest-users` | 生成压测用户 + token（t_a 下 1600 个，写进不进 git 的 `loadtest/tokens.json`）。下面四个场景目标都会自动依赖这个目标，不需要手动单独跑；每次都会重新生成（用户是幂等的，token 6 小时过期，重新生成保证不会拿到过期 token） |
| `make loadtest-steady` | 压测场景 1：稳定（500 连接，200 msg/s，5 分钟） |
| `make loadtest-burst` | 压测场景 2：突发（≥1000 VU，1000 msg/s，30 秒，另跑 3 分钟观察队列消化） |
| `make loadtest-finance` | 压测场景 3：财务查询（100 QPS，2 分钟） |
| `make loadtest-llm-timeout` | 压测场景 4：LLM 超时率 20%（内部用 `mockctl.py` 设置/重置，`trap` 保证不管成败都会重置） |
| `make loadtest` | 依次跑完上面四个场景 |
| `make eval` | 先跑打分函数单测（`tests/unit/test_eval_scoring.py`），再跑 `eval/run_eval.py`（50 道题走真实 WebSocket，前提是已经 `make up` 且两家机构今日 LLM token 预算没用完），明细写到 `eval/output/`（不进 git），报告见 `docs/EVAL_REPORT.md` |

`migrate`/`seed`/`reindex`/`demo` 实际上跑在一个叫 `tools` 的一次性容器里（跟 gateway/worker 共用
同一个镜像），`docker-compose.yml` 里给它设了 `profiles: ["tools"]`，所以 `make up` 不会把它一起
启动，只有 `docker compose run --rm tools ...` 显式点名才会临时起一个，干完活自动退出，不会一直占
资源；上面"命令行工具"一节列的脚本都是这样跑的。

## 新增的环境变量（知识检索与工具调用）

完整列表和注释见 `.env.example`，这里只列阶段二新增、且不是"密码/密钥"类的关键项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `EMBEDDING_PROVIDER` | `hash` | 知识库向量化方式，目前只有确定性哈希向量一种实现，不依赖网络/模型 |
| `RETRIEVER` | `pgvector` | 检索后端：`pgvector`（主路径）或 `mock_knowledge`（验证"检索也可以是外部系统"） |
| `KNOWLEDGE_MIN_SCORE` | `0.30` | pgvector 检索分数低于这个值就认为没查到明确依据，直接走固定话术，不调用 LLM |
| `MOCK_KNOWLEDGE_MIN_SCORE` | `0.04` | mock_knowledge 检索器的阈值，打分尺度和 pgvector 不同，单独标定，见 `docs/phase2_threshold.md` |
| `FINANCE_TIMEOUT_SECONDS` | `1.5` | worker 调 mock-finance 的超时；只对超时/5xx/连接错误重试 1 次，403/401 不重试 |
| `MOCK_FINANCE_MODE` | `normal` | mock-finance 故障模式：`normal`/`timeout`/`error500`，用 `mockctl.py finance mode=...` 运行时切换 |
| `PLATFORM_TIMEOUT_SECONDS` | `3` | worker 调 mock-platform 的超时；只对超时/5xx/连接错误重试，最多 2 次，间隔 0.5s/1s |
| `MOCK_PLATFORM_MODE` | `normal` | mock-platform 故障模式：`normal`/`timeout`（永久挂起，验证重试耗尽）/`slow_commit`（延迟后成功，验证幂等键找回结果）/`error500` |
| `PENDING_ACTION_TTL_SECONDS` | `300` | 高风险指令待确认操作的有效期，超过还没确认就按超时处理 |

## 新增的环境变量（提醒、限流与熔断）

同样只列关键项、不是"密码/密钥"类的，完整列表见 `.env.example`：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CONVERSATION_HISTORY_LIMIT` | `10` | 最近几条消息原样保留，更早的压成摘要；摘要超过这个阈值的未覆盖消息数才重新生成 |
| `SCHEDULER_INTERVAL_SECONDS` | `1` | scheduler 扫描到期提醒的间隔 |
| `SCHEDULER_BATCH_SIZE` | `100` | scheduler 每轮 `FOR UPDATE SKIP LOCKED` 最多取几条 |
| `RATE_LIMIT_USER_PER_10S` | `20` | 单用户 10 秒内最多几条消息，超了 ack 返回 `rate_limited` |
| `RATE_LIMIT_TENANT_PER_SEC` | `2000` | 单机构每秒最多几条消息 |
| `REDIS_RECONNECT_MIN_SECONDS` / `REDIS_RECONNECT_MAX_SECONDS` | `0.5` / `30` | gateway 订阅 Redis 频道断线重连的退避区间（翻倍增长，封顶后面这个值） |
| `CB_FAILURE_THRESHOLD` / `CB_OPEN_SECONDS` | `5` / `30` | LLM、mock-finance 各自的熔断器：连续失败几次打开、打开多久后进入半开试探 |
| `LLM_MAX_RETRIES` / `FINANCE_MAX_RETRIES` / `PLATFORM_MAX_RETRIES` | `1` / `1` / `2` | 各自的重试次数上限（重试之间的退避间隔另有单独的配置项，见 `.env.example`） |
| `DLQ_MAX_RETRIES` | `3` | 消息处理时出现意外异常，重新入队几次还失败就进 `inbound.dead` |
| `DEFAULT_DAILY_TOKEN_BUDGET` | 空（不限额） | 机构没在 `tenants.daily_token_budget` 单独设置时用这个值 |

## 新增的环境变量（LLM 超时拆分）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_NONSTREAM_TIMEOUT_SECONDS` | `3` | classify 这类非流式 LLM 调用的超时，超时不重试（阶段四故障注入把原来的 `LLM_TIMEOUT_SECONDS=15` 拆成非流式/流式两个更短的值，重试也从"超时重试一次"改成"超时不重试"） |
| `LLM_STREAM_TIMEOUT_SECONDS` | `4` | respond 阶段流式生成的超时，超时不重试；比非流式多给 1 秒是因为流式要先等首字节，理由见 `loadtest/llm_timeout.js` 顶部注释 |

## 测试结果摘要

`make test` 四层（前提：已经 `make up`），数字取自 2026-09-27 全新克隆复验（详见
`AGENT_LOG.md`"步骤 5.1"）：
- 单元测试：238 passed，2 skipped（核心模块覆盖率 92%，见 `make test` 输出的 `--cov-report`）
- mock-llm 专属单测：42 passed
- 集成测试：11 passed
- E2E（题目 6.3 十个场景）：10 passed

CI（`.github/workflows/ci.yml`）在 push 到 `main` 时自动跑单元测试这一层；integration/e2e
依赖完整 Docker Compose 环境，本地/演示环境手动跑 `make test`。

## 压测结果摘要

四个场景（稳定/突发/财务查询/LLM 超时率 20%）的脚本在 `loadtest/`，跑法见上面 Makefile
目标表。压测跑了两轮：第一轮的 k6 脚本是"每条消息各自建一次连接"的短连接模型，且跑的时候
t_a 机构的每日 LLM token 预算被打满，两个问题都让结果测不到题目原本要测的路径，已作废；
第二轮把 `loadtest/lib/ws_client.js` 改成长连接模型（每个 VU 建一条连接、持续发消息，
`message_id`/`reply_to` 匹配每条在途消息），解除了预算限制，是当前有效结果，其中场景 1 额外
对比了 1 个 worker 和 3 个 worker 的差异。

跟题目指标逐条对比（第二轮，最终结果，数字取自 `docs/LOADTEST.md`"和题目指标逐条对比"一节）：
- 稳定（500 连接/200msg/s/5 分钟）：不达标，1 worker 约 10/s、3 worker 约 37/s——worker 单核
  CPU 是瓶颈，水平扩展基本线性有效，Postgres 是下一个瓶颈
- 突发（≥1000 VU/1000msg/s/30 秒）：部分达标，并发数和发送速率达标、**零丢失**，但积压要
  12~13 分钟才能消化完
- 财务查询（100 QPS/P95<500ms）：不达标，`mock-finance` 本身 P95 只要 286ms（达标），瓶颈是
  排队等 worker，不是财务接口慢
- LLM 超时率 20%（降级且不崩溃）：**达标**，不崩溃、降级正确、熔断正确触发并自愈、无丢失

详细数字、方法、单 worker 吞吐上限的原因分析、下一个瓶颈（Postgres）都写在
[`docs/LOADTEST.md`](docs/LOADTEST.md)，不在这里重复。

## 评测结果摘要（阶段五 5.4）

LLM 质量评测（`eval/cases.jsonl` 50 条，走真实 WebSocket 链路，用 mock-llm）跑了两遍，
两遍的五个指标分子分母完全一致（可复现）：
- 事实准确率 28/33（84.8%）
- 引用命中率 7/12（58.3%）
- 越权拒绝率 5/5（100.0%），合法查询误拒 0/6
- 无依据拒答率 3/5（60.0%）
- 少 AI 味评分：平均 95.8 分，80 分以上占 86.0%（43/50）
- 转人工准确率 50/50（100.0%），误转/漏转均为 0

失败的 9 道题全部归因"系统问题"（主要是 `EMBEDDING_PROVIDER=hash` 的检索排序/假阳性问题，
外加一处敏感关键词不支持词序变化）或"mock 局限"（mock-llm 已知的关键词碰撞），没有"题目
预期有误"的情况。详细的每题证据、归因、评测过程中改过的题目（改前/改后/原因）见
[`docs/EVAL_REPORT.md`](docs/EVAL_REPORT.md)。

## 已知问题

见 [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md)：每条写现象、影响、为什么没做、后续怎么做，
来源包括 `AGENT_LOG.md` 里记录的已知问题、各阶段文档里砍掉/简化的内容、架构上已知的局限。

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/REQUIREMENTS.md` | 题目原文 |
| `docs/PHASE1.md` ~ `docs/PHASE5.md` | 各阶段任务拆解，coding agent 按这些文档小步实施 |
| `docs/phase2_threshold.md` | 知识检索阈值（`KNOWLEDGE_MIN_SCORE`/`MOCK_KNOWLEDGE_MIN_SCORE`）标定过程 |
| `docs/LOADTEST.md` | 压测报告（方法、四场景结果、跟题目指标逐条对比、已知问题） |
| `docs/FAULT_INJECTION.md` | 故障注入命令与验证记录（阶段四 4.5） |
| `docs/KNOWN_ISSUES.md` | 已知问题与后续规划 |
| `docs/ARCHITECTURE.md` | 架构图、消息完整路线、关键设计取舍（5.5，待创建） |
| `docs/API.md` | WebSocket/HTTP 接口文档（5.5，待创建） |
| `docs/CHECKLIST.md` | 题目要求逐条对照（5.5，待创建） |
| `docs/EVAL_REPORT.md` | LLM 质量评测报告（5.4：五个指标、每道失败题的证据和归因、评测过程中改过的题目） |
| `eval/SCORING.md` | 评测五个指标的打分口径 |
| `AGENT_LOG.md` | coding agent 使用记录，见下一节 |

## coding agent 使用说明

这个项目由 coding agent（Claude Code）按 `docs/PHASE1.md`~`docs/PHASE5.md` 小步实施，每步
完成后停下等人工审查、确认后再继续，不自行扩大范围。完整记录见 [`AGENT_LOG.md`](AGENT_LOG.md)：
- 文件开头"总览"：关键 prompt、agent 生成/修改的模块（按阶段）、人工审查与修复点、agent 做错
  或需要重写的部分（阶段五收尾时补齐，见 `docs/PHASE5.md` 5.6）
- "审查故事索引"：全部【人工审查发现】【agent 做错】【agent 自查修复】条目，每条固定写"agent
  做了什么、发现了什么问题、为什么是问题、怎么修改/怎么验证"
- 正文按步骤记录改了哪些模块、关键设计决策
