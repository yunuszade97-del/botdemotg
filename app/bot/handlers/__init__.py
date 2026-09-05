from aiogram import Router

from app.bot.handlers import commands, dialog, errors, niche


def build_router(*, showcase_enabled: bool = False) -> Router:
    """Собирает роутеры. Порядок важен: errors → commands → niche → dialog,
    catch-all диалога строго последним.

    Роутер ниш подключается только при включённой витрине: иначе в дереве
    появится callback_query-хэндлер, который меняет `allowed_updates` даже
    для клиента без витрины.
    """
    root = Router(name="root")
    root.include_router(errors.build_router())
    root.include_router(commands.build_router())
    if showcase_enabled:
        root.include_router(niche.build_router())
    root.include_router(dialog.build_router())
    return root


__all__ = ["build_router"]
