/**
 * 场景 3「财务查询」（PHASE4.md 4.6 / REQUIREMENTS.md 6.4）：每秒 100 条财务问题，
 * 目标 P95 < 500ms（题目原文指标，报告里要单独把这条拎出来对比，见 docs/LOADTEST.md）。
 *
 * 内容全部是会走 `query_finance` 工具调用的问法（订单/账单/发票/余额/退费），跟场景 1/2 的纯
 * 知识问答刻意分开：财务查询要多打一次 worker -> mock-finance 的网络往返
 * （app/common/finance_client.py），完整回复的耗时构成跟知识问答不一样，PHASE4.md 要求
 * 报告里把"完整回复的 P95"和"meta.timings.finance 的 P95"分开写，就是要看清楚这多出来的一段
 * 网络往返占了多大比例。
 *
 * Jo 审查后改成长连接模型，连接数和每条连接的发送速率配套算出来：
 *   目标 100 条/秒 ÷ 200 条连接 = 每条连接 0.5 条/秒 = 每 10 秒 5 条
 * 5 条/10秒远低于网关限流 20 条/10秒，留了 4 倍余量。t_a 下 1600 个压测用户，200 条连接
 * 够用。
 *
 * 持续时长 PHASE4.md/REQUIREMENTS.md 都没写死，默认给 2 分钟，够攒出稳定的 P95，可以用
 * FINANCE_DURATION_SECONDS 环境变量改；跟其它三个场景的持续时长（题目写死的 5 分钟/30 秒）
 * 不是同一个性质，这个默认值是本轮准备阶段自己定的，写进了 docs/LOADTEST.md 的设计说明里，
 * 不是题目原文。
 *
 * 用法：
 *   docker compose run --rm k6 run finance.js
 *   docker compose run --rm k6 run --out csv=/loadtest/output/finance.csv finance.js
 */
import { runPersistentConnection } from './lib/ws_client.js';
import { tokenForVU } from './lib/tokens.js';
import { pollQueueBacklog } from './lib/rabbitmq.js';
import { GATEWAY_WS_URL, conversationIdFor } from './lib/config.js';

const SCENARIO_DIGIT = '3';

const CONTENTS = [
  '帮我查一下最近的账单',
  '我的发票开了吗',
  '查一下我的余额',
  '退费审核到哪一步了',
  '我最近的订单状态是什么',
];

export const DURATION_SECONDS = Number(__ENV.FINANCE_DURATION_SECONDS || 120);
export const RATE = Number(__ENV.FINANCE_RATE || 100);
export const CONNECTIONS = Number(__ENV.FINANCE_CONNECTIONS || 200);
export const SEND_INTERVAL_MS = Math.round((1000 * CONNECTIONS) / RATE);

const TOTAL_DURATION_MS = DURATION_SECONDS * 1000;

export const options = {
  scenarios: {
    finance_load: {
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
    messageIdPrefix: `finance-${__VU}`,
    sendIntervalMs: SEND_INTERVAL_MS,
    totalDurationMs: TOTAL_DURATION_MS,
    contentForSeq: (seq) => CONTENTS[seq % CONTENTS.length],
  });
}

export function collectBacklog() {
  pollQueueBacklog();
}
