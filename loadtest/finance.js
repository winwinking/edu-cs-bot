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
 * 持续时长 PHASE4.md/REQUIREMENTS.md 都没写死，默认给 2 分钟，够攒出稳定的 P95，可以用
 * FINANCE_DURATION 环境变量改；跟其它三个场景的持续时长（题目写死的 5 分钟/30 秒）不是同一个
 * 性质，这个默认值是本轮准备阶段自己定的，写进了 docs/LOADTEST.md 的设计说明里，不是题目原文。
 *
 * 用法：
 *   docker compose run --rm k6 run finance.js
 *   docker compose run --rm k6 run --out csv=/loadtest/output/finance.csv finance.js
 */
import { sendOneMessage } from './lib/ws_client.js';
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

const DURATION = __ENV.FINANCE_DURATION || '2m';

export const options = {
  scenarios: {
    finance_load: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.FINANCE_RATE || 100),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Number(__ENV.FINANCE_VUS || 300),
      maxVUs: Number(__ENV.FINANCE_MAX_VUS || 400),
      exec: 'sendMessage',
    },
    queue_backlog_collector: {
      executor: 'constant-arrival-rate',
      rate: 1,
      timeUnit: '5s',
      duration: DURATION,
      preAllocatedVUs: 1,
      maxVUs: 1,
      exec: 'collectBacklog',
    },
  },
};

export function sendMessage() {
  const { token } = tokenForVU(__VU);
  const conversationId = conversationIdFor(SCENARIO_DIGIT, __VU);
  const messageId = `finance-${__VU}-${__ITER}-${Date.now()}`;
  const content = CONTENTS[__ITER % CONTENTS.length];
  sendOneMessage(GATEWAY_WS_URL, token, conversationId, messageId, content);
}

export function collectBacklog() {
  pollQueueBacklog();
}
