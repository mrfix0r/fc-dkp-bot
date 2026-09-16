"""Task Scheduler entry point: hidden Python, rotating logs, meaningful exit codes."""
from contextlib import redirect_stderr, redirect_stdout
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent


class LogStream:
    encoding = "utf-8"

    def __init__(self, logger, level):
        self.logger, self.level = logger, level

    def write(self, value):
        for line in value.splitlines():
            if line.strip():
                self.logger.log(self.level, line)
        return len(value)

    def flush(self):
        for handler in self.logger.handlers:
            handler.flush()

    def isatty(self):
        return False


def preflight():
    """Validate the installation and reject a second process before enabling the task."""
    try:
        from bot import load_config
        from runtime import InstanceLock
        _, _, directory = load_config()
        directory.mkdir(parents=True, exist_ok=True)
        with InstanceLock(directory / "bot.lock"):
            pass
    except RuntimeError:
        print("Bot is already running. Stop start.bat or the background task first.")
        return 1
    except Exception as error:
        print(f"Preflight failed ({type(error).__name__}). Run install.bat and configure.bat first.")
        return 1
    print("Installation and configuration OK. No other bot process holds this database.")
    return 0


def run():
    logs = ROOT / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("fc_dkp.background")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(logs / "background.log", maxBytes=1000000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    old_stdin = sys.stdin
    try:
        # pythonw may have no standard streams; set them before importing discord.py.
        with open(os.devnull, "r", encoding="utf-8") as stdin, \
                redirect_stdout(LogStream(logger, logging.INFO)), \
                redirect_stderr(LogStream(logger, logging.ERROR)):
            sys.stdin = stdin
            logger.info("Starting FC DKP in background. PID: %s; account: %s; executable: %s",
                        os.getpid(), os.environ.get("USERNAME", "unknown"), sys.executable)
            try:
                from bot import main
                code = main()
            except Exception as error:
                # Never write HTTP response bodies, tokens, or a full traceback here.
                logger.error("Background startup failed: %s", type(error).__name__)
                code = 1
            logger.info("Background process stopped. Exit code: %s", code)
            return code
    finally:
        sys.stdin = old_stdin
        logger.removeHandler(handler)
        handler.close()


if __name__ == "__main__":
    sys.exit(preflight() if sys.argv[1:] == ["--check"] else run())
