from aiogram import Router

from app.bot.handlers import (
    admin,
    answer,
    limits,
    progress,
    settings,
    start,
    subscription,
    voice,
)


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    root.include_router(answer.router)
    root.include_router(limits.router)
    # Раньше разговорного и раньше ожидания почты: нажатие на кнопку нижнего
    # меню — это команда, а не реплика в разговоре и не адрес для счёта.
    root.include_router(settings.router)
    root.include_router(progress.router)
    # Раньше подписки и разговора: пока админ набирает текст рассылки, его
    # сообщение — это текст рассылки, а не реплика в диалоге.
    root.include_router(admin.router)
    # Раньше разговорного: пока ждём почту для оплаты, текст — это адрес, а не
    # реплика. Всё остальное этот роутер пропускает дальше сам.
    root.include_router(subscription.router)
    # voice последним: у него есть перехватчик всех прочих сообщений.
    root.include_router(voice.router)
    return root
