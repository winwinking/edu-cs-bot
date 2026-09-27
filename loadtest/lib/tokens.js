// 加载 loadtest/gen_users.py 生成的 token 文件。用 SharedArray 而不是普通数组：k6 会给每个 VU
// 起一个独立的 JS 隔离环境，普通模块级数组在每个 VU 里都会被复制一份——1600 个 token 乘以几百
// 上千个 VU，内存会炸；SharedArray 只在所有 VU 间保存一份只读数据。
import { SharedArray } from 'k6/data';

import { TOKENS_FILE } from './config.js';

export const tokens = new SharedArray('loadtest tokens', function () {
  const data = JSON.parse(open(TOKENS_FILE));
  if (!Array.isArray(data) || data.length === 0) {
    throw new Error(
      `${TOKENS_FILE} 是空的或者格式不对，先跑 loadtest/gen_users.py 生成压测用户和 token`
    );
  }
  return data;
});

// __VU 从 1 开始，按取模循环分配 token——同一个 VU 在整个测试过程中固定用同一个用户身份，
// 不会一会儿是这个用户一会儿是那个用户（真实用户就是这样，一个连接对应一个人）。
export function tokenForVU(vu) {
  return tokens[(vu - 1) % tokens.length];
}
