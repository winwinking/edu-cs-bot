# 阶段三：进阶能力

目标：提醒、上下文、高并发保护、成本与指标全部到位，E2E 场景 5 跑通，演示控制台可用。

---

## 一、给 CC 的工作要求

### 开工前

- `/clear` 后依次读：`CLAUDE.md`、`docs/REQUIREMENTS.md`、`AGENT_LOG.md`、本文件。题目原文在 `docs/REQUIREMENTS.md`，一切以它为准。
- `docs/PHASE1.md`、`docs/PHASE2.md` 是历史任务单，只作参考，不要按它们改代码。和仓库不一致时以仓库为准。种子用户以这里为准：
  - t_a 星辰：u_a_1001 学生（春季数学班）、u_a_1002 家长（关联 u_a_1001）、u_a_1003 坐席、u_a_1004 学生
  - t_b 启明：u_b_1001 学生（春季英语班）

### 工作方式

- 共 7 步（第 0 步开工检查已完成）。按顺序做，在 4 个检查点停下汇报，等 Jo 确认后再继续：
  - 检查点 A：第 2 步做完
  - 检查点 B：第 3 步做完
  - 检查点 C：第 5 步做完
  - 检查点 D：第 7 步做完
- 汇报时每一步分开写，包含四部分：
  1. 改了哪些文件，每个文件一句话说明改了什么。
  2. 计划外改动单独列出。改到多个服务共用的文件（Dockerfile、docker-compose.yml、Makefile、CLAUDE.md、共享模块）时，说明影响了哪些服务。
  3. 每条验证：命令 + 原样输出。不能只写"验证通过"。输出很长时只贴关键行，并说明省略了什么。
  4. 想记为已知问题的，列在"建议记为已知问题"下面，由 Jo 决定，不要自己写进 AGENT_LOG 的已知问题。
- 给 Jo 的命令必须能在 Windows cmd 里直接执行：完整、不留占位符、不用 bash 专属语法（如 `$(...)`、单引号包 JSON）。
- 沿用 CLAUDE.md 的所有长期规则。本阶段特别注意：外部调用必须设超时；重试必须有上限；新 SQL 全部参数化并带 tenant_id 条件；日志不打印 token、手机号、身份证、银行卡；用户说的话不进 system prompt；LLM 输出不直接执行。
- 不要再往生产镜像里加测试代码（已知问题，阶段四处理，本阶段不要加重）。
- 新迁移沿用现有的时间戳命名方式。
- 新增配置项都写进 `.env.example`，带注释和默认值。不提交 `.env`。
- 每一步结束时 pytest 全部通过。每个检查点跑一次 `phase2_smoke.py`，9 个场景仍需全部 PASS。
- 每一步写 AGENT_LOG，审查点用三类标注：【人工审查发现】【agent 自查修复】【agent 做错】。
- 需要在浏览器里验证的，不要用 Chrome 插件，写清楚让 Jo 在浏览器里看什么。

---

## 二、关键设计决定

1. **提醒的时间存法**：数据库里所有时间存 UTC（timestamptz），另存时区名（如 `Asia/Shanghai`）。计算"明天 9 点""每个工作日"时，先换算到用户时区算日期，再换回 UTC。工作日按周一到周五算，不含法定节假日和调休。
2. **单独存"下一次触发时间"**：scheduler 只查这一列是否到期，不用每秒把所有提醒的规则重算一遍，这一列加索引。
3. **scheduler 是单独的服务**，和 gateway、worker 用同一个镜像。取到期提醒用 `FOR UPDATE SKIP LOCKED`，开多个 scheduler 也不会重复取同一条。
4. **scheduler 先推送再提交**：推送成功后才更新下一次触发时间并提交。代价是推送后、提交前如果崩溃，重启后会再推一次。这个概率很小，推两次提醒不涉及钱，接受，记为已知问题。反过来"先提交再推送"，推送失败时提醒就永久丢了，不能接受。
5. **错过的提醒只补推一次**：scheduler 停了一段时间再启动，过期的提醒立刻推一次；重复提醒错过了好几次的，也只推一次，然后直接跳到下一个将来的时间，不连续轰炸用户。
6. **提醒的时间由 LLM 理解、代码检查**：LLM 把"明天 9 点"转成具体的本地时间；代码检查这个时间必须在将来，时区必须合法，提前量在 0 到 1440 分钟之间。system prompt 里由代码写入"当前时间和星期"，这不是用户输入，可以放进 system prompt。
7. **上下文：最近 10 条原样保留，更早的压成摘要**。摘要在回复发完之后才生成，不拖慢首 token。摘要里有用户说过的话，所以只能放在 user 消息里的 `<历史摘要>` 块中，不能放进 system prompt。摘要存库前先脱敏。
8. **限流放在 gateway，放在去重之前**：被限流的消息不写去重键，用户稍后用同一个 message_id 重发仍然能被接收。
9. **Redis 挂了的策略**：去重跳过 Redis 这一层，靠数据库唯一约束兜底；限流直接放行（限流组件坏了，不能把所有用户都挡在外面）；token 预算检查也放行；推送不了的回复照样存库。日志只在"Redis 可用 → 不可用"和"不可用 → 恢复"时各打一条，避免刷屏。
10. **熔断状态放在每个 worker 进程自己的内存里**，不放 Redis。原因：Redis 本身也可能挂；实现简单。代价：开 3 个 worker 时，每个 worker 各自失败几次才会各自熔断。
11. **重试只有我们自己一层**：openai SDK 的 `max_retries` 设为 0，否则 SDK 自带的重试和我们的重试叠在一起，一次请求会变成好几次。
12. **死信前重试 3 次，但分情况**：消息本身坏了（解析不了、字段不合法），重试也没用，直接进 `inbound.dead`；处理中出现意外异常（比如数据库连不上），把消息重新投回原队列，消息头里的重试次数加 1，到第 3 次还失败才进 `inbound.dead`。重试之间没有等待间隔，记为已知问题。
13. **token 预算**：每个机构每天一个 token 上限，用 Redis 计数。调用 LLM 之前检查，超了就不调 LLM，走规则或模板回复。只在调用前检查，所以单次调用可能超出一点，接受。
14. **指标标签里只放 tenant，不放 user_id**：用户数量多，每个用户一组指标会让 Prometheus 的数据量失控。QPS 和错误率不单独做指标，由计数器用 `rate()` 算出来。

---

## 三、步骤

### 第 0 步：开工检查（已完成）

---

### 第 1 步：提醒的数据部分

**做什么**

- 新迁移：
  - `reminders` 表：id、tenant_id、user_id、conversation_id、title、event_at（事件时间，UTC）、timezone、repeat（none / daily / weekly / workdays）、advance_minutes（默认 30）、next_trigger_at（UTC，加索引）、status（active / cancelled / done）、created_at、updated_at。
  - `tenants` 表加 `timezone` 字段，默认 `Asia/Shanghai`。
- 新模块（纯函数，不连数据库、不调网络）：
  - 根据 event_at、timezone、repeat、advance_minutes 算出下一次触发时间。
  - 触发一次之后，算出下一次的 event_at 和 next_trigger_at。
  - 规则：创建时如果"事件时间减提前量"已经过了、但事件本身还没到，next_trigger_at 设为现在，立刻提醒；事件时间已过的，拒绝创建。
  - 错过多次的重复提醒，直接跳到下一个将来的时间（设计决定 5）。
- 参数模型（Pydantic，`extra="forbid"`）：创建、修改、取消、查看四个工具的参数。

**为什么**：对应 FR-5 "支持时区、重复规则、提前提醒"和 6.1 "日程规则单元测试"。

**验证**

- 单元测试至少覆盖：每天、每周、工作日（周五触发后下一次是周一）、提前 30 分钟、跨天（今晚 23:50 设"每天 00:10"）、事件已过被拒绝、离事件不到 30 分钟时立刻提醒、错过多次只补一次、非 Asia/Shanghai 时区。
- 迁移执行成功，用 `scripts/sql.py` 查到 reminders 表结构和 tenants 的 timezone 列。

---

### 第 2 步：提醒的推送和对话部分

**做什么**

- **scheduler 服务**：
  - docker-compose 新增 scheduler，健康检查端口 8002，依赖 postgres 和 redis 的 service_healthy。
  - 每秒一次：在一个事务里用 `FOR UPDATE SKIP LOCKED` 取最多 100 条到期的 active 提醒；对每一条：往 conversation_id 对应的会话写一条助手消息，发布到 Redis 频道 `im:out:{tenant_id}:{user_id}`（消息类型为 reminder），然后更新 next_trigger_at，或者把不重复的提醒标为 done；最后提交（设计决定 4）。
  - Redis 发布失败：这一条不更新，下一秒再试，日志按设计决定 9 的方式打。Redis 恢复后自动补推，提醒不丢。
  - 提醒文案用模板，不调 LLM，例如："提醒：今天 19:00 家长会，还有 30 分钟开始。"
- **worker 提醒节点**：替换现在的占位回复。
  - 四个工具：创建、修改、取消、查看提醒，参数走阶段二的 `parse_tool_call` 三关校验。
  - 代码检查按设计决定 6。
  - 修改和取消时，把该用户生效中的提醒（id、标题、时间）放进 user 消息的 `<提醒列表>` 块里，让 LLM 选 id；代码再查一遍这个 id 属于当前 tenant 和 user，不属于就按越权处理。
  - 用户没有生效的提醒：回"你目前没有生效的提醒"。有多个又说不清是哪个：列出来，请用户说时间或名称。
  - 取消和修改提醒不算高风险，不需要二次确认。
  - 回复用模板，例如："已设置提醒：明天（9 月 26 日）09:00 家长会，提前 30 分钟在 IM 通知你。"不要承诺短信等系统没有的功能。
  - LLM 失败时，回"提醒这次没设置成功，麻烦再发一次"，不猜时间。
- **mock-llm**：
  - 加提醒相关的规则，相对时间按请求里 system prompt 写的"当前时间"计算，保证测试结果固定。
  - 注意关键词碰撞（已出现三次），新规则要配 mock-llm 镜像内测试。
- **快进脚本** `scripts/reminder_ff.py`：把指定用户最新一条生效提醒的 next_trigger_at 改成"现在 + 3 秒"。只能在 tools 容器里跑；`APP_ENV=production` 时拒绝执行。
- **新建 `scripts/phase3_smoke.py`**，本步先放 E2E 场景 5：u_a_1001 发"明天早上 9 点提醒我交作业" → 查到 reminders 里有这一条 → 快进 → 在 WebSocket 上收到提醒 → 打印"实际推送时间 − next_trigger_at"，要求小于 5 秒。

**为什么**：对应 FR-5 全部要求、E2E 场景 5、负面清单"服务重启后提醒丢失"。

**验证**

- `phase3_smoke.py` 场景 5 PASS，输出里能看到延迟秒数。
- 重启不丢：创建提醒 → `docker compose stop scheduler` → 快进 → 等 10 秒，确认没有推送 → `docker compose start scheduler` → 启动后立刻收到提醒。
- 多个 scheduler 不重复：`docker compose up -d --scale scheduler=2`，快进一条提醒，只收到一次。验证完恢复成 1 个。
- 修改、取消：各发一句，用 `scripts/sql.py` 查到 reminders 表对应变化；取消后快进，不再推送。
- 越权：尝试修改另一个用户的提醒 id，被拒绝。

**▶ 检查点 A：停下汇报。**

---

### 第 3 步：上下文

**做什么**

- 新迁移：`conversation_summaries` 表：conversation_id、tenant_id、summary（脱敏后）、covered_until（摘要覆盖到哪条消息）、updated_at。
- 调用 LLM 时，上下文由两部分组成：最近 10 条消息原样带上；更早的内容用摘要，放在 user 消息的 `<历史摘要>` 块里（设计决定 7）。
- 生成时机：回复发完、reply_end 之后，如果"最近 10 条之前、尚未被摘要覆盖"的消息超过 10 条，就把旧摘要和这些消息一起交给 LLM 生成新摘要。失败就保留旧摘要、打日志，不重试，下次再生成。
- mock-llm 识别摘要请求时，按 system prompt 里的固定标记判断，不按用户说的内容判断，避免关键词碰撞。
- reply_end 的 meta 里加上下文信息：本次带了几条原文、有没有带摘要。

**为什么**：对应 FR-1 "短期记忆与历史摘要"。也是 Jo 一面没答好的"上下文过长怎么处理""记忆库设计"。

**验证**

- 单元测试：少于阈值不生成；超过阈值生成；摘要放在 user 消息里而不是 system；摘要存库前已脱敏（摘要输入里带一个手机号，存下来的是打码后的）。
- `phase3_smoke.py` 加一个场景：同一会话连续发 25 条消息，查到 `conversation_summaries` 有记录，最后一条回复的 meta 里显示带了摘要。

**▶ 检查点 B：停下汇报。**

---

### 第 4 步：gateway 这边的保护（限流 + Redis 降级）

**做什么**

- **限流**：Redis 计数，用 Lua 脚本或事务保证"加一"和"设过期"一起完成。
  - 按用户：`RATE_LIMIT_USER_PER_10S`，默认 20。
  - 按机构：`RATE_LIMIT_TENANT_PER_SEC`，默认 2000。默认值要足够大，阶段四压测时不能被限流挡住。
  - 顺序：鉴权 → 格式校验 → 限流 → 去重 → 投递队列（设计决定 8）。
  - 超限时 ACK 状态为 `rate_limited`，并回一句"发得有点快，稍等几秒再发。"
- **gateway 的 Redis 降级**（设计决定 9）：
  - 去重：Redis 报错时跳过，继续投递。
  - 限流：Redis 报错时放行。
  - 订阅回复：断开后自动重连，重连间隔逐步拉长、有上限，进程不能退出。
  - `/health` 在 Redis 不可用时仍返回 200，内容标明 `redis: down`、`status: degraded`。
- **worker 和 scheduler 的 Redis 降级**：发布回复失败时不崩溃，回复照样存库、用户消息照样改为 replied、照样 ACK。

**为什么**：对应负面清单"无限流"、NFR-2 "Redis 故障时能降级"、6.5 故障注入"Redis 重启"。

**验证**

- 单元测试：超过用户上限被拒；被限流的消息不写去重键；Redis 报错时限流放行、去重跳过。
- 限流：用一个脚本在 10 秒内连发 30 条，前 20 条 accepted，后 10 条 rate_limited。
- Redis 挂了：`docker compose stop redis` → 发一条消息，照样收到 ACK accepted → 用 `scripts/sql.py` 查到回复已存库 → gateway 和 worker 日志各有一条"Redis 不可用，已降级"，没有刷屏 → `docker compose start redis` → 再发一条，正常收到回复，日志有一条"Redis 已恢复"。

---

### 第 5 步：worker 这边的保护（熔断 + 有上限的重试 + 死信）

**做什么**

- **熔断器**（设计决定 10），LLM 和 mock-finance 各一个：
  - 连续失败 `CB_FAILURE_THRESHOLD` 次（默认 5）就熔断，熔断 `CB_OPEN_SECONDS` 秒（默认 30）。之后放 1 个试探请求：成功就恢复，失败就继续熔断。
  - 算失败的：超时、连接失败、5xx。不算失败的：403、404 这类业务结果。
  - 熔断期间不发请求，直接走阶段二已有的降级路径：LLM 走关键词规则或"系统这会儿有点忙"；财务走故障话术、写 followup_tasks、写审计。
  - 状态变化时打日志，reply_end 的 meta 里标出是否因熔断降级。
- **重试**（设计决定 11）：
  - openai 客户端 `max_retries=0`。
  - LLM：超时或 5xx 最多重试 1 次。
  - 财务：保持重试 1 次，加上带随机抖动的退避间隔。
  - 平台指令：保持最多 2 次，加退避，继续使用幂等键。
  - 所有重试次数和退避时间放进配置。
- **死信**（设计决定 12）：
  - 消息本身坏了，直接进 `inbound.dead`。
  - 意外异常：读消息头 `x-retry-count`（没有就是 0）；小于 3 就把消息重新投回原队列、次数加 1、等 publisher confirm 之后再 ACK 原消息；等于 3 就进 `inbound.dead`。
  - 日志带 trace_id 和重试次数。
- **死信重投脚本** `scripts/dlq_replay.py`：把 `inbound.dead` 里的消息重新投回原队列，重试次数清零。用于故障恢复后补处理，也是阶段五排障演练要用的。

**为什么**：对应负面清单"无熔断""无限重试导致雪崩"、NFR-1 "熔断、降级、重试、死信队列"、6.5 故障注入。

**验证**

- 单元测试：熔断器状态变化（连续失败到阈值熔断、到时间放试探、试探成功恢复、试探失败继续熔断）；403 不计入失败；重试次数不超过上限；`x-retry-count` 到 3 进死信。
- 熔断：用 `scripts/mockctl.py` 让 mock-llm 一直返回 500 → 连发 8 条闲聊 → worker 日志出现熔断打开 → `docker compose logs mock-llm --tail 30` 显示熔断后不再收到请求 → 用户收到降级话术 → 恢复 mock-llm，等 30 秒后再发，日志出现熔断恢复。
- 财务熔断：同样方式让 mock-finance 返回 500，用户收到故障话术，followup_tasks 和审计都有记录。
- 死信：`docker compose stop postgres` → 发一条消息 → worker 日志显示重试 3 次后进死信 → 在 RabbitMQ 管理页面 http://localhost:15672 看到 `inbound.dead` 多了 1 条 → `docker compose start postgres` → 跑 `dlq_replay.py` → 用户收到回复。

**▶ 检查点 C：停下汇报。**

---

### 第 6 步：成本和指标

**做什么**

- **token 记录**：
  - 新迁移：`llm_usage` 表：id、tenant_id、conversation_id、trace_id、purpose（intent / chat / knowledge / summary / handoff）、model、prompt_tokens、completion_tokens、estimated（是否估算）、created_at；在 (tenant_id, created_at) 上加索引。
  - 每次调用 LLM 都记录。流式调用要拿到 usage；mock-llm 如果不返回 usage，就补上。拿不到时按字数估算，estimated 标为 true。
  - 写入失败不影响回复，只打日志。
- **token 预算**（设计决定 13）：
  - `tenants` 表加 `daily_token_budget`，可为空，为空时用 `.env` 的 `DEFAULT_DAILY_TOKEN_BUDGET`。
  - Redis 键 `llm:budget:{tenant_id}:{日期}`，日期按机构时区算，过期时间 2 天。每次调用后累加。
  - 调用前检查，超了就不调 LLM：意图识别走关键词规则；闲聊走模板；知识问答直接给出命中条款原文并带出处（复用阶段二已有路径）；摘要跳过。
  - meta 里标 `budget_exceeded`。Redis 不可用时放行。
- **Prometheus 指标**（设计决定 14）：
  - gateway：消息计数（按 tenant 和结果：accepted / duplicate / rate_limited / error）、ACK 耗时直方图、当前连接数。
  - worker：处理计数（按意图和结果）、首 token 耗时直方图、完整回复耗时直方图、LLM 请求计数（按结果）、LLM token 计数（按 tenant，分输入和输出）、工具调用计数（按工具和结果）、熔断状态、重试计数、死信计数、队列积压（每 5 秒查一次 inbound 和 inbound.dead 的消息数）。
  - scheduler：提醒推送计数、推送延迟直方图（实际推送时间 − next_trigger_at）。
  - 各服务在健康检查端口上暴露 `/metrics`（gateway 8000、worker 8001、scheduler 8002）。
- **Prometheus 服务**：docker-compose 加 prometheus，端口 9090，抓取上面三个服务。worker 要支持 `--scale` 之后多个实例都能抓到（用 Docker 内部 DNS 按服务名发现）。不做 Grafana。

**为什么**：对应负面清单"不记录 token 成本"、NFR-4 Prometheus 指标、NFR-5 按租户统计 token 和模型降级、加分项"成本控制与 token 预算"。Prometheus 服务是阶段四压测报告要用的（队列积压随时间的变化）。

**验证**

- 聊几句之后，用 `scripts/sql.py` 按机构汇总今天的 token，t_a 和 t_b 分开。
- 预算：把 t_b 的 daily_token_budget 设成 100 → 用 u_b_1001 聊两句 → 第二句起 meta 显示 budget_exceeded，回复是模板或条款原文，不报错 → 改回来。
- 浏览器打开 http://localhost:8001/metrics ，能找到 token 计数、工具调用计数、队列积压。
- 浏览器打开 http://localhost:9090 ，查询 `rate()` 能看到 gateway 的消息速率；`docker compose up -d --scale worker=3` 后，Targets 页面能看到 3 个 worker。验证完恢复成 1 个。

---

### 第 7 步：演示控制台和收尾

**做什么**

- **把 mock-im（8080）改成演示控制台**，一个 HTML 页面，不引入前端框架，样式从简：
  - 顶部：身份切换，预置 t_a 的 u_a_1001 学生、u_a_1002 家长、u_a_1003 坐席、u_a_1004 学生和 t_b 的 u_b_1001 学生。切换后断开旧连接、换 token 重连。token 由已有的开发用签发方式生成，密钥来自环境变量，不能写死在页面或代码里。
  - 左边：聊天窗口。支持流式显示；提醒推送用不同样式显示；显示连接状态。
  - 左边下方：快捷按钮，一键发送 10 个 E2E 场景的原句，再加一个"重复发送上一条"按钮（同一个 message_id 再发一次，用来演示幂等）。
  - 右边：透视面板，数据来自每条回复的 reply_end meta：意图、调用的工具和结果、引用的知识来源、ACK 耗时、首 token 耗时、完整耗时、trace_id、降级标记（熔断、预算、限流）、上下文信息（带了几条原文、有没有摘要）。
  - meta 里不能有未脱敏的敏感信息。
- **收尾**：
  - `phase3_smoke.py` 补齐场景：提醒修改和取消、限流、熔断降级、预算降级。每个场景结束后把 mock 和配置恢复原样。
  - README 更新端口（scheduler 8002、prometheus 9090）和新命令。
  - `.env.example` 检查所有新配置都在。
  - AGENT_LOG 写完本阶段。

**为什么**：演示控制台对应演示视频和面试现场讲解，把每一步决策展示出来；两家机构切换演示租户隔离和越权。

**验证**

- 浏览器打开 http://localhost:8080 ：
  - 用 u_a_1001 问"寒假班请假会退课时费吗？"，右边显示意图、知识来源、各项耗时、trace_id。
  - 切到 u_b_1001 问同一句，引用的来源不同。
  - 用 u_a_1001 点"查 u_a_1004 的订单"，被拒绝。
  - 点"重复发送上一条"，显示 duplicate，没有第二条回复。
  - 创建提醒后跑快进脚本，页面几秒内弹出提醒。
- `phase2_smoke.py` 9 个场景全部 PASS，`phase3_smoke.py` 全部 PASS，pytest 全部通过。
- `git status` 里没有 `.env`。

**▶ 检查点 D：停下汇报。**

---

## 四、Jo 要能讲清楚的问题

**提醒**
1. 数据库里存了 UTC 时间，为什么还要存时区名？
2. 为什么要单独存一列"下一次触发时间"？
3. `SKIP LOCKED` 解决什么问题？
4. scheduler 是先推送再提交，还是先提交再推送？为什么？代价是什么？
5. scheduler 停了一小时再启动，会发生什么？
6. "明天 9 点"是谁理解的，谁检查的？为什么这样分工？

**上下文**
7. 对话太长了怎么处理？
8. 摘要为什么不能放进 system prompt？
9. 摘要为什么在回复发完之后才生成？

**保护**
10. 限流为什么放在 gateway，而且放在去重前面？
11. Redis 挂了，去重、限流、推送分别会怎样？
12. 熔断解决什么问题？和重试是什么关系？
13. 为什么要关掉 openai SDK 自带的重试？
14. 为什么坏消息直接进死信，而数据库连不上要先重试 3 次？

**成本和指标**
15. 一个机构的 token 用超了，系统怎么表现？
16. 指标里为什么不按 user_id 分？
17. 题目要 QPS 和错误率，为什么没有单独做这两个指标？

**控制台**
18. 透视面板上每一项是什么意思，数据从哪来？

---

## 五、本阶段预设的已知问题

以下是设计时就接受的代价，阶段五写进"已知问题与后续规划"：

- 工作日只按周一到周五算，不含法定节假日和调休。
- scheduler 推送后、提交前崩溃，重启后同一条提醒可能再推一次。
- 死信前的 3 次重试之间没有等待间隔。
- Redis 故障期间回复无法实时推送；恢复后客户端不会自动补上这段时间的回复，要重新打开会话才能看到。
- 熔断状态每个 worker 各自计算，不共享。
- token 预算在调用前检查，单次调用可能超出一点。
