// 压测脚本共用的 WebSocket 单次往返：连 gateway、发一条消息、按协议（app/common/schemas.py）
// 依次等 ack -> reply_chunk(可能多条) -> reply_end，记录三段耗时。跟 scripts/ws_client.py
// 是同一套协议理解，只是 ws_client.py 是给人手工验证用的 Python 脚本，这里是给 k6 压测用的
// JS 版本，两边故意不合并成一份——语言和运行时都不一样，硬共用只会让两边都要迁就对方。
import ws from 'k6/ws';
import { Trend, Counter } from 'k6/metrics';

// 三段耗时对应 PHASE4.md 4.6"消息里区分三个时间"：ACK 耗时、首句耗时、完整回复耗时。
// 命名跟 docs/LOADTEST.md 报告里的字段一一对应，不要改名，不然写报告的时候对不上。
export const ackLatencyMs = new Trend('ack_latency_ms', true);
export const firstChunkLatencyMs = new Trend('first_chunk_latency_ms', true);
export const fullReplyLatencyMs = new Trend('full_reply_latency_ms', true);

// 分开计数，不是所有"没拿到完整回复"都算系统出错：duplicate/rate_limited 是设计好的正常分支，
// client_timeout 才是真的没在预期时间内收到 reply_end（可能是系统卡住，也可能是压测本身把
// 系统压垮了），app_error 是收到 gateway 主动发的 type=error 消息或者 WebSocket 层报错。
export const appErrors = new Counter('app_errors');
export const appDuplicates = new Counter('app_duplicates');
export const appRateLimited = new Counter('app_rate_limited');
export const clientTimeouts = new Counter('app_client_timeouts');

// 跟 scripts/chat.py 的 REPLY_TIMEOUT_SECONDS 保持一致（30 秒）：压测时如果系统被压得比这个还慢，
// 直接算超时失败，不无限等下去卡住这个 VU 的下一次迭代。场景 4（LLM 超时率 20%）传了不同的值，
// 见 loadtest/llm_timeout.js 顶部注释里按 llm_nonstream_timeout_seconds（3 秒）/
// llm_stream_timeout_seconds（4 秒）算出来的推演，这里不重复。
const DEFAULT_REPLY_TIMEOUT_MS = 30000;

/**
 * 发一条消息并等完整回复，返回 k6 ws.connect 的 Response（主要看 .status 是不是 101）。
 * 不返回业务结果——业务结果都记进上面几个 Trend/Counter 里，报告直接从 k6 的汇总里拿。
 */
export function sendOneMessage(
  gatewayWsUrl,
  token,
  conversationId,
  messageId,
  content,
  timeoutMs = DEFAULT_REPLY_TIMEOUT_MS
) {
  const url = `${gatewayWsUrl}?token=${token}`;
  const t0 = Date.now();
  let firstChunkSeen = false;

  return ws.connect(url, {}, function (socket) {
    socket.on('open', function () {
      socket.send(
        JSON.stringify({
          type: 'message',
          message_id: messageId,
          conversation_id: conversationId,
          content: content,
        })
      );
    });

    socket.on('message', function (raw) {
      let msg;
      try {
        msg = JSON.parse(raw);
      } catch (e) {
        appErrors.add(1);
        socket.close();
        return;
      }

      if (msg.type === 'ack') {
        ackLatencyMs.add(Date.now() - t0);
        if (msg.status === 'duplicate') {
          appDuplicates.add(1);
          socket.close();
        } else if (msg.status === 'rate_limited') {
          appRateLimited.add(1);
          socket.close();
        }
        // status === 'accepted'：不关连接，继续等 reply_chunk/reply_end
      } else if (msg.type === 'reply_chunk') {
        if (!firstChunkSeen) {
          firstChunkSeen = true;
          firstChunkLatencyMs.add(Date.now() - t0);
        }
      } else if (msg.type === 'reply_end') {
        fullReplyLatencyMs.add(Date.now() - t0);
        socket.close();
      } else if (msg.type === 'error') {
        appErrors.add(1);
        socket.close();
      }
      // type === 'reminder'：压测场景不建提醒，理论上不会收到，收到也不处理
    });

    socket.on('error', function () {
      appErrors.add(1);
    });

    socket.setTimeout(function () {
      clientTimeouts.add(1);
      socket.close();
    }, timeoutMs);
  });
}
