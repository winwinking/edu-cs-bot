/**
 * 场景 2「突发」（PHASE4.md 4.6 / REQUIREMENTS.md 6.4）：至少 1000 个虚拟用户，每秒 1000 条，
 * 持续 30 秒；每个用户每 10 秒不超过 15 条，避开网关的用户限流（RATE_LIMIT_USER_PER_10S=20，
 * 见 app/common/config.py）。
 *
 * Jo 审查后改成长连接模型，连接数和每条连接的发送速率配套算出来：
 *   目标 1000 条/秒 ÷ 1500 条连接 = 每条连接 0.667 条/秒 = 每 10 秒 6.67 条
 * 6.67 条/10秒 比题目原文"每 10 秒不超过 15 条"这个上限还留了更大的余量（不到限流阈值 20 的
 * 三分之一），t_a 下 1600 个压测用户，用 1500 条连接够用（用了 1500/1600，剩 100 个没用到，
 * 不是用户数不够）。
 *
 * 突发场景要验证"发出的消息数和数据库里最终 replied 的消息数相等，证明不丢消息"（PHASE4.md），
 * 长连接模型下"发出数"改用 `messages_sent`（每次真正 socket.send() 才加一，不是 k6 的
 * `iterations`——iterations 现在是"连接数"，不是"消息数"），"ack 数"用 `messages_acked`，
 * 客户端观测到的"完整回复数"用 `messages_replied`，数据库里最终 replied 的数量另外用
 * scripts/sql.py 查，三个数字放在一起对比，写进 docs/LOADTEST.md：
 *   docker compose run --rm tools python scripts/sql.py "select count(*) from messages where tenant_id='t_a' and role='user' and status='replied' and created_at > '<压测开始时间，ISO格式>'"
 *
 * 用法：
 *   docker compose run --rm k6 run burst.js
 *   docker compose run --rm k6 run --out csv=/loadtest/output/burst.csv burst.js
 */
import { runPersistentConnection } from './lib/ws_client.js';
import { tokenForVU } from './lib/tokens.js';
import { pollQueueBacklog } from './lib/rabbitmq.js';
import { GATEWAY_WS_URL, conversationIdFor } from './lib/config.js';

const SCENARIO_DIGIT = '2';

const CONTENTS = [
  '请问退课政策是什么',
  '寒假班的收费标准是什么',
  '上课时间怎么安排',
  '请假需要提前多久申请',
  '转班需要什么手续',
];

export const DURATION_SECONDS = Number(__ENV.BURST_DURATION_SECONDS || 30);
export const RATE = Number(__ENV.BURST_RATE || 1000);
export const CONNECTIONS = Number(__ENV.BURST_CONNECTIONS || 1500);
export const SEND_INTERVAL_MS = Math.round((1000 * CONNECTIONS) / RATE);
// 突发结束后队列积压不会瞬间归零，PHASE4.md 要求记"队列积压最高值和消化完的时间"，采集要比
// 主场景多跑一段时间才能看到消化完的那一刻
const DRAIN_WATCH_SECONDS = Number(__ENV.BURST_DRAIN_WATCH_SECONDS || 180);

const TOTAL_DURATION_MS = DURATION_SECONDS * 1000;

export const options = {
  scenarios: {
    burst_load: {
      executor: 'per-vu-iterations',
      vus: CONNECTIONS,
      iterations: 1,
      maxDuration: `${DURATION_SECONDS + 60}s`,
      exec: 'openConnection',
    },
    queue_backlog_collector: {
      executor: 'constant-arrival-rate',
      rate: 1,
      timeUnit: '5s',
      duration: `${DRAIN_WATCH_SECONDS}s`,
      preAllocatedVUs: 1,
      maxVUs: 1,
      exec: 'collectBacklog',
    },
  },
};

export function openConnection() {
  const { token } = tokenForVU(__VU);
  const conversationId = conversationIdFor(SCENARIO_DIGIT, __VU);
  runPersistentConnection({
    gatewayWsUrl: GATEWAY_WS_URL,
    token,
    conversationId,
    messageIdPrefix: `burst-${__VU}`,
    sendIntervalMs: SEND_INTERVAL_MS,
    totalDurationMs: TOTAL_DURATION_MS,
    contentForSeq: (seq) => CONTENTS[seq % CONTENTS.length],
  });
}

export function collectBacklog() {
  pollQueueBacklog();
}
