/**
 * 场景 1「稳定」（PHASE4.md 4.6 / REQUIREMENTS.md 6.4）：500 个 WebSocket 连接，每秒 200 条，
 * 持续 5 分钟。
 *
 * 用 `constant-arrival-rate`（按目标吞吐量控制，不是按并发数控制）直接把"每秒 200 条"这个指标
 * 锁死，`preAllocatedVUs`/`maxVUs` 给到 500/600——这是达到 200 msg/s 且每次往返要等
 * ack+完整回复（不是发完就走）所需要的并发连接数量级，不是随便拍的数字：如果一次往返平均要
 * 2~3 秒，500 个并发连接同时各自在处理一次往返，粗算吞吐量就是 500 / 2.5 ≈ 200 条/秒，跟目标
 * 吞吐量自然对上，这就是"500 个连接"和"200 条/秒"这两个指标为什么能同时满足、不用另外调参数
 * 凑数字。真实运行时如果 k6 提示 VU 池不够用（`dropped_iterations` 升高），说明实际往返耗时比
 * 预估的长，需要调大 STEADY_MAX_VUS，到时候在 docs/LOADTEST.md 里如实记录，不悄悄改小目标吞吐量。
 *
 * 用法：
 *   docker compose run --rm k6 run steady.js
 *   docker compose run --rm k6 run --out csv=/loadtest/output/steady.csv steady.js
 *   docker compose run --rm k6 run -e STEADY_DURATION=30s -e STEADY_RATE=50 steady.js   # 本地小规模冒烟
 */
import { sendOneMessage } from './lib/ws_client.js';
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

const DURATION = __ENV.STEADY_DURATION || '5m';

export const options = {
  scenarios: {
    steady_load: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.STEADY_RATE || 200),
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Number(__ENV.STEADY_VUS || 500),
      maxVUs: Number(__ENV.STEADY_MAX_VUS || 600),
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
  const messageId = `steady-${__VU}-${__ITER}-${Date.now()}`;
  const content = CONTENTS[__ITER % CONTENTS.length];
  sendOneMessage(GATEWAY_WS_URL, token, conversationId, messageId, content);
}

export function collectBacklog() {
  pollQueueBacklog();
}
