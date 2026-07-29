import logging
import os
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

_global_logger = None

# 10 MiB per file, 5 rotated copies kept.
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5

# ANSI color codes for console output
RESET = "\033[0m"

COLORS = {
    logging.DEBUG: "\033[36m",  # Cyan
    logging.INFO: "\033[32m",  # Green
    logging.WARNING: "\033[33m",  # Yellow
    logging.ERROR: "\033[31m",  # Red
    logging.CRITICAL: "\033[41m",  # Red background
}

TIME_COLOR = "\033[35m"  # Magenta
FUNC_COLOR = "\033[34m"  # Blue
MSG_COLOR = "\033[37m"  # White/Gray


class ColorFormatter(logging.Formatter):
    """Formatter for colored stdout logs."""

    def format(self, record):
        orig_levelname = record.levelname
        orig_msg = record.msg
        orig_args = record.args
        orig_func_info = getattr(record, "func_info", None)
        orig_message = getattr(record, "message", None)

        try:
            level_color = COLORS.get(record.levelno, RESET)
            record.levelname = f"{level_color}{record.levelname}{RESET}"

            cwd = os.getcwd()
            rel_path = os.path.relpath(record.pathname, cwd)
            record.func_info = (
                f"{FUNC_COLOR}{rel_path}:{record.lineno}:{record.funcName}{RESET}"
            )

            record.msg = f"{MSG_COLOR}{record.getMessage()}{RESET}"
            record.args = None

            formatted = super().format(record)

            # Color timestamp
            parts = formatted.split(" - ", 1)
            if len(parts) == 2:
                timestamp, rest = parts
                formatted = f"{TIME_COLOR}{timestamp}{RESET} - {rest}"

            return formatted
        finally:
            record.levelname = orig_levelname
            record.msg = orig_msg
            record.args = orig_args
            if orig_func_info is not None:
                record.func_info = orig_func_info
            elif hasattr(record, "func_info"):
                del record.func_info
            if orig_message is not None:
                record.message = orig_message
            elif hasattr(record, "message"):
                del record.message


class PlainFormatter(logging.Formatter):
    """Formatter for plain file logs (no color)."""

    def format(self, record):
        orig_func_info = getattr(record, "func_info", None)
        try:
            cwd = os.getcwd()
            rel_path = os.path.relpath(record.pathname, cwd)
            record.func_info = f"{rel_path}:{record.lineno}:{record.funcName}"
            return super().format(record)
        finally:
            if orig_func_info is not None:
                record.func_info = orig_func_info
            elif hasattr(record, "func_info"):
                del record.func_info


def _env_int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def resolve_log_dir():
    """Directory the rotating log file lives in.

    Resolved without importing config, which would be circular: config imports
    this module before it has finished setting up the app directory. The lookup
    mirrors ``env.initialize_app_env`` closely enough for both the Docker case
    (``/config``) and a plain local install (``~/.aniworld``).
    """
    explicit = os.getenv("ANIWORLD_LOG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    install_folder = os.getenv("ANIWORLD_INSTALL_FOLDER", "").strip() or ".aniworld"
    app_dir = Path(install_folder).expanduser()
    if not app_dir.is_absolute():
        app_dir = Path.home() / app_dir
    return app_dir / "logs"


def get_log_file_path():
    """Absolute path of the active log file, or None when logging to file failed."""
    return _log_file_path


def _build_file_handler(formatter):
    """Rotating file handler, falling back to the temp dir if the app dir is unusable."""
    candidates = [resolve_log_dir(), Path(tempfile.gettempdir())]
    max_bytes = _env_int("ANIWORLD_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES)
    backup_count = _env_int("ANIWORLD_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT)

    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "aniworld.log"
            # mode="a": a 24/7 container restarts often and truncating on every
            # start would throw away exactly the logs explaining the restart.
            handler = RotatingFileHandler(
                path,
                mode="a",
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            handler.setFormatter(formatter)
            return handler, path
        except OSError:
            continue

    return None, None


_log_file_path = None


def get_logger(name=__name__, level=None):
    """Return a logger that writes to both file and stdout, colored in console."""
    global _global_logger, _log_file_path
    if _global_logger is None:
        _global_logger = logging.getLogger("aniworld")
        _global_logger.handlers.clear()

        log_format = "%(asctime)s - %(levelname)s - %(func_info)s - %(message)s"
        date_format = "%Y-%m-%d %H:%M:%S"

        # ------------------ File handler ------------------ #
        file_handler, _log_file_path = _build_file_handler(
            PlainFormatter(log_format, datefmt=date_format)
        )
        if file_handler is not None:
            _global_logger.addHandler(file_handler)

        # ------------------ Console handler ------------------ #
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ColorFormatter(log_format, datefmt=date_format))
        _global_logger.addHandler(console_handler)

        # Reduce noise from urllib3
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
        logging.getLogger("waitress.queue").setLevel(logging.ERROR)

    # Re-evaluated on every call: the debug flag is often only pushed into the
    # environment by argument parsing, long after the first get_logger().
    if level is None:
        level = (
            logging.DEBUG
            if os.getenv("ANIWORLD_DEBUG_MODE", "0") == "1"
            else logging.WARNING
        )
    _global_logger.setLevel(level)

    return _global_logger
