from aiogram import Router

from app.bot.handlers import answer, limits, start, voice


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(answer.router)
    root.include_router(limits.router)
    # voice последним: у него есть перехватчик всех прочих сообщений.
    root.include_router(voice.router)
    return root
