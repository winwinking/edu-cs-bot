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
