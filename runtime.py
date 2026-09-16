"""Cross-platform process lock, released by the OS even after a crash."""
import os


class InstanceLock:
    def __init__(self, path):
        self.path, self.file = path, None

    def __enter__(self):
        self.file = open(self.path, "a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise RuntimeError("Бот с этой базой уже запущен. Остановите текущий экземпляр: окно start.bat или автозапуск.") from error
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
