/**
 * 场景 2「突发」（PHASE4.md 4.6 / REQUIREMENTS.md 6.4）：至少 1000 个虚拟用户，每秒 1000 条，
 * 持续 30 秒；每个用户每 10 秒不超过 15 条，避开网关的用户限流（RATE_LIMIT_USER_PER_10S=20，
 * 见 app/common/config.py）。
 *
 * `constant-arrival-rate` 目标吞吐量 1000/s，`preAllocatedVUs=1200`：k6 会把这 1000 次/秒的
 * 迭代尽量摊到当前活跃的 VU 上，1000 次/秒 ÷ 1200 个 VU ≈ 每个 VU 每秒 0.83 次，换算到 10 秒
 * 是约 8.3 条，本来就在 15 条以内，不需要在脚本里再手动加一层节流计数器——如果真实运行时
 * k6 输出的 `dropped_iterations` 不是 0（说明 VU 池不够、实际摊到某些 VU 头上的频率比预估高），
 * 要先看是不是某个用户被限流命中，再决定是调大 preAllocatedVUs 还是别的，如实记进
 * docs/LOADTEST.md，不要不看数据就调大池子蒙混过去。
 *
 * 突发场景要验证"发出的消息数和数据库里最终 replied 的消息数相等，证明不丢消息"（PHASE4.md），
 * 这个对不上不是靠 k6 查，是压测跑完之后用 scripts/sql.py 在数据库里数
 * `select count(*) from messages where tenant_id='t_a' and role='user' and status='replied'
 * and created_at > '<压测开始时间>'`，写进 docs/LOADTEST.md，不在这个脚本里做。
 *
 * 用法：
 *   docker compose run --rm k6 run burst.js
 *   docker compose run --rm k6 run --out csv=/loadtest/output/burst.csv burst.js
 */
import { sendOneMessage } from './lib/ws_client.js';
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

const DURATION = __ENV.BURST_DURATION || '30s';

export const options = {
  scenarios: {
    burst_load: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.BURST_RATE || 1000),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Number(__ENV.BURST_VUS || 1200),
      maxVUs: Number(__ENV.BURST_MAX_VUS || 1500),
      exec: 'sendMessage',
    },
    queue_backlog_collector: {
      executor: 'constant-arrival-rate',
      rate: 1,
      timeUnit: '5s',
      // 突发结束后队列积压不会瞬间归零，PHASE4.md 要求记"队列积压最高值和消化完的时间"，
      // 采集要比主场景多跑一段时间才能看到消化完的那一刻，不能主场景一停采集也跟着停
      duration: __ENV.BURST_DRAIN_WATCH || '3m',
      preAllocatedVUs: 1,
      maxVUs: 1,
      exec: 'collectBacklog',
    },
  },
};

export function sendMessage() {
  const { token } = tokenForVU(__VU);
  const conversationId = conversationIdFor(SCENARIO_DIGIT, __VU);
  const messageId = `burst-${__VU}-${__ITER}-${Date.now()}`;
  const content = CONTENTS[__ITER % CONTENTS.length];
  sendOneMessage(GATEWAY_WS_URL, token, conversationId, messageId, content);
}

export function collectBacklog() {
  pollQueueBacklog();
}
