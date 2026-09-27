# 故障注入手册

PHASE4.md 4.5。这份文档只提供每种故障的**注入命令**和**恢复命令**（已经在本机跑过一遍确认
两条命令都真的生效），下面五项空着——现象、排查过程、结论由 Jo 亲手做故障演练时手写，
CC 不代填。

命令全部写成能在 **Windows cmd**（不是 Git Bash、不是 PowerShell）里直接复制粘贴运行的形式。
前提：`make up` 已经跑起来，`docker compose ps` 里所有服务都是 healthy。

## 最小告警（关键设计决定 5）

三个触发点打一条结构化日志：`level=error`、`event=alert`、带 `alert_type`（还有 `service`/
`reason` 等每种告警自己的字段），同时 `/metrics` 里 `alerts_total{alert_type=...}` 加一。
代码在 `app/common/alerts.py`，被这三处调用：
- `app/common/circuit_breaker.py`：熔断打开时（`alert_type=circuit_breaker_open`）
- `app/worker/consumer.py`：消息进死信时（`alert_type=dead_letter`）
- `app/common/redis.py`：Redis 从可用变不可用时（`alert_type=redis_unavailable`）

不接钉钉/邮件/短信这类真实告警通道，已知问题，见 AGENT_LOG.md。

查告警的方法：`docker compose logs gateway worker scheduler | findstr alert_type`，
或者直接看 `/metrics` 里的 `alerts_total`。

## 目录

**入口**
1. gateway 停了
2. token 错误或过期
3. 用户刷消息触发限流
4. 重复 message_id
5. RabbitMQ 停了

**worker**
6. worker 停了、队列积压【必测】
7. 格式坏的消息进死信【必测】
8. 数据库停了

**下游**
9. mock-llm 延迟 5 秒【必测】
10. mock-llm 返回 500 触发熔断【必测】
11. mock-llm 返回幻觉内容【必测】
12. mock-llm 返回非法 JSON
13. mock-finance 超时【必测】
14. mock-finance 返回 500【必测】
15. mock-platform 超时
16. 知识库没命中
17. 机构 token 预算用完

**推送**
18. Redis 重启【必测】
19. scheduler 停一段时间再启动

---

## 1. gateway 停了

**注入命令**
```
docker compose stop gateway
```

**恢复命令**
```
docker compose start gateway
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 2. token 错误或过期

一条命令里现场签发一个坏 token（过期或者签名对不上）并直接拿它连 gateway，打印 gateway 的
拒绝结果（`scripts/bad_token_probe.py`，新增，见 AGENT_LOG）。

**注入命令**
```
docker compose run --rm tools python scripts/bad_token_probe.py --tenant t_a --user u_a_1001 --kind expired
docker compose run --rm tools python scripts/bad_token_probe.py --tenant t_a --user u_a_1001 --kind invalid
```

**恢复命令**：没有需要恢复的系统状态（没有写任何数据），用一个正常 token 连接能成功即可确认
gateway 本身没问题：
```
docker compose run --rm tools python scripts/gen_token.py --tenant t_a --user u_a_1001
```
（这条只会打印一个 token，不会自动连接；正常连接验证用 PHASE2 的 `scripts/chat.py`）

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 3. 用户刷消息触发限流

**注入命令**
```
docker compose run --rm tools python scripts/rate_limit_burst.py --tenant t_a --user u_a_1001
```

**恢复命令**（立刻清掉限流计数，不用等 10 秒窗口自然过期）
```
docker compose exec redis redis-cli DEL ratelimit:user:t_a:u_a_1001 ratelimit:tenant:t_a
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 4. 重复 message_id

用固定的 message_id 发两次，第二次应该收到 `duplicate`。

**注入命令**
```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault4_demo --message-id 11111111-1111-1111-1111-111111111111 "你好，在吗"
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault4_demo --message-id 11111111-1111-1111-1111-111111111111 "你好，在吗"
```

**恢复命令**
```
docker compose exec redis redis-cli DEL dedup:t_a:11111111-1111-1111-1111-111111111111
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 5. RabbitMQ 停了

**注入命令**
```
docker compose stop rabbitmq
```

**恢复命令**
```
docker compose start rabbitmq
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 6. worker 停了、队列积压【必测】
**docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready

看：会列出几个队列名和数字。数字是"排队等着被取的消息数"，正常时都是 0。把输出贴给我，哪个是主队列、哪个是死信队列。

**注入命令**
```
docker compose stop worker
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault6_demo "消息1"
```
（上面这条会一直等回复，30 秒后自己超时退出打印"超过 30 秒没收到 reply_end"，这是正常现象——
worker 没在跑，没人会回复它；这条命令的作用只是把这条消息真的送进队列。想多攒几条积压就多开
几个 cmd 窗口各跑一次，或者等它超时退出后换个 `--conv` 再跑一次）

查证据

先确认 worker 确实停了。为什么查：这是固定排查顺序的第一步，出问题先看哪个容器不在跑。

docker compose ps -a worker

查积压：
```
docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready
```

**恢复命令**
```
docker compose start worker
```

**我 的预测**：（本次跳过预测，直接观察）

**现象**：worker 停掉后，在控制台用 u_a_1001 连发 4 条消息，都能发出去，但没有任何回复。启动 worker 后，4 条消息陆续收到回复，之后再发新消息也立即正常回复。

**查了哪里**：演示控制台；docker compose ps -a worker；RabbitMQ 队列（rabbitmqctl list_queues）。

**证据**：注入前 inbound.messages 为 0、inbound.dead 为 0。停掉 worker 后 docker compose ps -a worker 显示 Exited；连发 4 条后 inbound.messages 为 4，inbound.dead 仍为 0。启动 worker 后 4 条全部收到回复，inbound.messages 回到 0，inbound.dead 仍为 0。

**原因与结论**：符合设计。gateway 和 worker 是分开的两个服务，中间用 RabbitMQ 队列连接。worker 停止时 gateway 不受影响，照常接收消息并放进 inbound.messages 队列，消息在队列里积压等待；worker 恢复后从队列逐条取出处理，4 条消息全部处理，没有丢失，也没有进入死信队列。这对应题目"消息至少一次投递"和"突发流量不丢消息，恢复后能处理"的要求。用户在 worker 停止期间收不到回复，只是回复延迟，不丢消息。


---

## 7. 格式坏的消息进死信【必测】

gateway 自己的校验不会放过格式错的消息，这里绕过 gateway 直接往队列发一条 body 不是合法
JSON 的消息（`scripts/publish_malformed_message.py`，新增，见 AGENT_LOG）。

看正常状态

docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready

**注入命令**
```
docker compose run --rm tools python scripts/publish_malformed_message.py
```

确认进了死信：
```
docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready
```
ocker compose logs worker --tail 200 | findstr alert

看：有一行带 "event": "alert"，写着为什么进死信。

然后输入消息确认worker没被卡住，马上正常回复。这证明一条坏消息没有拖垮后面的正常消息。

**恢复命令**（清空死信队列，回到干净状态；不用 `dlq_replay.py`——这条坏消息重投回去只会立刻
再进一次死信，见脚本注释）
```
docker compose run --rm tools python scripts/purge_dead_queue.py
```

**Jo 的预测**：（本次跳过预测，直接观察）

**现象**：用脚本绕过 gateway，直接往 inbound.messages 发一条内容不是合法 JSON 的消息。几秒内这条消息进入死信队列 inbound.dead，worker 没有崩溃，之后发正常消息立即正常回复。

**查了哪里**：RabbitMQ 队列（rabbitmqctl list_queues）；worker 告警日志。

**证据**：注入前 inbound.messages、inbound.dead 都为 0。注入后 inbound.dead 为 1，inbound.messages 为 0。worker 日志 14:08:12（UTC）有一条告警：event 为 alert，alert_type 为 dead_letter，reason 为 malformed_body，level 为 error。该条 trace_id 为 44626d40-7e53-4d6a-a80c-939347ee1316，是脚本生成的，带横线，与 gateway 生成的 32 位 trace_id 格式不同，因此在 gateway 日志中查不到。执行 purge_dead_queue.py 后两个队列都回到 0。

**原因与结论**：符合设计。消息格式错误属于重试也无法恢复的错误，worker 不重试，直接转入死信队列并输出告警：不重试是为了避免同一条坏消息无限循环、占用 worker；不丢弃是为了保留原消息供事后排查。数据库连接失败这类可能自行恢复的错误才先重试 3 次再进死信。恢复时清空死信队列而不用 dlq_replay.py 重投，因为这条消息本身是坏的，重投后只会再次进入死信。

---

---

## 8. 数据库停了

**注入命令**
```
docker compose stop postgres
```

确认降级（`/ready` 返回 503，`postgres` 字段是 false，但进程本身没崩）：
```
curl -s http://localhost:8000/ready
```

**恢复命令**
```
docker compose start postgres
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 9. mock-llm 延迟 5 秒【必测】

**注入命令**
```
docker compose run --rm tools python scripts/mockctl.py llm latency_ms=5000
```

```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault9_demo "帮我查一下寒假班时间"
```

**恢复命令**
```
docker compose run --rm tools python scripts/mockctl.py llm reset
```

**我 的预测**：worker 等不到 LLM 回复会超时放弃，意图判断降级为关键词规则，回复降级为模板。

**现象**：没有降级。classify 5032ms，respond 8823.8ms，LLM 网络调用耗时 13834.7ms，判定来源 llm，意图 knowledge_qa，知识检索正常（knowledge 31ms），用户约 14 秒后收到正常回复。

**查了哪里**：演示控制台右侧面板的分段耗时和判定来源；app/common/config.py 的 llm_timeout_seconds 与 .env.example 的 LLM_TIMEOUT_SECONDS。

**证据**：LLM_TIMEOUT_SECONDS=15，llm_max_retries=1。load_context 5ms、knowledge 31ms，系统自身耗时正常，时间全部耗在等待 LLM。

**原因与结论**：超时阈值 15 秒大于注入的 5 秒延迟，worker 一直等待并最终拿到回复，因此未降级。LLM 无响应时，classify 需等 15 秒超时再重试 1 次，最坏约 30 秒才降级，对客服场景过长。修复方案：非流式调用超时 3 秒；流式调用首块和块间超时各 4 秒；超时不重试，500 等立即返回的错误仍重试 1 次；阈值放入配置。修复后重测结果待补。

**修复后重测结果**：LLM 超时改为非流式 3 秒、流式首块和块间各 4 秒、超时不重试后，重新注入 5 秒延迟，同一问题问两次。classify 3079.3ms、3016.9ms，判定来源变为 rule_fallback，即等待 3 秒后放弃 LLM、降级为关键词规则判断意图；respond 约 4010ms，流式等待 4 秒未收到首块后放弃；LLM 总耗时约 7 秒，修复前为 13834.7ms。两次结果一致。回复内容与正常时相同，为《课程服务协议》第 4.2 条原文，没有编造内容。
---

## 10. mock-llm 返回 500 触发熔断【必测】

`cb_failure_threshold`（.env，默认 5）次连续失败后熔断打开。下面这条命令连续跑 5 次
（每次用不同内容，避免碰上 mock-llm 规则里的固定关键词），第 5 次应该能在 worker 日志里看到
一条 `event=alert alert_type=circuit_breaker_open` 的记录。

**注入命令**
```
docker compose run --rm tools python scripts/mockctl.py llm mode=error500
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault10_demo "触发熔断测试消息1"
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault10_demo "触发熔断测试消息2"
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault10_demo "触发熔断测试消息3"
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault10_demo "触发熔断测试消息4"
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault10_demo "触发熔断测试消息5"
```

查告警日志：
```
docker compose logs worker | findstr "circuit_breaker_open"
```

查熔断器状态（0=closed 1=half_open 2=open；worker 容器没装 curl，用 tools 容器走 docker
内部网络查，不用先查宿主机映射端口）：
```
docker compose run --rm tools python -c "import urllib.request; print(urllib.request.urlopen('http://worker:8001/metrics', timeout=5).read().decode())" | findstr worker_circuit_breaker_state
```

**恢复命令**（重置 mock-llm；熔断器本身要等 `cb_open_seconds`，默认 30 秒，进入半开后再有一次
成功调用才会真正回到 closed——用下面第二条命令触发一次半开试探）
```
docker compose run --rm tools python scripts/mockctl.py llm reset
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault10_recover "熔断恢复探测消息"
```
**我 的预测**：连续发消息，熔断打开后 worker 不再调用 LLM，直接降级，回复很快；30 秒后恢复调用 LLM。

**现象**：注入后，熔断打开前的消息：classify 334.3ms，LLM 网络调用耗时 328.3ms，判定来源 rule_fallback，关键词未命中，返回兜底话术（respond 2.5ms）。熔断打开后：classify 9.1ms，LLM 网络调用耗时 0ms，面板显示熔断降级 llm。恢复 mock-llm 并等待 30 秒以上后发送消息，被 LLM 正常判定为闲聊。

**查了哪里**：演示控制台右侧面板；worker 日志中的熔断告警；用触发熔断那条消息的 trace_id 查 worker 日志；worker 的 /metrics 熔断器状态。

**证据**：告警日志 08:24:54（UTC）一条 reason=failure_threshold_reached、failures=5，之后三条 reason=half_open_probe_failed，间隔分别为 61、42、31 秒，均不少于 30 秒。trace_id 78605fce0faa44c1a48b28a5a69c88ea 的日志：第 1 次请求返回 500 并重试，312ms 后第 2 次失败，熔断打开，降级为关键词规则。LLM 耗时约 328ms，包含两次请求和中间 300ms 的重试等待（error500 模式立即返回）。恢复后 worker_circuit_breaker_state{service="llm"} 为 0.0。日志中 07:17、07:24 两条告警早于本次注入，非本次产生。

**原因与结论**：符合设计。LLM 返回 500 时，每次调用重试 1 次后降级；连续失败 5 次熔断器打开，30 秒内不调用 LLM、直接降级；30 秒后的下一条消息作为半开探测，失败则重新打开，成功则关闭。熔断打开和重新打开都会输出 event=alert 告警日志。

---

## 11. mock-llm 返回幻觉内容【必测】

**注入命令**
```
docker compose run --rm tools python scripts/mockctl.py llm mode=hallucinate
```

```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault11_demo "寒假班放假安排是什么"
```
***查看日志**
docker compose logs worker | findstr trace_id

dropped_sentences，dropped_sentences，fallback_used": false：没走兜底，其余句子正常发给了用户[OutputGuard 核对]

（docker compose logs worker --tail 200 | findstr /v /c:"/health" 最近日志）
**恢复命令**
```
docker compose run --rm tools python scripts/mockctl.py llm reset
```
**Jo 的预测**：（本次跳过预测，直接观察）

**现象**：同一句"寒假班放假安排是什么"，注入前后回复一字不差，都是"依据《课程服务协议》第 4.2 条、《课程服务协议》第 5.2 条：我查到的规定是：《课程服务协议》第 4.2 条"加第 4.2 条原文。知识来源两次相同：《课程服务协议》第 4.2 条(0.5393)、第 5.2 条(0.4509)、第 4.1 条(0.395)。意图 knowledge_qa / llm，工具结果 search_knowledge:ok。注入前 classify 336.2ms、respond 4133.5ms；注入后 classify 318.1ms、respond 5084.8ms，差别只是生成长短不同。中途有一次问的是退费相关问题，检索结果不同，与幻觉无关，不作对比。

**查了哪里**：演示控制台面板；worker 日志；mock-llm 日志；OutputGuard 单元测试；交 CC 离线核对代码行为。

**证据**：用 trace_id 前 12 位 6fcb005d74eb 搜 worker 日志，一行都搜不到；知识问答路径只有 httpx 打的纯文本请求日志"POST http://mock-llm:8000/v1/chat/completions 200 OK"，不带 trace_id。mock-llm 日志只记录请求返回 200，不记录返回内容。OutputGuard 单元测试中 test_sentence_with_disallowed_citation_is_dropped（引用未检索条款的句子被删）和 test_all_sentences_dropped_leaves_lead_in_unconsumed（全部删光时正确处理）均通过。CC 离线核对：hallucinate 模式在正常回答前加一句写死的假话，引用《课程服务协议》第 9.9 条；OutputGuard 删掉这一句，被删条款为 9.9，其余 4 句通过；正常模式没有删任何句子。两次都不是"全部删光后输出条款原文"的兜底，因为兜底输出不带"《课程服务协议》第 4.2 条"标题行。

**原因与结论**：符合设计。LLM 幻觉时返回 200、速度正常、不报错，从耗时和熔断都看不出来，只能检查内容。OutputGuard 逐句检查回答中带书名号的条款引用，编号不在本次检索结果里的句子直接删除，因此用户收不到编造的第 9.9 条。真实大模型的幻觉不可控，无法按需复现，所以用 mock-llm 写死一句假话来稳定验证。
本次发现一个问题，已交 CC 修改：知识问答路径没有任何带 trace_id 的结构化日志，OutputGuard 删没删句子无法从日志确认。CC 已补"知识问答 OutputGuard 核对完成"日志，记录删除句数、被删条款编号、是否走兜底。当前结论来自 CC 离线核对，以 worker 日志中的被删条款编号为准。

**build 后重测结果**：重新注入 hallucinate，用 t_b 提问"退费政策是什么"，trace_id 582d70b05888441d95303628459ce195。worker 日志"知识检索完成"显示检索结果为《退费政策》第 2.2 条(0.4162)、第 2.3 条(0.4114)、《课程服务协议》第 5.1 条(0.3933)，不含第 9.9 条；"知识问答 OutputGuard 核对完成"显示 dropped_sentences 为 1，dropped_citations 为《课程服务协议》第 9.9 条，fallback_used 为 false。检索结果中没有第 9.9 条，回答中却出现了引用它的句子，证明注入生效、LLM 输出了编造的引用；该句被删除，证明 OutputGuard 拦截生效。正常模式下 messages 表中回复的 meta 记录 dropped_sentences 为 0，作为对比。

---

---

## 12. mock-llm 返回非法 JSON

**注入命令**
```
docker compose run --rm tools python scripts/mockctl.py llm mode=invalid_json
```

```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault12_demo "帮我把自动续费关了"
```

**恢复命令**
```
docker compose run --rm tools python scripts/mockctl.py llm reset
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 13. mock-finance 超时【必测】

**注入命令**
```
docker compose run --rm tools python scripts/mockctl.py finance mode=timeout
```

```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault13_demo "帮我查一下最近的发票"
```
***查看日志**
docker compose logs worker | findstr trace_id


4. 查待跟进任务
```
docker compose exec postgres psql -U postgres -d edu_cs_bot -x -c "SELECT * FROM followup_tasks ORDER BY created_at DESC LIMIT 2;"
```
看：status 为 open，query 记录了查询条件，created_at 和日志时间对得上

5. 查审计
```
docker compose exec postgres psql -U postgres -d edu_cs_bot -x -c "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 3;"

(docker compose exec postgres psql -U postgres -d edu_cs_bot -P pager=off -x -c "SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 3;")
```
看：故障期间的查询 result 为 upstream_error

6. 恢复
```
docker compose run --rm tools python scripts/mockctl.py finance reset
```
看：配置里 "mode": "normal"

7. 财务熔断已打开，等 30 秒以上，控制台再发"帮我查一下最近的发票"
看：回复有订单号、金额、发票状态，邮箱为 l***@example.com；审计新增一条 result 为 success

**我 的预测**：（本次跳过预测，直接观察）

**现象**：回复"财务系统暂时查不到你的信息，这次查询我已记录，稍后回复你"，没有任何金额、单号。面板：classify 327.1ms，finance 3371.9ms，respond 0.8ms，意图 finance_query / llm，工具结果 query_finance:upstream_error。

**查了哪里**：演示控制台面板；worker 日志；followup_tasks 表；audit_logs 表。

**证据**：finance 3371.9ms，对应 1.5 秒超时、重试 1 次再超时。worker 日志 09:17:31、09:17:41（UTC）各有一条"财务系统查询失败，记一条跟进任务"；09:17:41 财务连续失败 5 次，熔断打开并输出告警。followup_tasks 有两条 status 为 open 的记录，created_at 为 09:17:31.440、09:17:41.907，与日志时间对应。audit_logs 故障期间两条 result 为 upstream_error；恢复后一条 result 为 success（09:32:09）。

**原因与结论**：符合设计。财务超时 1.5 秒，重试 1 次后失败，回复使用代码里的固定话术，没有放数字的位置，不会编造金额；同时写入待跟进任务和审计。财务超时保留重试：阈值只有 1.5 秒，重试后总共约 3.4 秒，用户可以接受；LLM 改为超时不重试，是因为原阈值 15 秒，重试后最坏约 30 秒。
本次发现两个问题，已交 CC 修改：一、日志脱敏把 trace_id 中连续 12 位数字当成银行卡号改写，导致用完整 trace_id 搜不到日志，修复前可用 trace_id 前 12 位搜索；二、财务失败日志 error 字段为空，看不出是超时还是 500，也没有重试日志。

---

## 14. mock-finance 返回 500【必测】

**注入命令**
```
docker compose run --rm tools python scripts/mockctl.py finance mode=error500
```

```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault14_demo "帮我查一下最近的账单"
```

**恢复命令**
```
docker compose run --rm tools python scripts/mockctl.py finance reset
```

**我 的预测**：（本次跳过预测，直接观察）

**现象**：回复"财务系统暂时查不到你的信息，这次查询我已记录，稍后回复你"，没有任何金额、单号。面板：classify 345.3ms，finance 370ms，respond 0.9ms，意图 finance_query / llm，工具结果 query_finance:upstream_error。注入前正常查询时 finance 85.3ms，工具结果 ok；恢复后再查，结果与注入前一致。

**查了哪里**：演示控制台面板；worker 日志；followup_tasks 表；audit_logs 表。

**证据**：finance 从正常的 85.3ms 变为 370ms，多出约 285ms，对应收到 500 后重试 1 次再失败。worker 日志 12:28:51（UTC）有一条"财务系统查询失败，记一条跟进任务"，error 为"上游 500"，circuit_open 为 false。followup_tasks 新增一条 status 为 open 的记录，created_at 为 12:28:51.518，与日志时间对应。audit_logs 注入前一条 result 为 success（12:27:34），故障期间一条 result 为 upstream_error（12:28:51）。

**原因与结论**：符合设计。500 表示财务系统内部出错，属于可重试错误，worker 重试 1 次后仍失败，回复使用代码里的固定话术，没有放数字的位置，不会编造金额；同时写入待跟进任务和审计。与故障 13 相比，失败方式不同：超时要等满 1.5 秒阈值，finance 3371.9ms；500 立即返回，finance 只有 370ms。但降级回复、跟进任务、审计三项结果一致。本次只发了一条消息，未触发财务熔断。
本次日志 error 字段显示"上游 500"，说明故障 13 发现的 error 为空只出现在超时的情况；具体尝试次数日志中仍看不到，已包含在交给 CC 的财务失败日志修改中。

---

---

## 15. mock-platform 超时

**注入命令**
```
docker compose run --rm tools python scripts/mockctl.py platform mode=timeout
```

```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault15_demo "帮我把自动续费关了"
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault15_demo "确认关闭"
```
（如果"春季数学班"的自动续费之前已经被关过、第一条直接回复"已经是关闭状态"，先跑一次下面的
恢复命令把状态和 mode 一起重置，再重新执行上面两条）

**恢复命令**（同时重置 mode 和"春季数学班"的自动续费开关状态）
```
docker compose run --rm tools python scripts/mockctl.py platform reset
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 16. 知识库没命中

不需要注入任何故障状态，正常发一句知识库里没有的问题就行。

**注入命令**
```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault16_demo "火星殖民地的入学考试是什么时候"
```

**恢复命令**：无需恢复，没有改动任何系统状态。

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 17. 机构 token 预算用完

直接把 t_a 今天的用量 Redis key 写成一个超大值（`scripts/exhaust_token_budget.py`，新增，
见 AGENT_LOG），不用真的发几百万字的对话才能刷完预算。

**注入命令**
```
docker compose run --rm tools python scripts/exhaust_token_budget.py --tenant t_a set
```

```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault17_demo "帮我查一下寒假班时间"
```

**恢复命令**
```
docker compose run --rm tools python scripts/exhaust_token_budget.py --tenant t_a clear
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 18. Redis 重启【必测】
curl -s http://localhost:8000/health
```

看：{"status":"ok","redis":"up"}。


**注入命令**
```
docker compose stop redis
```

确认降级（`/health` 返回 `degraded`，但进程本身没崩、没被重启）：
```
curl -s http://localhost:8000/health
docker compose ps redis gateway worker
```
看：STATUS 一列，redis 是 Exited，gateway 和 worker 是 Up (healthy)，没有 Restarting。

再问一次。控制台点同一个快捷按钮。
证明系统报了警：

```
docker compose logs gateway --tail 200 | findstr alert
```

看：有 "alert_type": "redis_unavailable" 的行。

证明 worker 照样处理完了，只是推不出去：


**恢复命令**
```
docker compose start redis
```

**Jo 的预测**：（本次跳过预测，直接观察）

**现象**：停掉 Redis 后，/health 从 {"status":"ok","redis":"up"} 变为 {"status":"degraded","redis":"down"}；gateway 和 worker 进程没有崩溃或重启。在控制台发"寒假班请假会退课时费吗？"，消息能发出，控制台没有收到回复。恢复 Redis 后 /health 回到 ok，新消息正常回复。

**查了哪里**：/health 接口；docker compose ps；gateway 告警日志；messages 表。

**证据**：docker compose ps -a 显示 redis 为 Exited，gateway 为 Up 7 hours (healthy)，worker 为 Up 36 minutes (healthy)，没有 Restarting。gateway 日志 14:31:04、14:31:05（UTC）各有一条告警，alert_type 为 redis_unavailable，level 为 error。messages 表中 Redis 停止期间的用户消息"寒假班请假会退课时费吗？"（14:33:20，trace_id 52756963f10f473b9676750b1d82246e）status 为 replied，14:33:30 有对应的机器人回复入库。从发出到回复入库约 10 秒，恢复后同一问题约 4.5 秒，多出的时间原因未深查。

**原因与结论**：符合设计。Redis 在系统中负责三件事：gateway 限流、gateway 去重、worker 回复推送。Redis 不可用时，限流放行，去重跳过并由数据库唯一约束兜底，gateway 和 worker 都不崩溃，/health 返回 degraded 并输出告警。消息照常进入队列，worker 照常处理并把回复写入数据库，只有推送到浏览器这一段中断。这与故障 6 不同：故障 6 是 worker 停止，数据库中没有回复；故障 18 是回复已生成，只是推不出去。Redis 停止期间生成的回复，恢复后不会自动补推，这是已记录的已知问题。

---

---

## 19. scheduler 停一段时间再启动

**注入命令**
```
docker compose stop scheduler
```
（想验证"停的这段时间里到期的提醒，起来后会不会补推"，配合 `scripts/reminder_ff.py` 把某个
提醒快进到 3 秒后触发，再停 scheduler，等超过 3 秒之后再执行下面的恢复命令）

**恢复命令**
```
docker compose start scheduler
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：
