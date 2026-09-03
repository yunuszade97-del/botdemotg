from aiogram import Router

from app.bot.handlers import commands, dialog, errors


def build_router() -> Router:
    """Собирает роутеры. Порядок важен: команды идут до catch-all диалога."""
    root = Router(name="root")
    root.include_router(errors.build_router())
    root.include_router(commands.build_router())
    root.include_router(dialog.build_router())
    return root


__all__ = ["build_router"]
