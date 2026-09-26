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

**注入命令**
```
docker compose stop worker
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault6_demo "消息1"
```
（上面这条会一直等回复，30 秒后自己超时退出打印"超过 30 秒没收到 reply_end"，这是正常现象——
worker 没在跑，没人会回复它；这条命令的作用只是把这条消息真的送进队列。想多攒几条积压就多开
几个 cmd 窗口各跑一次，或者等它超时退出后换个 `--conv` 再跑一次）

查积压：
```
docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready
```

**恢复命令**
```
docker compose start worker
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 7. 格式坏的消息进死信【必测】

gateway 自己的校验不会放过格式错的消息，这里绕过 gateway 直接往队列发一条 body 不是合法
JSON 的消息（`scripts/publish_malformed_message.py`，新增，见 AGENT_LOG）。

**注入命令**
```
docker compose run --rm tools python scripts/publish_malformed_message.py
```

确认进了死信：
```
docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready
```

**恢复命令**（清空死信队列，回到干净状态；不用 `dlq_replay.py`——这条坏消息重投回去只会立刻
再进一次死信，见脚本注释）
```
docker compose run --rm tools python scripts/purge_dead_queue.py
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

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

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

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

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

---

## 11. mock-llm 返回幻觉内容【必测】

**注入命令**
```
docker compose run --rm tools python scripts/mockctl.py llm mode=hallucinate
```

```
docker compose run --rm tools python scripts/chat.py --tenant t_a --user u_a_1001 --conv fault11_demo "寒假班放假安排是什么"
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

**恢复命令**
```
docker compose run --rm tools python scripts/mockctl.py finance reset
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

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

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

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

**注入命令**
```
docker compose stop redis
```

确认降级（`/health` 返回 `degraded`，但进程本身没崩、没被重启）：
```
curl -s http://localhost:8000/health
docker compose ps redis gateway worker
```

**恢复命令**
```
docker compose start redis
```

**Jo 的预测**：

**现象**：

**查了哪里**：

**证据**：

**原因与结论**：

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
