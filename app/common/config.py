"""统一配置入口：全项目任何地方需要配置，只能从这里的 Settings 拿，不允许各模块自己读环境变量。"""
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty=True：环境变量存在但值是空字符串时，当成"没设置"处理，落回代码默认值，
    # 不会尝试把 "" 解析成 int/float 报错。阶段四审查发现的真实问题：.env.example 里
    # `DEFAULT_DAILY_TOKEN_BUDGET=`（等号后面留空，说明"不填就是不限额"）在 CI 里被
    # `cp .env.example .env` 原样复制成正式配置，pydantic 把这个空字符串当成"传了一个值"去
    # 解析成 Optional[int]，解析失败直接让 Settings() 在模块导入阶段抛异常，19 个单测文件
    # collection 全部失败；本机也发生过一次一样的事故（把这一项手动删空导致 mock-im/worker
    # 反复重启），当时靠删掉整行绕过，没有从配置加载层根治。这个选项对本来就用 `= ""` 当默认值
    # 的字段（比如 REDIS_PASSWORD）没有副作用——"当成没设置"落回的默认值本来就是空字符串，
    # 结果不变；对必填字符串字段（比如 JWT_SECRET）反而是变严格了：以前留空会静默变成空字符串
    # 密钥，现在会因为"缺少必填字段"直接报错拒绝启动，不会带着空密钥跑起来。
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

    # ---------- 应用行为 ----------
    app_env: str = "development"
    log_level: str = "INFO"
    ws_message_max_length: int = 2000
    dedup_ttl_seconds: int = 86400
    conversation_history_limit: int = 10

    # ---------- PostgreSQL ----------
    postgres_user: str
    postgres_password: str
    postgres_db: str
    postgres_host: str
    postgres_port: int = 5432
    postgres_host_port: int = 5432
    database_url: str

    # ---------- Redis ----------
    redis_host: str
    redis_port: int = 6379
    redis_host_port: int = 6380
    redis_password: str = ""
    redis_url: str

    # ---------- RabbitMQ ----------
    rabbitmq_user: str
    rabbitmq_password: str
    rabbitmq_host: str
    rabbitmq_port: int = 5672
    rabbitmq_host_port: int = 5672
    rabbitmq_management_host_port: int = 15672
    rabbitmq_url: str
    mq_prefetch_count: int = 32

    # ---------- 外部调用超时（秒）----------
    db_connect_timeout_seconds: float = 5
    redis_timeout_seconds: float = 5
    mq_connect_timeout_seconds: float = 10

    # ---------- JWT ----------
    jwt_secret: str
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    # ---------- LLM（切换 mock-llm / DeepSeek 只改这几个环境变量，代码不用动）----------
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    # 故障注入 9（阶段四 4.5）审查发现：原来只有一个 15 秒的笼统超时，mock-llm 延迟 5 秒时，
    # classify+respond 两段加起来要等约 14 秒还没触发降级；超时后原来还会重试 1 次，最坏情况
    # 一步就要约 30 秒——用户等不起，也起不到"降级"应有的效果。拆成两个独立的超时，含义和用法
    # 见 app/common/llm_client.py：
    # - 非流式调用（分类意图/转人工摘要/历史摘要）：一次性等答案，没有半截内容可保留，超时就
    #   直接降级最省事，给短一点——3 秒是"用户能接受的等待"和"给真实 LLM 留出识别时间"的取舍。
    #   接真实 LLM 演示时如果发现经常顶到这个值，调大这个数字就行，不用改代码。
    llm_nonstream_timeout_seconds: float = 3
    # - 流式调用（respond 生成回复正文）：httpx 的 read 超时语义本来就是"距离上一次收到数据
    #   过了多久"而不是"总共花了多久"，直接拿来实现"等第一个字最多这么久、之后每两个字之间
    #   间隔也最多这么久"，不限制总时长——回复越长，允许的总耗时自然也越长，符合直觉。定 4 秒
    #   不是 5 秒：故障注入 9 用 mock-llm 延迟 5 秒复测这次修复时，如果超时值也是 5 秒，两个
    #   5 秒谁先到没有确定性，重测结果会在"超时降级"和"卡够 5 秒后终于收到"之间摇摆，跟故障
    #   延迟错开一秒才能稳定复现"超时"这个分支。
    llm_stream_timeout_seconds: float = 4

    # ---------- mock-llm 行为配置 ----------
    mock_llm_latency_ms: int = 300
    mock_llm_error_rate: float = 0.0
    mock_llm_mode: str = "normal"

    # ---------- 知识库 ----------
    # 阶段二默认用不依赖网络/模型的哈希向量（见 app/common/embedding.py），做成可切换的接口，
    # 以后换成真实 embedding 模型只需要新增一个 EMBEDDING_PROVIDER 的实现，不用改调用方代码
    embedding_provider: str = "hash"
    # pgvector 是主路径；mock_knowledge 用来验证"检索也可以是外部系统"这条设计不是硬编码死的
    retriever: str = "pgvector"
    # 低于这个分数就认为"没查到明确依据"，直接走固定话术，不给 LLM 编的机会；具体标定过程见
    # docs/phase2_threshold.md
    knowledge_min_score: float = 0.30
    mock_knowledge_base_url: str = "http://mock-knowledge:8000"
    mock_knowledge_timeout_seconds: float = 2
    # mock_knowledge 的打分方式（两字片段 Jaccard）和 pgvector 完全不是一个尺度，必须单独标定，
    # 不能共用 knowledge_min_score。标定结果显示两组分数没有干净分开（有重叠），这个值是
    # "宁可漏判也不误判"的保守取值，不是像 knowledge_min_score 那样有干净分界的标定结果，
    # 细节和原因见 docs/phase2_threshold.md
    mock_knowledge_min_score: float = 0.04

    # ---------- 财务查询（阶段二 2.9） ----------
    # worker 和 mock-finance 之间的服务令牌，不是用户的登录 token；mock 服务专用，不是真实密钥，
    # 但同样只从环境变量读，不写死在代码里
    finance_service_token: str
    mock_finance_base_url: str = "http://mock-finance:8000"
    finance_timeout_seconds: float = 1.5
    mock_finance_latency_ms: int = 50
    mock_finance_mode: str = "normal"

    # ---------- 平台指令与二次确认（阶段二 2.10） ----------
    mock_platform_base_url: str = "http://mock-platform:8000"
    platform_timeout_seconds: float = 3
    mock_platform_latency_ms: int = 50
    mock_platform_mode: str = "normal"
    # 待确认操作的有效期：超过这个时间用户还没回复"确认……"，就按超时处理，不再执行
    pending_action_ttl_seconds: int = 300

    # ---------- 提醒调度（阶段三 2） ----------
    scheduler_interval_seconds: float = 1
    scheduler_batch_size: int = 100

    # ---------- 限流（阶段三 4，设计决定 8）----------
    rate_limit_user_per_10s: int = 20
    rate_limit_tenant_per_sec: int = 2000

    # ---------- Redis 断线重连（阶段三 4，gateway 订阅回复用）----------
    # 重连间隔从这个值开始，每次翻倍，封顶在 max；不能无限等，也不能一直用固定间隔猛敲 Redis
    redis_reconnect_min_seconds: float = 0.5
    redis_reconnect_max_seconds: float = 30

    # ---------- 熔断器（阶段三 5，设计决定 10）：LLM 和 mock-finance 各一个，状态存进程内存 ----------
    cb_failure_threshold: int = 5
    cb_open_seconds: float = 30

    # ---------- 重试（阶段三 5，设计决定 11）：次数和退避间隔都放配置，不写死在代码里 ----------
    llm_max_retries: int = 1
    llm_retry_backoff_seconds: float = 0.3
    finance_max_retries: int = 1
    finance_retry_backoff_seconds: float = 0.2
    # 抖动范围：实际退避 = base + uniform(0, jitter)，避免多个请求同时超时后又同时重试撞在一起
    finance_retry_backoff_jitter_seconds: float = 0.1
    platform_max_retries: int = 2
    # 平台指令保留原来"第 1 次重试等 0.5 秒、第 2 次等 1 秒"的阶梯退避，用一个基数乘以第几次重试算出来
    platform_retry_backoff_base_seconds: float = 0.5

    # ---------- 死信（阶段三 5，设计决定 12）----------
    # 消息头 x-retry-count 达到这个值还失败，就不再重新投回队列，直接进 inbound.dead
    dlq_max_retries: int = 3

    # ---------- token 预算（阶段三 6，设计决定 13）----------
    # 机构没在 tenants.daily_token_budget 里单独设置时用这个值；留空（None）就是不限额，
    # 开发/测试环境默认不设，演示预算降级时才通过 mockctl 之外的方式（改这个机构的这一列）单独打开
    default_daily_token_budget: Optional[int] = None

    # ---------- 服务端口 ----------
    gateway_host_port: int = 8000
    worker_health_host_port: int = 8011
    scheduler_health_host_port: int = 8002
    mock_im_host_port: int = 8080
    mock_llm_host_port: int = 8100
    mock_knowledge_host_port: int = 8101
    mock_platform_host_port: int = 8102
    mock_finance_host_port: int = 8103


@lru_cache
def get_settings() -> Settings:
    # 用 lru_cache 让 Settings 全进程只解析一次环境变量，避免到处 new 出不一致的配置对象
    return Settings()
