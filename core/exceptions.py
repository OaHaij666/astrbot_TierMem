class MemoryError(Exception):
    """记忆插件基础异常"""

    pass


class ValidationError(MemoryError):
    """校验失败异常"""

    pass


class MigrationError(MemoryError):
    """模式迁移异常"""

    pass


class SummaryError(MemoryError):
    """总结流程异常"""

    pass


class ProviderNotFoundError(MemoryError):
    """指定的 LLM Provider 不存在"""

    pass
