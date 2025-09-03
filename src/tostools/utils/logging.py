"""
Advanced logging utilities for tostools.

This module provides a comprehensive, centralized logging system that supports:
- Multiple output destinations (console, files)
- Level-based file separation
- Structured logging for both humans and machines
- Centralized configuration management
- Per-module customization while maintaining consistency
"""

import json
import logging
import logging.config
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

# Global logging configuration
_logging_initialized = False
_logging_lock = threading.Lock()
_default_config = None


class StructuredFormatter(logging.Formatter):
    """
    Formatter that can output both human-readable and structured (JSON) logs.
    """

    def __init__(self, format_type: str = "human", include_extra: bool = True):
        """
        Initialize the formatter.

        Args:
            format_type: "human" for readable format, "json" for structured format
            include_extra: Whether to include extra fields in structured output
        """
        self.format_type = format_type
        self.include_extra = include_extra

        if format_type == "human":
            # Human-readable format with timestamp, module, function, level, and message
            fmt = "%(asctime)s | %(name)-20s | %(funcName)-15s | %(levelname)-7s | %(message)s"
            super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")
        else:
            # JSON format doesn't need a format string
            super().__init__()

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record."""
        if self.format_type == "human":
            return super().format(record)
        else:
            return self._format_json(record)

    def _format_json(self, record: logging.LogRecord) -> str:
        """Format record as JSON for structured logging."""
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # Add extra fields if requested
        if self.include_extra:
            extra_fields = {}
            for key, value in record.__dict__.items():
                if key not in {
                    "name",
                    "msg",
                    "args",
                    "levelname",
                    "levelno",
                    "pathname",
                    "filename",
                    "module",
                    "lineno",
                    "funcName",
                    "created",
                    "msecs",
                    "relativeCreated",
                    "thread",
                    "threadName",
                    "processName",
                    "process",
                    "getMessage",
                    "exc_info",
                    "exc_text",
                    "stack_info",
                }:
                    extra_fields[key] = value

            if extra_fields:
                log_data["extra"] = extra_fields

        return json.dumps(log_data)


class LoggingConfig:
    """
    Centralized logging configuration for tostools.
    """

    def __init__(
        self,
        console_level: int = logging.INFO,
        file_level: int = logging.DEBUG,
        log_dir: Optional[Union[str, Path]] = None,
        console_format: str = "human",
        file_format: str = "human",
        structured_file: bool = True,
        separate_levels: bool = True,
        max_file_size: int = 10 * 1024 * 1024,  # 10MB
        backup_count: int = 5,
    ):
        """
        Initialize logging configuration.

        Args:
            console_level: Minimum level for console output
            file_level: Minimum level for file output
            log_dir: Directory for log files (None = no file logging)
            console_format: Format for console output ("human" or "json")
            file_format: Format for regular log files ("human" or "json")
            structured_file: Whether to create separate structured JSON log file
            separate_levels: Whether to create separate files per level
            max_file_size: Maximum size per log file before rotation
            backup_count: Number of backup files to keep
        """
        self.console_level = console_level
        self.file_level = file_level
        self.log_dir = Path(log_dir) if log_dir else None
        self.console_format = console_format
        self.file_format = file_format
        self.structured_file = structured_file
        self.separate_levels = separate_levels
        self.max_file_size = max_file_size
        self.backup_count = backup_count

        # Create log directory if needed
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)


def configure_logging(
    config: Optional[LoggingConfig] = None, force_reconfigure: bool = False
) -> None:
    """
    Configure the global logging system for tostools.

    Args:
        config: Logging configuration. If None, uses default configuration.
        force_reconfigure: Force reconfiguration even if already initialized
    """
    global _logging_initialized, _default_config

    with _logging_lock:
        if _logging_initialized and not force_reconfigure:
            return

        if config is None:
            config = LoggingConfig()

        _default_config = config

        # Clear any existing configuration
        logging.getLogger().handlers.clear()

        # Build logging configuration dictionary
        log_config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "human_console": {
                    "()": StructuredFormatter,
                    "format_type": config.console_format,
                },
                "human_file": {
                    "()": StructuredFormatter,
                    "format_type": config.file_format,
                },
                "structured": {
                    "()": StructuredFormatter,
                    "format_type": "json",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "level": config.console_level,
                    "formatter": "human_console",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {
                "level": (
                    config.console_level if not config.log_dir else logging.DEBUG
                ),  # Console-only: match console level; File logging: use DEBUG
                "handlers": ["console"],
            },
            "loggers": {
                "tostools": {
                    "level": logging.DEBUG,
                    "propagate": True,
                },
                # Suppress verbose third-party library logging on console
                "urllib3.connectionpool": {
                    "level": logging.WARNING,
                    "propagate": True,
                },
                "requests.packages.urllib3": {
                    "level": logging.WARNING,
                    "propagate": True,
                },
            },
        }

        # Add file handlers if log directory is specified
        if config.log_dir:
            # Ensure log directory exists
            config.log_dir.mkdir(parents=True, exist_ok=True)
            handlers = ["console"]

            # Main log file
            main_log_file = config.log_dir / "tostools.log"
            log_config["handlers"]["main_file"] = {
                "class": "logging.handlers.RotatingFileHandler",
                "level": config.file_level,
                "formatter": "human_file",
                "filename": str(main_log_file),
                "maxBytes": config.max_file_size,
                "backupCount": config.backup_count,
                "encoding": "utf-8",
            }
            handlers.append("main_file")

            # Structured JSON log file (for programmatic analysis)
            if config.structured_file:
                structured_log_file = config.log_dir / "tostools_structured.jsonl"
                log_config["handlers"]["structured_file"] = {
                    "class": "logging.handlers.RotatingFileHandler",
                    "level": config.file_level,
                    "formatter": "structured",
                    "filename": str(structured_log_file),
                    "maxBytes": config.max_file_size,
                    "backupCount": config.backup_count,
                    "encoding": "utf-8",
                }
                handlers.append("structured_file")

            # Separate files per level
            if config.separate_levels:
                for level_name, level_num in [
                    ("error", logging.ERROR),
                    ("warning", logging.WARNING),
                    ("info", logging.INFO),
                    ("debug", logging.DEBUG),
                ]:
                    level_file = config.log_dir / f"tostools_{level_name}.log"
                    handler_name = f"{level_name}_file"

                    log_config["handlers"][handler_name] = {
                        "class": "logging.handlers.RotatingFileHandler",
                        "level": level_num,
                        "formatter": "human_file",
                        "filename": str(level_file),
                        "maxBytes": config.max_file_size
                        // 2,  # Smaller files for level-specific logs
                        "backupCount": config.backup_count,
                        "encoding": "utf-8",
                        "filters": [f"level_filter_{level_name}"],
                    }
                    handlers.append(handler_name)

                # Add level filters
                log_config["filters"] = {}
                for level_name, level_num in [
                    ("error", logging.ERROR),
                    ("warning", logging.WARNING),
                    ("info", logging.INFO),
                    ("debug", logging.DEBUG),
                ]:
                    log_config["filters"][f"level_filter_{level_name}"] = {
                        "()": LevelFilter,
                        "level": level_num,
                    }

            # Update root logger handlers
            log_config["root"]["handlers"] = handlers

        # Apply configuration
        logging.config.dictConfig(log_config)
        _logging_initialized = True


class LevelFilter:
    """Filter to only allow specific log levels."""

    def __init__(self, level: int):
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno == self.level


def get_logger(
    name: str = __name__,
    level: Optional[int] = None,
    extra_context: Optional[Dict[str, Any]] = None,
) -> logging.Logger:
    """
    Get a logger for tostools modules with consistent configuration.

    Args:
        name: Logger name (typically __name__)
        level: Override log level for this specific logger
        extra_context: Additional context to include in all log messages

    Returns:
        Configured logger instance
    """
    global _logging_initialized, _default_config

    # Initialize logging if not already done
    if not _logging_initialized:
        configure_logging()

    # Get logger
    logger = logging.getLogger(name)

    # Set specific level if provided, but only if no centralized logging is configured
    # This prevents individual functions from overriding centralized log level control
    if level is not None and not _logging_initialized:
        logger.setLevel(level)

    # Add extra context if provided
    if extra_context:
        logger = LoggerAdapter(logger, extra_context)

    return logger


class LoggerAdapter(logging.LoggerAdapter):
    """
    Logger adapter that adds extra context to log messages.
    """

    def process(self, msg: Any, kwargs: Dict[str, Any]) -> tuple[Any, Dict[str, Any]]:
        """Process the logging call by adding extra context."""
        if "extra" in kwargs:
            kwargs["extra"].update(self.extra)
        else:
            kwargs["extra"] = self.extra.copy()
        return msg, kwargs


# Convenience functions for different logging scenarios
def setup_console_logging(level: int = logging.INFO) -> None:
    """Quick setup for console-only logging."""
    configure_logging(
        LoggingConfig(console_level=level, log_dir=None), force_reconfigure=True
    )


def setup_file_logging(
    log_dir: Union[str, Path],
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> None:
    """Quick setup for file + console logging."""
    configure_logging(
        LoggingConfig(
            console_level=console_level, file_level=file_level, log_dir=log_dir
        ),
        force_reconfigure=True,
    )


def setup_development_logging(log_dir: Optional[Union[str, Path]] = "logs") -> None:
    """Setup logging optimized for development work."""
    configure_logging(
        LoggingConfig(
            console_level=logging.INFO,
            file_level=logging.DEBUG,
            log_dir=log_dir,
            console_format="human",
            file_format="human",
            structured_file=True,
            separate_levels=True,
        ),
        force_reconfigure=True,
    )


def setup_production_logging(log_dir: Union[str, Path] = "/var/log/tostools") -> None:
    """Setup logging optimized for production deployment."""
    configure_logging(
        LoggingConfig(
            console_level=logging.WARNING,
            file_level=logging.INFO,
            log_dir=log_dir,
            console_format="human",
            file_format="json",
            structured_file=True,
            separate_levels=True,
            max_file_size=50 * 1024 * 1024,  # 50MB
            backup_count=10,
        )
    )


# Legacy compatibility
def get_tostools_logger(
    name: str = __name__, loglevel: int = logging.WARNING
) -> logging.Logger:
    """
    Legacy compatibility function.

    Args:
        name: Logger name
        loglevel: Log level (renamed from inconsistent parameter names)

    Returns:
        Logger instance
    """
    return get_logger(name, loglevel)
