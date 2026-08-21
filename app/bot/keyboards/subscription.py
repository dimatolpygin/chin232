"""Кнопки раздела «Подписка»."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.texts import ru
from app.db.models import Plan

SUB_PREFIX = "sub"
# Начать оплату: выставить счёт и отдать ссылку.
SUB_PAY = "pay"


def parse_subscription_action(data: str) -> tuple[str, str] | None:
    """Разобрать `sub:pay:<код тарифа>`. Код может отсутствовать у старых кнопок.

    Старые сообщения с кнопкой без кода живут в переписке вечно, и нажатие на
    такую кнопку не должно молча ничего не делать: пустой код означает тариф
    по умолчанию, а не ошибку.
    """
    parts = (data or "").split(":")
    if len(parts) < 2 or parts[0] != SUB_PREFIX:
        return None
    return parts[1], parts[2] if len(parts) > 2 else ""


def _button(plan: Plan, price: str, currency: str) -> InlineKeyboardButton:
    """Кнопка тарифа: цена и способ прямо на ней.

    Цена на кнопке, а не только в тексте: человек не должен нажимать
    «оплатить», не понимая, сколько с него спишут и как именно.
    """
    template = ru.BTN_PAY_CARD if plan.autorenew else ru.BTN_PAY_SBP
    return InlineKeyboardButton(
        text=template.format(price=price, currency=currency, days=plan.duration_days),
        callback_data=f"{SUB_PREFIX}:{SUB_PAY}:{plan.code}",
    )


def offer_keyboard(plans: list[tuple[Plan, str, str]]) -> InlineKeyboardMarkup:
    """Экран предложения: по кнопке на каждый доступный способ оплаты."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(plan, price, currency)] for plan, price, currency in plans]
    )


def payment_keyboard(url: str) -> InlineKeyboardMarkup:
    """Ссылка на виджет оплаты. Способ вшит в ссылку ещё при создании счёта."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=ru.BTN_PAY_OPEN, url=url)]]
    )
