"""统一配置入口：全项目任何地方需要配置，只能从这里的 Settings 拿，不允许各模块自己读环境变量。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
    llm_timeout_seconds: float = 15

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

    # ---------- 服务端口 ----------
    gateway_host_port: int = 8000
    worker_health_host_port: int = 8001
    mock_im_host_port: int = 8080
    mock_llm_host_port: int = 8100
    mock_knowledge_host_port: int = 8101
    mock_platform_host_port: int = 8102
    mock_finance_host_port: int = 8103


@lru_cache
def get_settings() -> Settings:
    # 用 lru_cache 让 Settings 全进程只解析一次环境变量，避免到处 new 出不一致的配置对象
    return Settings()
