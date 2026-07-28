"""配置管理模块 - 集中管理所有配置项"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

@dataclass
class ServerConfig:
    """服务器配置"""
    host: str = field(
        default_factory=lambda: os.getenv('MUYE_AGENT_HOST', '127.0.0.1')
    )
    port: int = field(
        default_factory=lambda: int(os.getenv('MUYE_AGENT_PORT', '9860'))
    )
    workers: int = field(
        default_factory=lambda: int(os.getenv('SERVER_WORKERS', '1'))
    )
    log_level: str = field(
        default_factory=lambda: os.getenv('SERVER_LOG_LEVEL', 'info')
    )
    access_log: bool = field(
        default_factory=lambda: os.getenv('SERVER_ACCESS_LOG', 'true').lower() == 'true'
    )


@dataclass
class APIConfig:
    """API 相关配置"""
    timeout: int = field(
        default_factory=lambda: int(os.getenv('API_TIMEOUT', '380'))
    )
    retries: int = field(
        default_factory=lambda: int(os.getenv('API_RETRIES', '3'))
    )


@dataclass
class HTTPPoolConfig:
    """HTTP 连接池配置"""
    max_connections: int = field(
        default_factory=lambda: int(os.getenv('HTTP_MAX_CONNECTIONS', '200'))
    )
    max_keepalive: int = field(
        default_factory=lambda: int(os.getenv('HTTP_MAX_KEEPALIVE', '50'))
    )
    keepalive_expiry: float = field(
        default_factory=lambda: float(os.getenv('HTTP_KEEPALIVE_EXPIRY', '30.0'))
    )


@dataclass
class MiddlewareConfig:
    """中间件配置"""
    loop_warn_threshold: int = field(
        default_factory=lambda: int(os.getenv('LOOP_WARN_THRESHOLD', '3'))
    )
    loop_hard_limit: int = field(
        default_factory=lambda: int(os.getenv('LOOP_HARD_LIMIT', '5'))
    )

    # LLM错误重试配置
    llm_max_retries: int = field(
        default_factory=lambda: int(os.getenv('LLM_MAX_RETRIES', '3'))
    )
    llm_base_delay: float = field(
        default_factory=lambda: float(os.getenv('LLM_BASE_DELAY', '1.0'))
    )
    llm_max_delay: float = field(
        default_factory=lambda: float(os.getenv('LLM_MAX_DELAY', '8.0'))
    )

    # 文件上传配置
    uploads_base_dir: Optional[str] = field(
        default_factory=lambda: os.getenv('UPLOADS_BASE_DIR', None)
    )


@dataclass
class LLMConfig:
    """主 Agent 到 muye-llm 的内部网关配置，不保存供应商凭据。"""
    model: str = field(
        default_factory=lambda: os.getenv('MUYE_LLM_MODEL', 'deepseek-v4-flash')
    )
    api_base: str = field(
        default_factory=lambda: os.getenv('MUYE_LLM_BASE_URL', 'http://127.0.0.1:9850')
    )

    vision_model: str = field(
        default_factory=lambda: os.getenv('MUYE_LLM_MODEL', 'deepseek-v4-flash')
    )
    vision_api_base: str = field(
        default_factory=lambda: os.getenv('MUYE_LLM_BASE_URL', 'http://127.0.0.1:9850')
    )

    temperature: float = field(
        default_factory=lambda: float(os.getenv('MUYE_LLM_TEMPERATURE', '0.1'))
    )
    max_tokens: int = field(
        default_factory=lambda: int(os.getenv('MUYE_LLM_MAX_TOKENS', '4096'))
    )


@dataclass
class CheckpointerConfig:
    """Checkpointer 配置（仅负责持久化存储）"""

    # 后端类型: memory, sqlite, postgres
    backend: str = field(
        default_factory=lambda: os.getenv('CHECKPOINTER_BACKEND', 'sqlite')
    )

    # SQLite 数据库路径
    sqlite_path: str = field(
        default_factory=lambda: os.getenv('CHECKPOINTER_SQLITE_PATH', 'conversations.db')
    )

    # PostgreSQL 连接字符串（格式: postgresql://用户名:密码@主机:端口/数据库名）
    postgresql_uri: str = field(
        default_factory=lambda: os.getenv(
            'CHECKPOINTER_POSTGRESQL_URI',
            ''
        )
    )
    # PostgreSQL 连接池配置
    postgres_pool_size: int = field(
        default_factory=lambda: int(os.getenv('POSTGRES_POOL_SIZE', '20'))
    )
    postgres_max_overflow: int = field(
        default_factory=lambda: int(os.getenv('POSTGRES_MAX_OVERFLOW', '10'))
    )
    postgres_pool_timeout: int = field(
        default_factory=lambda: int(os.getenv('POSTGRES_POOL_TIMEOUT', '30'))
    )
    postgres_pool_recycle: int = field(
        default_factory=lambda: int(os.getenv('POSTGRES_POOL_RECYCLE', '3600'))
    )


@dataclass
class CompressionConfig:
    """消息压缩配置（独立于 Checkpointer）"""

    # 压缩模式: 'compress' (压缩旧消息), 'truncate' (直接截断), 'none' (不处理)
    mode: str = field(
        default_factory=lambda: os.getenv('COMPRESSION_MODE', 'truncate')
    )

    # 是否启用压缩（推荐启用，支持更长对话历史）
    enable_compression: bool = field(
        default_factory=lambda: os.getenv('COMPRESSION_ENABLE', 'true').lower() == 'true'
    )

    # 热数据窗口大小（最近 N 轮完整保留）
    hot_window_size: int = field(
        default_factory=lambda: int(os.getenv('COMPRESSION_HOT_WINDOW_SIZE', '20'))
    )

    # 压缩阈值（超过 N 轮开始压缩）
    compression_threshold: int = field(
        default_factory=lambda: int(os.getenv('COMPRESSION_THRESHOLD', '20'))
    )

    # 压缩间隔（每 N 轮重新压缩一次）
    compression_interval: int = field(
        default_factory=lambda: int(os.getenv('COMPRESSION_INTERVAL', '5'))
    )

    # 热数据最大 tokens
    hot_window_tokens: int = field(
        default_factory=lambda: int(os.getenv('COMPRESSION_HOT_WINDOW_TOKENS', '12000'))
    )

    # 压缩摘要最大 tokens
    compressed_tokens: int = field(
        default_factory=lambda: int(os.getenv('COMPRESSION_COMPRESSED_TOKENS', '1000'))
    )

    # 截断模式配置 - 保留最近 N 轮对话（0 表示不限制）
    keep_recent_turns: int = field(
        default_factory=lambda: int(os.getenv('COMPRESSION_KEEP_RECENT_TURNS', '5'))
    )

    # 简单截断配置（禁用压缩时使用）- 最大历史消息数（0 表示不限制）
    max_messages: int = field(
        default_factory=lambda: int(os.getenv('COMPRESSION_MAX_MESSAGES', '50'))
    )


@dataclass
class ContentProcessingConfig:
    """内容处理配置"""
    # 内容截断长度
    max_content_length: int = field(
        default_factory=lambda: int(os.getenv('MAX_CONTENT_LENGTH', '4096'))
    )

    # 压缩摘要长度
    compression_summary_length: int = field(
        default_factory=lambda: int(os.getenv('COMPRESSION_SUMMARY_LENGTH', '1000'))
    )

    # 记忆内容截断长度
    memory_content_max_length: int = field(
        default_factory=lambda: int(os.getenv('MEMORY_CONTENT_MAX_LENGTH', '1000'))
    )


@dataclass
class InfoQuestConfig:
    """InfoQuest API 配置"""
    search_url: str = field(
        default_factory=lambda: os.getenv(
            'INFOQUEST_SEARCH_URL',
            'https://search.infoquest.bytepluses.com'
        )
    )
    reader_url: str = field(
        default_factory=lambda: os.getenv(
            'INFOQUEST_READER_URL',
            'https://reader.infoquest.bytepluses.com'
        )
    )


@dataclass
class WebSearchConfig:
    """网页搜索配置"""
    # 启用的搜索引擎列表: ddg, tavily, infoquest, langsearch, serper
    enabled_engines: list = field(
        default_factory=lambda: os.getenv('WEB_SEARCH_ENGINES', 'serper,langsearch,tavily,ddg').split(',')
    )
    # 搜索引擎优先级顺序（用于 web_search_auto 降级）
    engine_priority: list = field(
        default_factory=lambda: os.getenv('WEB_SEARCH_ENGINE_PRIORITY', 'langsearch,tavily,ddg').split(',')
    )
    # 搜索结果数量
    max_results: int = field(
        default_factory=lambda: int(os.getenv('WEB_SEARCH_MAX_RESULTS', '5'))
    )
    # LangSearch 的 API Key
    langsearch_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv('LANGSEARCH_API_KEY', '')
    )
    # Tavily 的 API Key
    tavily_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv('TAVILY_API_KEY', '')
    )
    # InfoQuest 的 API Key
    infoquest_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv('INFOQUEST_API_KEY', '')
    )
    # InfoQuest 时间范围过滤（天），-1 表示禁用
    infoquest_time_range: int = field(
        default_factory=lambda: int(os.getenv('INFOQUEST_TIME_RANGE', '-1'))
    )
    # Google Serper 的 API Key
    serper_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv('SERPER_API_KEY', '')
    )
    # Google Serper 的 Base URL
    serper_base_url: str = field(
        default_factory=lambda: os.getenv('SERPER_BASE_URL', 'https://google.serper.dev')
    )


@dataclass
class WebFetchConfig:
    """网页抓取配置"""
    # 是否启用网页抓取
    enabled: bool = field(
        default_factory=lambda: os.getenv('WEB_FETCH_ENABLED', 'true').lower() == 'true'
    )
    # 抓取超时时间（秒）
    timeout: int = field(
        default_factory=lambda: int(os.getenv('WEB_FETCH_TIMEOUT', '10'))
    )
    max_response_bytes: int = field(
        default_factory=lambda: int(os.getenv('WEB_FETCH_MAX_RESPONSE_BYTES', '2000000'))
    )
    # Jina API Key（可选，不设置则使用免费额度）
    jina_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv('JINA_API_KEY', '')
    )
    # InfoQuest 抓取超时（秒）
    infoquest_timeout: int = field(
        default_factory=lambda: int(os.getenv('INFOQUEST_FETCH_TIMEOUT', '-1'))
    )
    # InfoQuest 页面加载后等待时间（秒）
    infoquest_fetch_time: int = field(
        default_factory=lambda: int(os.getenv('INFOQUEST_FETCH_TIME', '-1'))
    )
    # InfoQuest 导航超时（秒）
    infoquest_navigation_timeout: int = field(
        default_factory=lambda: int(os.getenv('INFOQUEST_NAVIGATION_TIMEOUT', '-1'))
    )


@dataclass
class WorkflowConfig:
    """工作流配置"""
    default_user_id: str = "default_user"
    default_session_id: str = "default_session"
    default_latitude: str = "34.348035"
    default_longitude: str = "108.930677"
    default_timeout: int = 300
    max_concurrent_tools: int = field(
        default_factory=lambda: int(os.getenv('MAX_CONCURRENT_TOOLS', '5'))
    )


@dataclass
class TaskDecompositionConfig:
    """任务分解配置"""
    # 任务分解模式: none, todolist
    # none: 不启用任务分解
    # todolist: 使用 LangChain 的 TodoListMiddleware
    mode: str = field(
        default_factory=lambda: os.getenv('TASK_DECOMPOSITION_MODE', 'todolist')
    )
    # 触发任务分解的关键词
    trigger_keywords: list = field(
        default_factory=lambda: os.getenv(
            'TASK_DECOMPOSITION_KEYWORDS',
            '对比,比较,分别,各自,多个,依次'
        ).split(',')
    )


@dataclass
class RedisConfig:
    """Redis 短期记忆配置"""
    host: str = field(
        default_factory=lambda: os.getenv('REDIS_HOST', '')
    )
    port: int = field(
        default_factory=lambda: int(os.getenv('REDIS_PORT', '6379'))
    )
    db: int = field(
        default_factory=lambda: int(os.getenv('REDIS_DB', '0'))
    )
    password: Optional[str] = field(
        default_factory=lambda: os.getenv('REDIS_PASSWORD')
    )
    ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv('REDIS_TTL_SECONDS', '3600'))
    )
    max_messages: int = field(
        default_factory=lambda: int(os.getenv('REDIS_MAX_MESSAGES', '100'))
    )
    key_prefix: str = field(
        default_factory=lambda: os.getenv('REDIS_KEY_PREFIX', 'muye_agent_main:session:')
    )
    # Memory 提取跟踪配置
    tracker_ttl_hours: int = field(
        default_factory=lambda: int(os.getenv('REDIS_TRACKER_TTL_HOURS', '24'))
    )
    tracker_key_prefix: str = field(
        default_factory=lambda: os.getenv('REDIS_TRACKER_KEY_PREFIX', 'memory:processed:')
    )


@dataclass
class MongoDBConfig:
    """MongoDB 结构化长期记忆配置"""
    uri: str = field(
        default_factory=lambda: os.getenv('MONGODB_URI', '')
    )
    database: str = field(
        default_factory=lambda: os.getenv('MONGODB_DATABASE', 'user_memory')
    )
    collection_contexts: str = field(
        default_factory=lambda: os.getenv('MONGODB_COLLECTION_CONTEXTS', 'user_contexts')
    )
    collection_facts: str = field(
        default_factory=lambda: os.getenv('MONGODB_COLLECTION_FACTS', 'facts_backup')
    )
    max_pool_size: int = field(
        default_factory=lambda: int(os.getenv('MONGODB_MAX_POOL_SIZE', '10'))
    )


@dataclass
class EvermemConfig:
    """evermemOS 语义长期记忆配置（仅 API 调用）"""
    api_url: str = field(
        default_factory=lambda: os.getenv('EVERMEM_API_URL', '')
    )
    api_key: str = field(
        default_factory=lambda: os.getenv('EVERMEM_API_KEY', '')
    )
    timeout: int = field(
        default_factory=lambda: int(os.getenv('EVERMEM_TIMEOUT', '30'))
    )
    max_retries: int = field(
        default_factory=lambda: int(os.getenv('EVERMEM_MAX_RETRIES', '3'))
    )


@dataclass
class MemoryConfig:
    """三层记忆系统总配置。

    ``enabled`` 决定主 Agent 是否装配记忆中间件；关闭后不会建立任何
    Redis、MongoDB 或 evermemOS 连接。
    """
    enabled: bool = field(
        default_factory=lambda: os.getenv('MEMORY_ENABLED', 'true').lower() == 'true'
    )

    # 各层配置
    redis: RedisConfig = field(default_factory=RedisConfig)
    mongodb: MongoDBConfig = field(default_factory=MongoDBConfig)
    evermem: EvermemConfig = field(default_factory=EvermemConfig)

    # 各层启用开关
    enable_redis: bool = field(
        default_factory=lambda: os.getenv('MEMORY_ENABLE_REDIS', 'true').lower() == 'true'
    )
    enable_mongodb: bool = field(
        default_factory=lambda: os.getenv('MEMORY_ENABLE_MONGODB', 'true').lower() == 'true'
    )
    enable_evermem: bool = field(
        default_factory=lambda: os.getenv('MEMORY_ENABLE_EVERMEM', 'false').lower() == 'true'
    )

    # 记忆提取配置
    extraction_model: str = field(
        default_factory=lambda: os.getenv('MEMORY_EXTRACTION_MODEL', os.getenv('LLM_MODEL', 'qwen-plus'))
    )
    extraction_temperature: float = field(
        default_factory=lambda: float(os.getenv('MEMORY_EXTRACTION_TEMPERATURE', '0.1'))
    )

    # 队列和防抖配置
    debounce_seconds: int = field(
        default_factory=lambda: int(os.getenv('MEMORY_DEBOUNCE_SECONDS', '30'))
    )

    # 并行处理配置
    max_concurrent_tasks: int = field(
        default_factory=lambda: int(os.getenv('MEMORY_MAX_CONCURRENT_TASKS', '10'))
    )

    # 消息累积配置
    max_accumulated_messages: int = field(
        default_factory=lambda: int(os.getenv('MEMORY_MAX_ACCUMULATED_MESSAGES', '100'))
    )

    # 事实管理配置
    fact_confidence_threshold: float = field(
        default_factory=lambda: float(os.getenv('MEMORY_FACT_THRESHOLD', '0.7'))
    )
    max_facts_per_user: int = field(
        default_factory=lambda: int(os.getenv('MEMORY_MAX_FACTS_PER_USER', '100'))
    )

    # 记忆衰减配置
    decay_enabled: bool = field(
        default_factory=lambda: os.getenv('MEMORY_DECAY_ENABLED', 'true').lower() == 'true'
    )
    decay_days_threshold: int = field(
        default_factory=lambda: int(os.getenv('MEMORY_DECAY_DAYS_THRESHOLD', '30'))
    )
    decay_rate: float = field(
        default_factory=lambda: float(os.getenv('MEMORY_DECAY_RATE', '0.1'))
    )

    # 记忆注入配置
    injection_enabled: bool = field(
        default_factory=lambda: os.getenv('MEMORY_INJECTION_ENABLED', 'true').lower() == 'true'
    )
    max_injection_tokens: int = field(
        default_factory=lambda: int(os.getenv('MEMORY_MAX_INJECTION_TOKENS', '2000'))
    )

    # 优先级权重配置
    priority_high_weight: float = field(
        default_factory=lambda: float(os.getenv('MEMORY_PRIORITY_HIGH_WEIGHT', '0.5'))
    )
    priority_medium_weight: float = field(
        default_factory=lambda: float(os.getenv('MEMORY_PRIORITY_MEDIUM_WEIGHT', '0.3'))
    )
    priority_low_weight: float = field(
        default_factory=lambda: float(os.getenv('MEMORY_PRIORITY_LOW_WEIGHT', '0.2'))
    )


@dataclass
class LoggerConfig:
    """日志配置"""
    log_file: str = field(
        default_factory=lambda: os.getenv('LOG_FILE', './logs/muye_agent.log')
    )
    level: str = field(
        default_factory=lambda: os.getenv('LOG_LEVEL', 'INFO')
    )
    backup_count: int = field(
        default_factory=lambda: int(os.getenv('LOG_BACKUP_COUNT', '7'))
    )
    use_rotating: bool = field(
        default_factory=lambda: os.getenv('LOG_USE_ROTATING', 'false').lower() == 'true'
    )
    use_timed_rotating: bool = field(
        default_factory=lambda: os.getenv('LOG_USE_TIMED_ROTATING', 'true').lower() == 'true'
    )
    max_bytes: int = field(
        default_factory=lambda: int(os.getenv('LOG_MAX_BYTES', str(100 * 1024 * 1024)))
    )


@dataclass
class ProfilingConfig:
    """性能统计配置"""
    enabled: bool = field(
        default_factory=lambda: os.getenv('ENABLE_PROFILING', 'true').lower() == 'true'
    )
    slow_threshold_ms: int = field(
        default_factory=lambda: int(os.getenv('PROFILING_SLOW_THRESHOLD_MS', '1000'))
    )


@dataclass
class Config:
    """全局配置"""
    server: ServerConfig = field(default_factory=ServerConfig)
    api: APIConfig = field(default_factory=APIConfig)
    http_pool: HTTPPoolConfig = field(default_factory=HTTPPoolConfig)
    middleware: MiddlewareConfig = field(default_factory=MiddlewareConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)
    checkpointer: CheckpointerConfig = field(default_factory=CheckpointerConfig)
    compression: CompressionConfig = field(default_factory=CompressionConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)
    web_fetch: WebFetchConfig = field(default_factory=WebFetchConfig)
    task_decomposition: TaskDecompositionConfig = field(default_factory=TaskDecompositionConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    logger: LoggerConfig = field(default_factory=LoggerConfig)
    profiling: ProfilingConfig = field(default_factory=ProfilingConfig)
    content_processing: ContentProcessingConfig = field(default_factory=ContentProcessingConfig)
    infoquest: InfoQuestConfig = field(default_factory=InfoQuestConfig)


# 全局配置实例
_config: Optional[Config] = None


def validate_config(config: Config) -> None:
    """验证必需的配置项"""
    errors = []

    # 验证 LLM 配置
    if not config.llm.api_base:
        errors.append("MUYE_LLM_BASE_URL 未配置（必需）")
    elif not config.llm.api_base.startswith(("http://", "https://")):
        errors.append("MUYE_LLM_BASE_URL 必须以 http:// 或 https:// 开头")

    # 验证数据库配置（如果使用 PostgreSQL）
    if config.checkpointer.backend == 'postgres' and not config.checkpointer.postgresql_uri:
        errors.append("CHECKPOINTER_POSTGRESQL_URI 未配置（使用 PostgreSQL 后端时必需）")

    # 记忆中间件关闭时，所有记忆后端配置均不应阻止主 Agent 启动。
    # 启用时，长期记忆更新流程需要 MongoDB 保存结构化上下文。
    if config.memory.enabled and not config.memory.enable_mongodb:
        errors.append("MEMORY_ENABLED=true 时必须启用 MEMORY_ENABLE_MONGODB")
    elif config.memory.enabled and not config.memory.mongodb.uri:
        errors.append("MONGODB_URI 未配置（MEMORY_ENABLED=true 时必需）")

    if errors:
        error_msg = "配置验证失败:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)


def get_config() -> Config:
    """获取全局配置实例（单例模式）"""
    global _config
    if _config is None:
        _config = Config()
        validate_config(_config)
    return _config


def reload_config() -> Config:
    """重新加载配置（用于测试或配置更新）"""
    global _config
    _config = Config()
    validate_config(_config)
    return _config
