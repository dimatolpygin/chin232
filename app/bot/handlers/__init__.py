from aiogram import Router

from app.bot.handlers import answer, limits, start, subscription, voice


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(answer.router)
    root.include_router(limits.router)
    # Раньше разговорного: пока ждём почту для оплаты, текст — это адрес, а не
    # реплика. Всё остальное этот роутер пропускает дальше сам.
    root.include_router(subscription.router)
    # voice последним: у него есть перехватчик всех прочих сообщений.
    root.include_router(voice.router)
    return root
