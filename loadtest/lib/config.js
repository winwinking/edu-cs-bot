// 压测脚本共用配置。全部从环境变量读（`docker compose run --rm k6 run -e KEY=VALUE ...` 或
// docker-compose.yml 里 k6 服务的 environment），跑在 docker 网络内部，直接用服务名不用宿主机
// 映射端口——跟 scripts/mockctl.py 访问 mock 服务是同一个道理。

export const GATEWAY_WS_URL = __ENV.GATEWAY_WS_URL || 'ws://gateway:8000/ws';
export const TENANT_ID = __ENV.LOADTEST_TENANT || 't_a';
// 用容器内的绝对路径，不用相对路径：k6 的 `open()` 对相对路径的解析是相对"调用 open() 的
// 那个模块文件自己的位置"，不是相对入口脚本——这个文件在 tokens.js 里被 open()，写成相对路径
// 容易解析成 loadtest/lib/tokens.json（多绕一层 lib/），而不是我们真正想要的
// loadtest/tokens.json。docker-compose.yml 里 k6 服务把宿主机的 ./loadtest 挂到容器里的
// /loadtest，用绝对路径直接消掉这个歧义。
export const TOKENS_FILE = __ENV.TOKENS_FILE || '/loadtest/tokens.json';

export const RABBITMQ_MGMT_URL = __ENV.RABBITMQ_MGMT_URL || 'http://rabbitmq:15672';
export const RABBITMQ_USER = __ENV.RABBITMQ_USER || 'guest';
export const RABBITMQ_PASSWORD = __ENV.RABBITMQ_PASSWORD || '';

// 每个场景一个数字前缀，拼进会话 UUID 里，避免不同场景的会话 id 撞在一起（同一批压测用户
// token 是四个场景共用的，如果 conversation_id 算法不带场景区分，两个场景前后脚跑，第二个
// 场景会命中第一个场景留下的历史对话，多轮上下文不是压测想验证的东西，也会让首句延迟这些
// 指标混进"生成历史摘要"的额外耗时）。
export function conversationIdFor(scenarioDigit, vu) {
  const hex = vu.toString(16).padStart(12, '0');
  return `00000000-0000-4000-800${scenarioDigit}-${hex}`;
}
