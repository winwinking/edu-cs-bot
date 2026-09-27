// 每 5 秒采一次 RabbitMQ 队列积压（PHASE4.md 4.6"采集"要求），走 RabbitMQ 管理接口
// （跟 scripts/mockctl.py 之外——那个是控制 mock 服务，这里是查 RabbitMQ 自己的管理 API，
// 两个完全不同的接口）。用 k6 自己的 http 模块采，跟主场景在同一个 k6 进程里跑一个独立的
// "采集" scenario（见各场景文件的 options.scenarios），这样这份时间序列能跟主场景的延迟指标
// 一起通过 `k6 run --out csv=...` 落进同一份 CSV，事后按时间戳对齐着看，不用另外开一个采集
// 脚本、再手动拿时间戳去对表。
import http from 'k6/http';
import encoding from 'k6/encoding';
import { Gauge } from 'k6/metrics';

import { RABBITMQ_MGMT_URL, RABBITMQ_USER, RABBITMQ_PASSWORD } from './config.js';

export const queueBacklogInbound = new Gauge('queue_backlog_inbound');
export const queueBacklogDead = new Gauge('queue_backlog_dead');

function authHeader() {
  return 'Basic ' + encoding.b64encode(`${RABBITMQ_USER}:${RABBITMQ_PASSWORD}`);
}

function fetchQueueDepth(queueName) {
  const resp = http.get(`${RABBITMQ_MGMT_URL}/api/queues/%2f/${queueName}`, {
    headers: { Authorization: authHeader() },
    timeout: '5s',
  });
  if (resp.status !== 200) {
    // 管理接口偶尔慢一点很正常（阶段三就记过管理接口统计有几秒延迟这个已知问题），
    // 采集失败不应该让整个压测脚本报错退出，跳过这一次就行，下一次 5 秒后再采
    return null;
  }
  const body = JSON.parse(resp.body);
  return body.messages_ready || 0;
}

export function pollQueueBacklog() {
  const inbound = fetchQueueDepth('inbound.messages');
  if (inbound !== null) {
    queueBacklogInbound.add(inbound);
  }
  const dead = fetchQueueDepth('inbound.dead');
  if (dead !== null) {
    queueBacklogDead.add(dead);
  }
}
