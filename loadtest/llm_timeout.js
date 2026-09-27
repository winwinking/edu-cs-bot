/**
 * 场景 4「LLM 超时率 20%」（PHASE4.md 4.6 / REQUIREMENTS.md 6.4）：用 mockctl 设置故障，
 * 跑场景 1（稳定）同样的负载，验证系统降级且不崩溃。
 *
 * 负载跟 steady.js 完全一样（复用同一套目标吞吐量/时长/连接数的思路，PHASE4.md 原文就是
 * "跑稳定场景的负载"），复用同一批环境变量名（`STEADY_DURATION_SECONDS`/`STEADY_RATE`/
 * `STEADY_CONNECTIONS`），这个文件不重新发明一遍连接数怎么算，只是套了个不同的
 * SCENARIO_DIGIT（避免会话 id 和 steady.js 撞在一起）。
 *
 * 检查点审查修复：原计划用 `error_rate=0.2` 近似"20% 超时率"，被指出这是错的——`error_rate`
 * 命中时 mock-llm 立刻返回 500，worker 立刻重试/降级，跟"客户端一直等到超时阈值、这段时间
 * 连接和协程被占用"是两种完全不同的压力，题目要测的是后者。已经给 `mocks/mock_llm/main.py`
 * 加了真正的 `timeout_rate` 参数（命中后 `await asyncio.Event().wait()` 永远不返回，细节和
 * worker 超时阈值的出处见 mock-llm 源码里 `_maybe_timeout()` 的注释），这个场景改用它。
 *
 * 单条消息等 reply_end 的超时（`replyTimeoutMs`）重新按新的超时值估算过（原来给 70 秒是按
 * "超时会重试、非流式 15 秒"那版旧行为估的，已经过时）：
 * - 非流式（classify）超时 3 秒且不重试（`llm_nonstream_timeout_seconds`），命中就直接降级成
 *   关键词规则、respond 阶段改用固定话术模板，不会再调用一次 LLM——这条路径最多 3 秒出头。
 * - 流式（respond）超时 4 秒且不重试（`llm_stream_timeout_seconds`；定 4 不是 5，是因为
 *   故障注入 9 本身就是拿 mock-llm 延迟 5 秒复测这个超时行为的，超时值也是 5 秒会跟注入的
 *   延迟撞在一起、谁先到没有确定性，错开一秒才稳定），mock-llm 的 `timeout_rate` 判定只在
 *   请求最开始做一次（见 `_maybe_timeout()`），命中就是整个请求挂起，不会中途才卡住——这条
 *   路径最多 4 秒出头。
 * - classify 和 respond 是否命中 timeout_rate 相互独立，但 classify 一旦命中就直接走模板
 *   回复、不会再有 respond 阶段的 LLM 调用，两段不会叠加成"3+4"；两段各自独立的最坏情况取
 *   较大值，约 4~5 秒（含正常的分类/检索开销）。
 * 给 15 秒（`LLM_TIMEOUT_SCENARIO_CLIENT_TIMEOUT_MS`）留出压测负载下排队/DB/Redis 抖动的
 * 余量，比 4~5 秒的理论值宽松不少，但远小于旧版的 70 秒；长连接模型下这个超时只影响单条消息
 * 的等待判定，不会关闭整条连接，后面的消息还会按节奏继续发。
 *
 * 运行前后各一步 mockctl（`make loadtest-llm-timeout` 已经把这两步和 k6 运行串在一起，用
 * trap 保证不管 k6 那步成功/失败/被中断都会执行 reset，见 Makefile 注释）：
 *   docker compose run --rm tools python scripts/mockctl.py llm timeout_rate=0.2
 *   docker compose run --rm k6 run llm_timeout.js
 *   docker compose run --rm tools python scripts/mockctl.py llm reset
 *
 * 用法：
 *   docker compose run --rm k6 run --out csv=/loadtest/output/llm_timeout.csv llm_timeout.js
 */
import { runPersistentConnection } from './lib/ws_client.js';
import { tokenForVU } from './lib/tokens.js';
import { pollQueueBacklog } from './lib/rabbitmq.js';
import { GATEWAY_WS_URL, conversationIdFor } from './lib/config.js';

const SCENARIO_DIGIT = '4';

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
export const SEND_INTERVAL_MS = Math.round((1000 * CONNECTIONS) / RATE);
const CLIENT_REPLY_TIMEOUT_MS = Number(__ENV.LLM_TIMEOUT_SCENARIO_CLIENT_TIMEOUT_MS || 15000);

const TOTAL_DURATION_MS = DURATION_SECONDS * 1000;

export const options = {
  scenarios: {
    llm_timeout_load: {
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
    messageIdPrefix: `llmtimeout-${__VU}`,
    sendIntervalMs: SEND_INTERVAL_MS,
    totalDurationMs: TOTAL_DURATION_MS,
    contentForSeq: (seq) => CONTENTS[seq % CONTENTS.length],
    replyTimeoutMs: CLIENT_REPLY_TIMEOUT_MS,
  });
}

export function collectBacklog() {
  pollQueueBacklog();
}
