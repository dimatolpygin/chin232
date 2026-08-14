from aiogram import Router

from app.bot.handlers import start


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    return root
