"""mock-llm 新增 `timeout_rate` 参数的测试（PHASE4.md 4.6 检查点审查修复）。

不测"聊天补全回复内容对不对"（那是 test_mock_llm_rules.py 测 rules.py 纯函数的事），这里只测
运维行为：`timeout_rate` 命中的请求要真的"很久不返回"，不是返回一个错误——这跟
`error_rate`/`mode=error500`（命中就立刻返回 500）是完全不同的语义，压测场景 4「LLM 超时率
20%」要验证的是"worker 一直卡到自己的超时阈值、这段时间被占用"，不是"LLM 立刻报错、worker
立刻重试"，这也是审查后从"用 error_rate 近似超时"改成"给 mock-llm 加真超时模式"的原因。

跟 test_mock_llm_rules.py 一样，`mocks/` 目录只打进 mocks 镜像，这个文件在 tools（app）镜像里
跑不到 `mocks.mock_llm.main`，用 importorskip 优雅跳过。真正执行要用 mocks 镜像的一次性容器：
    docker compose run --rm mocks-tools pytest -q tests/unit/test_mock_llm_timeout.py
`make test` 会自动跑这一步。

第一次真跑（故障注入结束后）验证了文件顶部原先的担心是真的：`TestClient` 用的是 httpx
`ASGITransport`（进程内直接调用 ASGI app，不走真实 socket），`_maybe_timeout()` 里的
`await asyncio.Event().wait()` 永远不返回时，httpx 的 `timeout=` 参数对这种进程内传输不生效
（没有真实 I/O 可以超时），`client.post(..., timeout=0.3)` 会跟着永远卡住，不会抛异常——
这条用例原来的写法（`with pytest.raises(Exception): client.post(..., timeout=0.3)`）会让
整个 `make test` 卡死，构建验证时两次都卡在这条用例上。已经按文件顶部原来写的备用方案改成
"另外想办法验证请求真的卡住了"：把请求放进一个 `daemon=True` 的线程里跑，`thread.join(1.0)`
最多等 1 秒就拿回控制权，断言线程"1 秒后还活着"（真的卡住了），而不是等它抛异常——daemon
线程不会阻塞进程退出，即使这次请求在整个测试进程生命周期内都不会返回也没关系。
"""
import threading

import pytest

pytest.importorskip("mocks.mock_llm.main")

from fastapi.testclient import TestClient  # noqa: E402

from mocks.mock_llm.main import _DEFAULT_CONFIG, _config, app  # noqa: E402

client = TestClient(app)

_CHAT_PAYLOAD = {"model": "mock-gpt", "messages": [{"role": "user", "content": "你好"}]}


@pytest.fixture(autouse=True)
def _reset_mock_llm_config():
    # 每个用例前后都恢复默认配置，不让某个用例改的 timeout_rate/mode 漏到下一个用例里
    _config.update(_DEFAULT_CONFIG)
    yield
    _config.update(_DEFAULT_CONFIG)


def test_admin_config_accepts_timeout_rate():
    resp = client.post("/admin/config", json={"timeout_rate": 0.2})
    assert resp.status_code == 200
    assert resp.json()["timeout_rate"] == 0.2


def test_admin_reset_restores_timeout_rate_to_zero():
    client.post("/admin/config", json={"timeout_rate": 0.5})
    resp = client.post("/admin/reset")
    assert resp.status_code == 200
    assert resp.json()["timeout_rate"] == 0.0


def test_timeout_rate_zero_does_not_hang():
    # 默认值 0.0：新参数不应该影响任何已有行为，正常请求正常返回
    resp = client.post("/v1/chat/completions", json=_CHAT_PAYLOAD)
    assert resp.status_code == 200


def test_timeout_rate_one_hangs_past_client_timeout():
    # timeout_rate=1.0：这次请求 100% 命中超时分支，应该一直挂起不返回——TestClient 的
    # ASGITransport 是进程内直接调用，没有真实 socket I/O，httpx 的 timeout= 参数对这种
    # 挂起的协程不生效（不会抛异常），所以不能指望"传个 timeout= 然后等它超时报错"，改成
    # 放进一个 daemon 线程里跑，等 1 秒后检查线程还活不活着——还活着就是真的卡住了，
    # daemon 线程不会因为一直卡住而阻塞测试进程退出
    client.post("/admin/config", json={"timeout_rate": 1.0})

    thread = threading.Thread(
        target=client.post, args=("/v1/chat/completions",), kwargs={"json": _CHAT_PAYLOAD}, daemon=True
    )
    thread.start()
    thread.join(timeout=1.0)

    assert thread.is_alive(), "timeout_rate=1.0 命中时请求应该一直挂起，不应该在 1 秒内返回"
