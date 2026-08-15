"""Кнопки раздела «Подписка»."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import ru

SUB_PREFIX = "sub"
# Начать оплату: выставить счёт и отдать ссылку.
SUB_PAY = "pay"


def parse_subscription_action(data: str) -> str | None:
    parts = (data or "").split(":")
    if len(parts) != 2 or parts[0] != SUB_PREFIX:
        return None
    return parts[1]


def offer_keyboard(price: str, currency: str) -> InlineKeyboardMarkup:
    """Экран предложения: одна кнопка, и на ней сразу видна цена.

    Цена на кнопке, а не только в тексте: человек не должен нажимать «оплатить»,
    не понимая, сколько с него спишут.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ru.BTN_PAY.format(price=price, currency=currency),
                    callback_data=f"{SUB_PREFIX}:{SUB_PAY}",
                )
            ]
        ]
    )


def payment_keyboard(url: str) -> InlineKeyboardMarkup:
    """Ссылка на виджет оплаты. Способ (карта или СБП) юзер выбирает уже там."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ru.BTN_PAY_OPEN, url=url)]]
    )
