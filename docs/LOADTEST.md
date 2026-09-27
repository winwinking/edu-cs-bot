# 压测报告

PHASE4.md 4.6 / REQUIREMENTS.md 6.4。本文档只是骨架——脚本和采集方式已经准备好，**还没有
实际跑过**（阶段四 4.6 这一轮是 Jo 在同一套容器上做故障注入期间的准备工作，明确不能跑压测、
不能碰容器状态），下面所有数字位置先留空，等 Jo 通知故障注入结束、真正跑完压测之后再填。

如实写，不为了凑数字调参数（PHASE4.md 关键设计决定 8）：达不到题目指标的场景，写清楚当前
硬件、瓶颈在哪、证据是什么、怎么改进，不悄悄改小目标吞吐量或者调松阈值让报告好看。

## 准备工作（跑压测之前）

1. 确认已经 `make up`，所有服务 healthy。
2. 生成压测用户 + token（t_a 下 1600 个，覆盖题目要求的"至少 1500 个"）：
   ```
   make loadtest-users
   ```
   token 有效期 6 小时（`loadtest/gen_users.py` 里的 `_TOKEN_EXPIRE_MINUTES`），写进
   `loadtest/tokens.json`——这个文件在 `.gitignore` 里，不会被提交，压测机器换一台就要重新生成。
   用户行是幂等插入的，重复跑这一步不会报错也不会重复插入。
3. mock-llm 速度按关键设计决定第 7 条设置（意图识别约 300ms、流式生成首字约 300ms 之后每秒约
   50 字）——种子环境默认值就是这样（`.env.example` 的 `MOCK_LLM_LATENCY_MS=300`），除非之前
   被别的操作改过，跑之前用 `docker compose run --rm tools python scripts/mockctl.py llm show`
   确认一下。
4. 每个场景开始前，另开一个终端跑 docker stats 采集（见下面"采集方式"），场景结束后 Ctrl+C
   停掉，换下一个场景前换一个输出文件名。

## 怎么跑（也能单独跑某一个场景）

```
make loadtest-steady          # 场景 1：稳定
make loadtest-burst           # 场景 2：突发
make loadtest-finance         # 场景 3：财务查询
make loadtest-llm-timeout     # 场景 4：LLM 超时率 20%（内部会先后调用 mockctl 设置/重置）
make loadtest                 # 四个场景依次跑完
```

每个场景可以用环境变量覆盖目标吞吐量/时长/VU 数做小规模冒烟（数值定义在各自的 `.js` 文件里），
例如：
```
docker compose run --rm k6 run -e STEADY_DURATION=30s -e STEADY_RATE=20 loadtest/steady.js
```

## 采集方式

- **k6 自带的延迟和错误率**：`ack_latency_ms`/`first_chunk_latency_ms`/`full_reply_latency_ms`
  三个自定义 Trend（对应 ACK 耗时、首句耗时、完整回复耗时），`app_errors`/`app_duplicates`/
  `app_rate_limited`/`app_client_timeouts` 四个 Counter。跑完 k6 会在终端打印这些指标的
  avg/min/max/p90/p95，`--out csv=...`（Makefile 里已经接上）还会把每一次采样按时间戳落进
  CSV，事后可以自己算精确的 P50/P99（k6 终端汇总默认不打 P50/P99，只打 p90/p95，报告里的
  P50/P99 从 CSV 里用 pandas/Excel 之类的工具算）。
- **RabbitMQ 队列积压**：`loadtest/lib/rabbitmq.js` 里的 `queue_backlog_inbound`/
  `queue_backlog_dead` 两个 Gauge，每 5 秒采一次（每个场景文件里的 `queue_backlog_collector`
  这个独立 scenario），跟延迟指标一起进同一份 CSV，按时间戳对齐着看。突发场景（`burst.js`）
  的采集比主负载多跑几分钟（`BURST_DRAIN_WATCH`，默认 3 分钟），专门用来看"消化完的时间"。
- **docker stats**：k6 是 JS 沙箱，碰不到宿主机的 docker 命令，这部分单独用
  `loadtest/collect_docker_stats.ps1` 在宿主机 PowerShell 里跑，跟 k6 场景并行、手动对齐开始/
  结束时间：
  ```
  powershell -File loadtest/collect_docker_stats.ps1 -OutFile loadtest/output/steady_docker_stats.csv
  ```
- **突发场景"发出消息数 = 最终 replied 消息数"**：k6 自己的 `iterations` 指标就是发出的消息数；
  数据库里最终 replied 的数量用 `scripts/sql.py` 查（压测开始时间要记下来，SQL 按
  `created_at` 过滤这次压测期间的数据）：
  ```
  docker compose run --rm tools python scripts/sql.py "select count(*) from messages where tenant_id='t_a' and role='user' and status='replied' and created_at > '<压测开始时间，ISO格式>'"
  ```

## 已知的实现落差

- **场景 4「LLM 超时率 20%」用 `timeout_rate=0.2`**：`mocks/mock_llm/main.py` 新增了
  `timeout_rate` 参数（命中后挂起不返回，跟 mock-finance/mock-platform 的 `mode=timeout`
  是同一个思路），不再是最初准备阶段用 `error_rate` 近似的方案——`error_rate` 命中是立刻返回
  500，worker 立刻重试/降级，跟"客户端一直等到超时阈值、这段时间连接和协程被占用"是两种完全
  不同的压力，题目要的是后者，审查后已经改用真正的超时模式（见 `loadtest/llm_timeout.js`
  顶部注释、`mocks/mock_llm/main.py` 的 `_maybe_timeout()` 注释）。`tests/unit/
  test_mock_llm_timeout.py` 加了对应用例，本轮同样没有实际执行，等故障注入结束后随
  `make test` 一起跑。
- k6 的 `constant-arrival-rate` 执行器是"按目标吞吐量分配 VU"，不是"固定 500/1000 个连接
  从头开到尾"——`preAllocatedVUs`/`maxVUs` 是为了达到目标吞吐量估算出来的并发量级，如果实际
  运行时 k6 报 `dropped_iterations`（VU 池不够用），说明真实往返耗时比准备阶段的估算长，需要
  调大池子，这类调整要如实记进下面对应场景的"备注"里，不是数字不好看就悄悄調。
- 场景 4 的客户端等待超时（`LLM_TIMEOUT_SCENARIO_CLIENT_TIMEOUT_MS`）是 15 秒，比其它场景的
  默认 30 秒小：按新的超时配置重新估算过（`llm_nonstream_timeout_seconds=3` 且超时不重试、
  `llm_stream_timeout_seconds=4` 且超时不重试——定 4 秒不是 5 秒，是因为故障注入 9 本身就是拿
  mock-llm 延迟 5 秒复测这个超时行为，超时值也是 5 秒会跟注入延迟撞在一起、结果不确定，错开
  一秒才稳定），classify 命中超时会直接走固定话术模板、不会再触发 respond 阶段的 LLM 调用，
  两段不会叠加，各自独立的最坏情况取较大值，约 4~5 秒，15 秒是留了压测负载下排队/DB/Redis
  抖动的余量，详细推演见 `loadtest/llm_timeout.js` 顶部注释。

---

## 场景 1：稳定（500 连接，200 msg/s，5 分钟）

**本机硬件**：CPU 核数＿＿　内存＿＿　worker 副本数＿＿

| 指标 | 结果 |
| --- | --- |
| 实际 QPS |  |
| ACK 耗时 P50/P95/P99 |  |
| 首句耗时 P50/P95/P99 |  |
| 完整回复耗时 P50/P95/P99 |  |
| 错误率 |  |
| 队列积压最高值 |  |
| 队列消化完用时 |  |
| 各容器 CPU 峰值 |  |
| 各容器内存峰值 |  |

**跟题目指标（500 连接/200 msg/s/5 分钟）对比**：

**备注**（有没有调过 preAllocatedVUs、观察到的异常等）：

---

## 场景 2：突发（≥1000 VU，1000 msg/s，30 秒）

| 指标 | 结果 |
| --- | --- |
| 实际 QPS |  |
| ACK 耗时 P50/P95/P99 |  |
| 首句耗时 P50/P95/P99 |  |
| 完整回复耗时 P50/P95/P99 |  |
| 错误率 |  |
| 队列积压最高值 |  |
| 队列消化完用时 |  |
| 各容器 CPU 峰值 |  |
| 各容器内存峰值 |  |
| k6 发出的消息数（iterations） |  |
| 数据库里最终 replied 的消息数 |  |
| 两者是否相等（证明不丢消息） |  |

**跟题目指标（≥1000 VU/1000 msg/s/30 秒）对比**：

**备注**：

---

## 场景 3：财务查询（100 QPS）

| 指标 | 结果 |
| --- | --- |
| 实际 QPS |  |
| 完整回复耗时 P50/P95/P99 |  |
| `meta.timings.finance` P50/P95/P99 |  |
| 错误率 |  |
| 队列积压最高值 |  |
| 各容器 CPU 峰值 |  |
| 各容器内存峰值 |  |

**跟题目指标（100 QPS，P95 < 500ms）对比**：

**备注**：

---

## 场景 4：LLM 超时率 20%（跑场景 1 的负载）

| 指标 | 结果 |
| --- | --- |
| 系统是否崩溃（服务是否都在、`/health` 是否正常） |  |
| 降级回复占比 |  |
| 是否触发熔断（`worker_circuit_breaker_state{service="llm"}`） |  |
| ACK 耗时 P50/P95/P99 |  |
| 完整回复耗时 P50/P95/P99 |  |
| 错误率 |  |

**跟题目指标（LLM 超时率 20% 时系统降级且不崩溃）对比**：

**备注**：

---

## 耗时拆分：mock 耗时 vs 系统自身耗时

用 `reply_end.meta` 里的 `llm_ms` 和 `timings`（`load_context`/`classify`/各工具节点/
`respond`）把 mock 服务的耗时和系统自己处理的耗时拆开写，说明阶段一"完整回复 3004ms"这个
数字具体是怎么构成的（哪部分是 mock-llm 的固定延迟，哪部分是系统自己的处理时间）。

**结果**：

---

## 和题目指标逐条对比（汇总）

| 题目要求 | 实测结果 | 达标？ | 原因/改进方向 |
| --- | --- | --- | --- |
| 稳定：500 连接，200 msg/s，5 分钟 |  |  |  |
| 突发：≥1000 VU，1000 msg/s，30 秒，不丢消息 |  |  |  |
| 财务查询：100 QPS，P95 < 500ms |  |  |  |
| LLM 超时率 20%：降级且不崩溃 |  |  |  |

## 已知问题

- 场景 4 用的 `timeout_rate` 新参数和对应测试本轮都没有实际执行过，第一次正式跑之前需要先
  验证一遍这个假设成立，见上面"已知的实现落差"。
