/**
 * 场景 1「稳定」（PHASE4.md 4.6 / REQUIREMENTS.md 6.4）：500 个 WebSocket 连接，每秒 200 条，
 * 持续 5 分钟。
 *
 * Jo 审查后改成长连接模型（原来是"每条消息新建一次连接"，ws_sessions 跟消息数基本相等，测的
 * 是大量短连接各发一条消息，不是题目原意）：每个 VU 建一条连接、保持满 5 分钟，在这条连接上
 * 按固定节奏连续发消息，不等上一条回复回来就发下一条。连接数和"每条连接的发送速率"是配套算
 * 出来的，不是分别拍脑袋定的：
 *   目标 200 条/秒 ÷ 500 条连接 = 每条连接 0.4 条/秒 = 每 10 秒 4 条
 * 网关限流是每用户每 10 秒 20 条（`RATE_LIMIT_USER_PER_10S`），4 条只占限额的 20%，留了 5 倍
 * 余量。500 个连接、每个连接对应一个独立压测用户，t_a 下有 1600 个压测用户，够用（用了
 * 500/1600）。
 *
 * 用法：
 *   docker compose run --rm k6 run steady.js
 *   docker compose run --rm k6 run --out csv=/loadtest/output/steady.csv steady.js
 *   docker compose run --rm k6 run -e STEADY_DURATION_SECONDS=20 -e STEADY_RATE=5 \
 *     -e STEADY_CONNECTIONS=10 steady.js   # 本地小规模冒烟
 */
import { runPersistentConnection } from './lib/ws_client.js';
import { tokenForVU } from './lib/tokens.js';
import { pollQueueBacklog } from './lib/rabbitmq.js';
import { GATEWAY_WS_URL, conversationIdFor } from './lib/config.js';

const SCENARIO_DIGIT = '1';

// 避开 mock-llm 规则引擎里有特殊含义的词（"转人工"、"确认关闭"、"转人工摘要"这类），
// 纯知识问答，不触发工具调用，测的是"稳定负载下系统本身撑不撑得住"，不是业务分支覆盖
const CONTENTS = [
  '请问退课政策是什么',
  '寒假班的收费标准是什么',
  '上课时间怎么安排',
  '请假需要提前多久申请',
  '转班需要什么手续',
];

export const DURATION_SECONDS = Number(__ENV.STEADY_DURATION_SECONDS || 300);
export const RATE = Number(__ENV.STEADY_RATE || 200);
export const CONNECTIONS = Number(__ENV.STEADY_CONNECTIONS || 500);
// 每条连接的发送间隔（毫秒）：总目标速率均摊到每条连接上
export const SEND_INTERVAL_MS = Math.round((1000 * CONNECTIONS) / RATE);

const TOTAL_DURATION_MS = DURATION_SECONDS * 1000;

export const options = {
  scenarios: {
    steady_load: {
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
      duration: `${DURATION_SECONDS}s`,
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
    messageIdPrefix: `steady-${__VU}`,
    sendIntervalMs: SEND_INTERVAL_MS,
    totalDurationMs: TOTAL_DURATION_MS,
    contentForSeq: (seq) => CONTENTS[seq % CONTENTS.length],
  });
}

export function collectBacklog() {
  pollQueueBacklog();
}
