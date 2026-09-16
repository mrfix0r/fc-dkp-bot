"""Local setup: credentials are read without echo and never sent anywhere here."""
from getpass import getpass
from pathlib import Path
import os
import re
import sys

ROOT = Path(__file__).resolve().parent


def main():
    target = ROOT / ".env"
    print("FC DKP — локальная настройка\nТокен вводится скрыто. Он не будет показан на экране.")
    if target.exists() and input("Файл .env уже есть. Заменить настройки? [yes/нет]: ").strip().lower() != "yes":
        print("Настройки оставлены без изменений.")
        return 0
    token = getpass("Bot Token (Developer Portal → Bot → Token): ").strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9._-]+", token):
        print("Некорректный формат токена. Вставьте токен бота без кавычек и пробелов.")
        return 1
    guild_id = input("ID Discord-сервера: ").strip()
    if not guild_id.isdecimal() or not 1 <= int(guild_id) < 2**63:
        print("ID должен быть положительным целым числом (скопируйте ID сервера в Discord).")
        return 1
    # Write atomically, with restrictive permissions on POSIX. Windows inherits folder ACLs.
    temporary = ROOT / ".env.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
        out.write(f"DISCORD_TOKEN={token}\nGUILD_ID={guild_id}\nDATA_DIR=data\n")
    os.replace(temporary, target)
    print("Сохранено в .env. Теперь запустите start.bat.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (EOFError, KeyboardInterrupt):
        print("\nНастройка отменена.")
        sys.exit(1)
