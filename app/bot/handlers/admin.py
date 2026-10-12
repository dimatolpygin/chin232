"""Админка: `/admin` для владельца, всем остальным её не существует.

Хендлер только разбирает нажатия и рисует экраны. Числа считает
`app/core/services/admin.py`, статистику — `stats.py`, рассылку выполняет
воркер: тысяча сообщений в обработчике апдейта заблокировала бы бота целиком.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards.admin import (
    SECTION_ADMINS,
    SECTION_BROADCAST,
    SECTION_CANCEL,
    SECTION_KNOB,
    SECTION_LIMITS,
    SECTION_LOAD,
    SECTION_NOOP,
    SECTION_PARTNER,
    SECTION_PARTNERS,
    SECTION_PLAN,
    SECTION_PRICE,
    SECTION_SEND,
    SECTION_SPEND,
    SECTION_STATS,
    admin_menu,
    admins_keyboard,
    confirm_keyboard,
    knob_keyboard,
    limits_keyboard,
    load_keyboard,
    parse_admin_action,
    partner_keyboard,
    partners_keyboard,
    plan_keyboard,
    price_keyboard,
    segments_keyboard,
    spend_keyboard,
)
from app.bot.render import (
    esc,
    plural,
    render_limits,
    render_load,
    render_partner_report,
    render_partners,
    render_price,
    render_spending,
    render_stats,
)
from app.bot.texts import ru
from app.core.events import track
from app.core.services.admin import (
    KNOBS_BY_KEY,
    SEGMENT_TITLES,
    add_admin,
    admin_ids,
    admin_names,
    audience_size,
    change_limit,
    change_price,
    config_admins,
    current_value,
    editable_plans,
    is_admin,
    remove_admin,
    resolve_telegram_id,
)
from app.core.services.backup import backup_status
from app.core.services.limits import get_limits
from app.core.services.load import load_report
from app.core.services.partners import (
    PERIOD_ALL,
    PERIODS,
    PartnerError,
    build_report,
    find_partner,
    list_partners,
    normalize_code,
    partner_link,
    promos_by_partner,
    save_partner,
    save_promo,
    set_active,
    set_promo_active,
)
from app.core.services.stats import load_spending, load_summary
from app.db.models import User
from app.logging import get_logger

router = Router(name="admin")
log = get_logger("bot")

# Потолок Telegram на одно сообщение. Проверяем до постановки в очередь: узнать
# о том, что текст не влез, из отчёта о неудавшейся рассылке — поздно.
MAX_BROADCAST_LEN = 4096


def _people(count: int) -> str:
    return plural(count, "человеку", "людям", "людям")


class Broadcast(StatesGroup):
    """Ожидание текста рассылки. Состояние только у админа: попасть в него
    больше неоткуда."""

    text = State()


class AdminFilter(Filter):
    """Пропускает только админов.

    Сессию фильтр получает из данных апдейта — её кладёт та же middleware, что
    и пользователя. Без базы обойтись нельзя: часть админов выдана командой, а
    не прописана в конфиге.
    """

    async def __call__(self, event: TelegramObject, session: AsyncSession) -> bool:
        who = getattr(event, "from_user", None)
        return await is_admin(session, who.id if who else None)


# Кнопки админки чужому не отвечают вовсе: callback_data подобрать несложно.
router.callback_query.filter(AdminFilter())


async def _redraw(callback: CallbackQuery, text: str, markup) -> None:
    """Перерисовать экран на месте: админка живёт одним сообщением."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        log.debug("экран админки не изменился", причина=str(exc))


async def _pulse(session: AsyncSession) -> str:
    summary = await load_summary(session)
    return "\n\n".join(
        [
            ru.ADMIN_HEADER,
            ru.ADMIN_PULSE.format(
                users=summary.users_total,
                paying=summary.paying,
                active=summary.active_today,
            ),
        ]
    )


@router.message(Command("admin"), AdminFilter())
async def cmd_admin(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(await _pulse(session), reply_markup=admin_menu())
    await track(session, "admin_opened", user_id=user.id)
    log.info("открыта админка", user_id=str(user.id), telegram_id=message.from_user.id)


@router.message(Command("admin"))
async def cmd_admin_denied(message: Message, session: AsyncSession, user: User) -> None:
    """Чужому `/admin` бот не отвечает ничего.

    Молчание намеренное: любой ответ, даже «нет доступа», подтверждает, что
    админка существует. Ловим команду здесь, а не отдаём разговорному
    роутеру, — иначе тот вежливо сообщит, что не понял формат.
    """
    await track(session, "admin_denied", user_id=user.id, telegram_id=message.from_user.id)
    log.warning(
        "попытка войти в админку",
        user_id=str(user.id),
        telegram_id=message.from_user.id,
        username=f"@{message.from_user.username or '—'}",
    )


async def _show_admins(callback: CallbackQuery, session: AsyncSession) -> None:
    await _redraw(callback, await _admins_text(session), admins_keyboard())


async def _admins_text(session: AsyncSession) -> str:
    ids = await admin_ids(session)
    имена = await admin_names(session, ids)
    из_конфига = config_admins()
    строки = [
        ru.ADMIN_ADMINS_ROW.format(
            who=f"{имена[i]} ({i})" if i in имена else str(i),
            fixed=ru.ADMIN_ADMINS_FIXED if i in из_конфига else "",
        )
        for i in sorted(ids)
    ]
    return ru.ADMIN_ADMINS.format(list="\n".join(строки))


def _who(telegram_id: int, имена: dict[int, str]) -> str:
    return f"{имена[telegram_id]} ({telegram_id})" if telegram_id in имена else str(telegram_id)


@router.message(Command("admin_add"), AdminFilter())
async def cmd_admin_add(message: Message, session: AsyncSession, user: User) -> None:
    """Выдать доступ. Самое опасное действие в боте, поэтому в лог — warning."""
    аргумент = (message.text or "").partition(" ")[2].strip()
    if not аргумент:
        await message.answer(ru.ADMIN_ADD_USAGE)
        return

    telegram_id = await resolve_telegram_id(session, аргумент)
    if telegram_id is None:
        await message.answer(ru.ADMIN_ADD_UNKNOWN)
        log.info("кандидат в админы не найден", запрос=аргумент)
        return

    имена = await admin_names(session, {telegram_id})
    добавлен = await add_admin(session, telegram_id, by=message.from_user.id)
    шаблон = ru.ADMIN_ADD_DONE if добавлен else ru.ADMIN_ADD_ALREADY
    await message.answer(шаблон.format(who=_who(telegram_id, имена)))
    if добавлен:
        log.warning(
            "новый админ добавлен из бота",
            user_id=str(user.id),
            кому=telegram_id,
            кем=message.from_user.id,
        )


@router.message(Command("admin_del"), AdminFilter())
async def cmd_admin_del(message: Message, session: AsyncSession, user: User) -> None:
    аргумент = (message.text or "").partition(" ")[2].strip()
    if not аргумент:
        await message.answer(ru.ADMIN_DEL_USAGE)
        return

    telegram_id = await resolve_telegram_id(session, аргумент)
    if telegram_id is None:
        await message.answer(ru.ADMIN_ADD_UNKNOWN)
        return

    имена = await admin_names(session, {telegram_id})
    убран = await remove_admin(session, telegram_id, by=message.from_user.id)
    шаблон = ru.ADMIN_DEL_DONE if убран else ru.ADMIN_DEL_FIXED
    await message.answer(шаблон.format(who=_who(telegram_id, имена)))
    log.warning(
        "правка списка админов",
        user_id=str(user.id),
        кому=telegram_id,
        убран=убран,
        кем=message.from_user.id,
    )


# --- партнёры -------------------------------------------------------------------


@router.message(Command("partner_add"), AdminFilter())
async def cmd_partner_add(message: Message, session: AsyncSession, user: User) -> None:
    """`/partner_add код процент Имя`: завести партнёра или поправить его условия."""
    части = (message.text or "").split(maxsplit=3)
    if len(части) < 4:
        await message.answer(ru.ADMIN_PARTNER_USAGE)
        return
    _, код, процент, имя = части
    try:
        partner, заведён = await save_partner(session, код, процент, имя)
    except PartnerError as exc:
        await message.answer(ru.ADMIN_PARTNER_BAD.get(str(exc), ru.ADMIN_PARTNER_USAGE))
        return

    шаблон = ru.ADMIN_PARTNER_CREATED if заведён else ru.ADMIN_PARTNER_UPDATED
    await message.answer(
        шаблон.format(
            name=esc(partner.name),
            percent=f"{float(partner.percent):g}",
            link=partner_link(partner.code),
        )
    )
    await track(
        session,
        "partner_saved",
        user_id=user.id,
        партнёр=partner.code,
        процент=float(partner.percent),
        новый=заведён,
    )
    log.warning(
        "партнёр заведён" if заведён else "условия партнёра изменены",
        user_id=str(user.id),
        партнёр=partner.code,
        имя=partner.name,
        процент=float(partner.percent),
        кем=message.from_user.id,
    )


@router.message(Command("partner_off", "partner_on"), AdminFilter())
async def cmd_partner_toggle(
    message: Message, command: CommandObject, session: AsyncSession, user: User
) -> None:
    включить = command.command == "partner_on"
    if not command.args:
        await message.answer(ru.ADMIN_PARTNER_TOGGLE_USAGE)
        return
    partner = await set_active(session, command.args, включить)
    if partner is None:
        await message.answer(ru.ADMIN_PARTNER_UNKNOWN)
        return
    шаблон = ru.ADMIN_PARTNER_ON_DONE if включить else ru.ADMIN_PARTNER_OFF_DONE
    await message.answer(шаблон.format(name=esc(partner.name)))
    await track(session, "partner_toggled", user_id=user.id, партнёр=partner.code, активен=включить)
    log.warning(
        "партнёр включён" if включить else "партнёр отключён",
        user_id=str(user.id),
        партнёр=partner.code,
        кем=message.from_user.id,
    )


@router.message(Command("promo_add"), AdminFilter())
async def cmd_promo_add(
    message: Message, command: CommandObject, session: AsyncSession, user: User
) -> None:
    """`/promo_add КОД [код_партнёра]`: привязать код скидки из lava.top к партнёру."""
    части = (command.args or "").split()
    if not части or len(части) > 2:
        await message.answer(ru.ADMIN_PROMO_USAGE)
        return
    try:
        promo, partner, заведён = await save_promo(
            session, части[0], части[1] if len(части) > 1 else None
        )
    except PartnerError as exc:
        await message.answer(ru.ADMIN_PROMO_BAD.get(str(exc), ru.ADMIN_PROMO_USAGE))
        return

    await message.answer(
        ru.ADMIN_PROMO_SAVED.format(
            code=esc(promo.code),
            action="заведён" if заведён else "обновлён",
            partner=ru.ADMIN_PROMO_FOR.format(name=esc(partner.name)) if partner else "",
        )
    )
    await track(
        session,
        "promo_saved",
        user_id=user.id,
        промокод=promo.code,
        партнёр=partner.code if partner else None,
    )
    log.warning(
        "промокод заведён" if заведён else "промокод перепривязан",
        user_id=str(user.id),
        промокод=promo.code,
        партнёр=partner.code if partner else None,
        кем=message.from_user.id,
    )


@router.message(Command("promo_off", "promo_on"), AdminFilter())
async def cmd_promo_toggle(
    message: Message, command: CommandObject, session: AsyncSession, user: User
) -> None:
    включить = command.command == "promo_on"
    if not command.args:
        await message.answer(ru.ADMIN_PROMO_TOGGLE_USAGE)
        return
    promo = await set_promo_active(session, command.args, включить)
    if promo is None:
        await message.answer(ru.ADMIN_PROMO_UNKNOWN)
        return
    шаблон = ru.ADMIN_PROMO_ON_DONE if включить else ru.ADMIN_PROMO_OFF_DONE
    await message.answer(шаблон.format(code=esc(promo.code)))
    await track(session, "promo_toggled", user_id=user.id, промокод=promo.code, активен=включить)
    log.warning(
        "промокод включён" if включить else "промокод отключён",
        user_id=str(user.id),
        промокод=promo.code,
        кем=message.from_user.id,
    )


@router.message(
    Command(
        "admin_add",
        "admin_del",
        "partner_add",
        "partner_off",
        "partner_on",
        "promo_add",
        "promo_off",
        "promo_on",
    )
)
async def cmd_admin_grant_denied(message: Message, session: AsyncSession, user: User) -> None:
    """Чужому — то же молчание, что и на `/admin`."""
    await track(session, "admin_denied", user_id=user.id, telegram_id=message.from_user.id)
    log.warning(
        "попытка раздать права админа",
        user_id=str(user.id),
        telegram_id=message.from_user.id,
        команда=message.text,
    )


# --- разделы -------------------------------------------------------------------


async def _show_limits(callback: CallbackQuery, session: AsyncSession) -> None:
    limits = await get_limits(session)
    await _redraw(callback, render_limits(limits), limits_keyboard(limits))


async def _show_knob(callback: CallbackQuery, session: AsyncSession, key: str) -> None:
    knob = KNOBS_BY_KEY.get(key)
    if knob is None:
        await _show_limits(callback, session)
        return
    limits = await get_limits(session)
    await _redraw(
        callback,
        f"🎚 <b>{knob.title}</b>\n\n{ru.ADMIN_LIMITS}",
        knob_keyboard(knob, current_value(limits, key)),
    )


async def _show_price(callback: CallbackQuery, session: AsyncSession) -> None:
    plans = await editable_plans(session)
    await _redraw(callback, render_price(plans), price_keyboard(plans))


async def _show_spend(callback: CallbackQuery, session: AsyncSession, days: int) -> None:
    spending = await load_spending(session, days)
    await _redraw(callback, render_spending(spending, days), spend_keyboard(days))


async def _show_load(callback: CallbackQuery, session: AsyncSession, queue, hours: int) -> None:
    """Экран нагрузки: справляется ли сервер и целы ли копии базы.

    Состояние копий тянется вместе с нагрузкой одним экраном намеренно: оба
    вопроса про сервер, и оба вспоминают в один и тот же момент — когда решают
    его менять.
    """
    load = await load_report(session, hours=hours, queue=queue)
    await _redraw(callback, render_load(load, await backup_status()), load_keyboard(hours))


async def _show_partners(callback: CallbackQuery, session: AsyncSession) -> None:
    partners = await list_partners(session)
    promos = await promos_by_partner(session)
    await _redraw(callback, render_partners(partners, promos), partners_keyboard(partners))


async def _show_partner(callback: CallbackQuery, session: AsyncSession, value: str) -> None:
    """Отчёт по партнёру. `value` — `<код>.<период>`, битое — назад к списку."""
    код, _, период = value.partition(".")
    код = normalize_code(код)
    partner = await find_partner(session, код) if код else None
    if partner is None:
        await _show_partners(callback, session)
        return
    период = период if период in PERIODS else PERIOD_ALL
    report = await build_report(session, partner, период)
    await _redraw(callback, render_partner_report(report), partner_keyboard(partner.code, период))


async def _show_segments(callback: CallbackQuery, session: AsyncSession) -> None:
    sizes = {code: await audience_size(session, code) for code in SEGMENT_TITLES}
    await _redraw(callback, ru.ADMIN_BROADCAST, segments_keyboard(sizes))


@router.callback_query(F.data.startswith("adm:"))
async def on_admin_action(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    queue,
    state: FSMContext,
    request_id: str,
) -> None:
    action = parse_admin_action(callback.data or "")
    if action is None:
        await callback.answer()
        return

    # Часики гасим сразу: Telegram даёт на ответ считаные секунды, а впереди
    # запросы к базе.
    await callback.answer()

    if action.section == SECTION_NOOP:
        return

    if action.section == SECTION_STATS:
        summary = await load_summary(session)
        await _redraw(callback, render_stats(summary), admin_menu())
        return

    if action.section == SECTION_SPEND:
        # Период из нажатия — число, но нажатие могло прийти и не от нашей
        # клавиатуры: неделя как запасной вариант лучше исключения в хендлере.
        дней = int(action.value) if action.value.isdigit() else 7
        await _show_spend(callback, session, дней)
        return

    if action.section == SECTION_LOAD:
        часов = int(action.value) if action.value.isdigit() else 24
        await _show_load(callback, session, queue, часов)
        return

    if action.section == SECTION_LIMITS:
        await _show_limits(callback, session)
        return

    if action.section == SECTION_KNOB:
        if action.delta:
            await change_limit(session, action.value, action.delta)
        await _show_knob(callback, session, action.value)
        return

    if action.section == SECTION_PRICE:
        await _show_price(callback, session)
        return

    if action.section == SECTION_PLAN:
        if action.delta:
            await change_price(session, action.value, action.delta)
        plan = next((p for p in await editable_plans(session) if p.code == action.value), None)
        if plan is None:
            await _show_price(callback, session)
            return
        await _redraw(callback, render_price([plan]), plan_keyboard(plan))
        return

    if action.section == SECTION_ADMINS:
        await _show_admins(callback, session)
        return

    if action.section == SECTION_PARTNERS:
        await _show_partners(callback, session)
        return

    if action.section == SECTION_PARTNER:
        await _show_partner(callback, session, action.value)
        return

    if action.section == SECTION_BROADCAST:
        if not action.value:
            await _show_segments(callback, session)
            return
        await _ask_text(callback, session, state, action.value)
        return

    if action.section == SECTION_SEND:
        await _start_broadcast(callback, session, user, queue, state, action.value, request_id)
        return

    if action.section == SECTION_CANCEL:
        await state.clear()
        await _redraw(callback, ru.ADMIN_BROADCAST_CANCELLED, admin_menu())
        log.info("рассылка отменена", user_id=str(user.id))
        return

    await _redraw(callback, await _pulse(session), admin_menu())


# --- рассылка ------------------------------------------------------------------


async def _ask_text(
    callback: CallbackQuery, session: AsyncSession, state: FSMContext, segment: str
) -> None:
    count = await audience_size(session, segment)
    title = SEGMENT_TITLES.get(segment, segment)
    if not count:
        # Сегмент опустел, пока админ выбирал: перерисовываем его же экран с
        # честными числами, а не пустой клавиатурой из нулей.
        sizes = {code: await audience_size(session, code) for code in SEGMENT_TITLES}
        await _redraw(callback, ru.ADMIN_BROADCAST_EMPTY, segments_keyboard(sizes))
        return
    await state.set_state(Broadcast.text)
    await state.update_data(segment=segment)
    await _redraw(
        callback,
        ru.ADMIN_BROADCAST_ASK.format(segment=title, count=_people(count)),
        None,
    )


@router.message(Broadcast.text, F.text.startswith("/"), AdminFilter())
async def cancel_broadcast(message: Message, state: FSMContext) -> None:
    """Любая команда отменяет набор текста.

    Иначе `/start`, набранный по привычке, уехал бы всем адресатам как текст
    рассылки — а отменить отправленное нечем.
    """
    await state.clear()
    await message.answer(ru.ADMIN_BROADCAST_CANCELLED)
    log.info("набор текста рассылки прерван командой", команда=message.text)


@router.message(Broadcast.text, F.text, AdminFilter())
async def got_broadcast_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    """Текст рассылки. Показываем ровно то, что уйдёт людям.

    Берём `html_text`, а не голый текст: жирный шрифт и ссылки, набранные
    админом, обязаны доехать до адресатов, а разметка у бота HTML.
    """
    body = message.html_text
    if len(body) > MAX_BROADCAST_LEN:
        await message.answer(ru.ADMIN_BROADCAST_TOO_LONG)
        return

    data = await state.get_data()
    segment = data.get("segment", "")
    count = await audience_size(session, segment)
    await state.update_data(text=body)

    await message.answer(ru.ADMIN_BROADCAST_PREVIEW.format(text=body))
    await message.answer(
        ru.ADMIN_BROADCAST_CONFIRM.format(
            count=_people(count), segment=SEGMENT_TITLES.get(segment, segment)
        ),
        reply_markup=confirm_keyboard(segment),
    )
    log.info("текст рассылки принят", сегмент=segment, знаков=len(body), адресатов=count)


async def _start_broadcast(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    queue,
    state: FSMContext,
    segment: str,
    request_id: str,
) -> None:
    data = await state.get_data()
    body = data.get("text")
    if not body:
        await _redraw(callback, ru.ADMIN_BROADCAST_CANCELLED, admin_menu())
        await state.clear()
        return

    count = await audience_size(session, segment)
    await state.clear()
    await queue.enqueue_job(
        "run_broadcast",
        segment=segment,
        text=body,
        admin_chat_id=callback.message.chat.id,
        request_id=request_id,
    )
    await _redraw(callback, ru.ADMIN_BROADCAST_STARTED.format(count=_people(count)), None)
    await track(session, "broadcast_started", user_id=user.id, сегмент=segment, адресатов=count)
    log.info(
        "рассылка поставлена в очередь",
        user_id=str(user.id),
        сегмент=segment,
        адресатов=count,
        знаков=len(body),
    )
