// 压测脚本共用的 WebSocket 长连接客户端（Jo 审查后从"每条消息新建一次连接"改成"每个 VU 建一条
// 长连接、保持到场景结束、按固定节奏连续发消息，不等上一条回复回来再发下一条"）。旧版本每条
// 消息各自 ws.connect() 一次、发完等完整回复就关闭连接，ws_sessions 因此跟消息数基本相等，
// 测的是"大量短连接各发一条消息"，不是题目原意"N 个连接持续发消息"这个负载模型，也没有真的
// 验证网关维持大量长连接、同一条连接上并发处理多条在途消息的能力——这是本轮改造要解决的问题。
// 协议理解跟之前一样按 app/common/schemas.py，用 message_id/reply_to 把发出的消息和它的
// ack/reply_chunk/reply_end 对应起来（一条连接上任意时刻可能有多条消息在途，不能再用"连接级"
// 的单一状态变量去追踪，必须按 message_id 分别记录）。跟 scripts/ws_client.py 不合并成一份，
// 理由跟之前一样：语言和运行时不一样，硬共用只会让两边都要迁就对方。
import ws from 'k6/ws';
import { Trend, Counter } from 'k6/metrics';

// 三段耗时对应 PHASE4.md 4.6"消息里区分三个时间"：ACK 耗时、首句耗时、完整回复耗时。
// 命名跟 docs/LOADTEST.md 报告里的字段一一对应，不要改名，不然写报告的时候对不上。
// Jo 审查后明确要求：这三个耗时的起点都是"消息发出时"，不含建连时间——建连时间单独记
// wsConnectLatencyMs，不算进 ack_latency_ms。
export const ackLatencyMs = new Trend('ack_latency_ms', true);
export const firstChunkLatencyMs = new Trend('first_chunk_latency_ms', true);
export const fullReplyLatencyMs = new Trend('full_reply_latency_ms', true);
export const wsConnectLatencyMs = new Trend('ws_connect_latency_ms', true);

// 发出数/ACK 数/replied 数三个数分开计数（PHASE4.md 6.4 + Jo 本轮要求）：报告里要能把这三个
// 数字并排放在一起，尤其是突发场景要核对"发出数是否等于最终 replied 数"证明不丢消息。
// messagesReplied 在收到 reply_end 时加一，是"k6 客户端观测到的完整回复数"，跟数据库里
// status='replied' 的行数分开验证（数据库是最终真相，客户端这个数字只是交叉核对用）。
export const messagesSent = new Counter('messages_sent');
export const messagesAcked = new Counter('messages_acked');
export const messagesReplied = new Counter('messages_replied');

// 连接尝试数/成功数：单独统计"连接本身有没有建立成功"，别跟"消息有没有发出去"混在一起——
// 长连接模型下一条连接对应很多条消息，一次连接失败会导致这条连接上所有原计划要发的消息都
// 发不出去，需要单独看得出"损失是在连接层还是消息层"。
export const wsConnectAttempts = new Counter('ws_connect_attempts');
export const wsConnectSuccess = new Counter('ws_connect_success');

// 分开计数，不是所有"没拿到完整回复"都算系统出错：duplicate/rate_limited 是设计好的正常分支，
// client_timeout 才是真的没在预期时间内收到 reply_end（可能是系统卡住，也可能是压测本身把
// 系统压垮了），app_error 是收到 gateway 主动发的 type=error 消息或者 WebSocket 层报错。
export const appErrors = new Counter('app_errors');
export const appDuplicates = new Counter('app_duplicates');
export const appRateLimited = new Counter('app_rate_limited');
export const clientTimeouts = new Counter('app_client_timeouts');

// 跟 scripts/chat.py 的 REPLY_TIMEOUT_SECONDS 保持一致（30 秒）：单条消息发出去这么久还没等到
// reply_end 就判定这条消息客户端超时——只丢弃这一条消息的等待状态，不关闭整条连接（长连接模型
// 下一条消息超时不代表整条连接要断，后面的消息还要继续按节奏发）。场景 4 传了不同的值，见
// loadtest/llm_timeout.js 顶部注释。
const DEFAULT_REPLY_TIMEOUT_MS = 30000;

// 每 1 秒扫一次在途消息表，把超过超时阈值还没等到 reply_end 的记录清掉——用轮询而不是给每条
// 消息单独开一个 setTimeout，是因为 k6 的 ws.Socket 定时器数量出问题时不好排查，长连接一开
// 就是几分钟、成千上万条消息，轮询扫描表比维护成千上万个独立定时器更简单可靠。
const HOUSEKEEP_INTERVAL_MS = 1000;

/**
 * 建一条长连接，保持到 totalDurationMs 结束，期间按 sendIntervalMs 的固定节奏连续发消息，
 * 不等上一条的回复回来就发下一条——这是本轮改造的核心：模拟"一个用户开一条连接、后面陆续发
 * 很多条消息"，而不是"每条消息各自开一条连接"。
 *
 * 每条连接的发送速率（1000/sendIntervalMs 条/秒）必须留够余量地低于网关限流
 * RATE_LIMIT_USER_PER_10S（默认 20 条/10 秒）——具体每个场景配的 sendIntervalMs 和换算出来的
 * 每 10 秒条数见各自场景文件顶部注释和 docs/LOADTEST.md 里的表格，这里不重复计算。
 *
 * @param {object} opts
 * @param {string} opts.gatewayWsUrl
 * @param {string} opts.token
 * @param {string} opts.conversationId 这条连接整个生命周期只用这一个会话 id（模拟同一个用户
 *   连续对话，历史摘要等按会话累计的逻辑才有意义）
 * @param {string} opts.messageIdPrefix 生成 message_id 用的前缀，通常是 `${场景名}-${__VU}`
 * @param {number} opts.sendIntervalMs 发消息的固定间隔（毫秒）
 * @param {number} opts.totalDurationMs 这条连接总共保持多久（毫秒），到点主动关闭
 * @param {function(number): string} opts.contentForSeq 按第几条消息生成消息内容
 * @param {number} [opts.replyTimeoutMs] 单条消息等 reply_end 的超时（默认 30 秒）
 */
export function runPersistentConnection(opts) {
  const {
    gatewayWsUrl,
    token,
    conversationId,
    messageIdPrefix,
    sendIntervalMs,
    totalDurationMs,
    contentForSeq,
    replyTimeoutMs = DEFAULT_REPLY_TIMEOUT_MS,
  } = opts;

  const url = `${gatewayWsUrl}?token=${token}`;
  const tConnectStart = Date.now();
  let seq = 0;
  // message_id -> { sentAt, firstChunkSeen }：一条连接上任意时刻可能有多条消息在途
  const pending = {};

  wsConnectAttempts.add(1);

  ws.connect(url, {}, function (socket) {
    socket.on('open', function () {
      wsConnectSuccess.add(1);
      wsConnectLatencyMs.add(Date.now() - tConnectStart);

      socket.setInterval(function () {
        seq += 1;
        const messageId = `${messageIdPrefix}-${seq}-${Date.now()}`;
        const sentAt = Date.now();
        pending[messageId] = { sentAt: sentAt, firstChunkSeen: false };
        messagesSent.add(1);
        socket.send(
          JSON.stringify({
            type: 'message',
            message_id: messageId,
            conversation_id: conversationId,
            content: contentForSeq(seq),
          })
        );
      }, sendIntervalMs);

      socket.setInterval(function () {
        const now = Date.now();
        for (const id in pending) {
          if (now - pending[id].sentAt > replyTimeoutMs) {
            clientTimeouts.add(1);
            delete pending[id];
          }
        }
      }, HOUSEKEEP_INTERVAL_MS);

      socket.setTimeout(function () {
        socket.close();
      }, totalDurationMs);
    });

    socket.on('message', function (raw) {
      let msg;
      try {
        msg = JSON.parse(raw);
      } catch (e) {
        appErrors.add(1);
        return;
      }

      if (msg.type === 'ack') {
        const p = pending[msg.message_id];
        if (p !== undefined) {
          messagesAcked.add(1);
          ackLatencyMs.add(Date.now() - p.sentAt);
        }
        if (msg.status === 'duplicate') {
          appDuplicates.add(1);
          delete pending[msg.message_id];
        } else if (msg.status === 'rate_limited') {
          appRateLimited.add(1);
          delete pending[msg.message_id];
        }
        // status === 'accepted'：不做任何事，继续等这条消息的 reply_chunk/reply_end
      } else if (msg.type === 'reply_chunk') {
        const p = pending[msg.reply_to];
        if (p !== undefined && !p.firstChunkSeen) {
          p.firstChunkSeen = true;
          firstChunkLatencyMs.add(Date.now() - p.sentAt);
        }
      } else if (msg.type === 'reply_end') {
        const p = pending[msg.reply_to];
        if (p !== undefined) {
          fullReplyLatencyMs.add(Date.now() - p.sentAt);
          messagesReplied.add(1);
          delete pending[msg.reply_to];
        }
      } else if (msg.type === 'error') {
        appErrors.add(1);
        if (msg.message_id !== undefined && msg.message_id !== null) {
          delete pending[msg.message_id];
        }
      }
      // type === 'reminder'：压测场景不建提醒，理论上不会收到，收到也不处理
    });

    socket.on('error', function () {
      appErrors.add(1);
    });
  });
}
