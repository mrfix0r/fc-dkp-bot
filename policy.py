"""Authorization rules independent of Discord transport."""
from store import RuleError


def authorize(*, guild_id, expected_guild_id, is_human_member, is_admin,
              role_ids, channel_id, settings, officer=False, admin=False, require_setup=True):
    if guild_id != expected_guild_id or not is_human_member:
        raise RuleError("Бот работает только на настроенном Discord-сервере, для участников-людей.")
    if admin and not is_admin:
        raise RuleError("Настройки доступны только администратору сервера.")
    if not require_setup:
        return
    if not settings:
        raise RuleError("Администратор должен сначала выполнить /dkp setup.")
    is_officer = is_admin or settings["officer_role_id"] in role_ids
    if officer and not is_officer:
        raise RuleError("Это действие доступно только ДКП-офицеру или администратору.")
    if not is_officer and settings["member_role_id"] and settings["member_role_id"] not in role_ids:
        raise RuleError("Для ДКП нужна настроенная роль участника гильдии.")
    if channel_id != settings["channel_id"]:
        raise RuleError(f"Используйте ДКП-канал <#{settings['channel_id']}>.")
