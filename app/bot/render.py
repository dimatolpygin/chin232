"""Подготовка текста к отправке в Telegram.

Разметка у бота HTML, а в подписи и подсказки попадает текст от модели. Один
угловой скобкой в переводе Telegram отвечает 400 и сообщение не уходит вовсе —
поэтому всё чужое экранируется на границе отправки.
"""

from __future__ import annotations

import html
from datetime import date

from app.bot.texts import ru
from app.core.providers.pronunciation.speechsuper import LOW_INTEGRITY
from app.core.services.limits import KIND_CHECK, Limits, Quota
from app.core.services.progress import Progress
from app.core.services.pronunciation import AssessResult, CharResult, PracticeTarget
from app.core.services.stats import Spend, Summary

# Пороги окраски взяты из демо самого сервиса оценки: ниже 70 — красное, до 85 —
# жёлтое. Свои числа выдумывать нельзя, шкала не линейная.
SCORE_GOOD = 85
SCORE_FAIR = 70

# Пятый тон в китайском — нейтральный, и цифра «5» ученику ничего не говорит.
TONE_NAMES = {1: "1-й", 2: "2-й", 3: "3-й", 4: "4-й", 5: "нейтральный"}


def esc(value: str | None) -> str:
    return html.escape(value or "", quote=False)


def _mark(score: int | None, tone_ok: bool | None = None) -> str:
    # Сбитый тон окрашивает строку красным независимо от балла: именно тон —
    # то, ради чего юзер сюда пришёл, и потерять его в общем «неплохо» нельзя.
    if tone_ok is False:
        return "🔴"
    if score is None:
        return "⚪"
    if score >= SCORE_GOOD:
        return "🟢"
    if score >= SCORE_FAIR:
        return "🟡"
    return "🔴"


def _tone_name(tone: int | None) -> str:
    return TONE_NAMES.get(tone or 0, str(tone))


def render_char(char: CharResult) -> str:
    mark = _mark(char.score, char.tone_ok)
    score = "—" if char.score is None else char.score
    if char.tone_ok is False and char.tone_actual is None:
        return ru.PRON_CHAR_TONE_LOW.format(
            mark=mark,
            char=esc(char.char),
            pinyin=esc(char.pinyin),
            score=score,
            tone=_tone_name(char.tone_expected),
            tone_score="—" if char.tone_score is None else char.tone_score,
        )
    if char.tone_ok is False:
        return ru.PRON_CHAR_WRONG.format(
            mark=mark,
            char=esc(char.char),
            pinyin=esc(char.pinyin),
            score=score,
            expected=_tone_name(char.tone_expected),
            actual=_tone_name(char.tone_actual),
        )
    if char.tone_ok is True:
        return ru.PRON_CHAR_OK.format(
            mark=mark,
            char=esc(char.char),
            pinyin=esc(char.pinyin),
            score=score,
            tone=_tone_name(char.tone_expected),
        )
    return ru.PRON_CHAR_PLAIN.format(
        mark=mark, char=esc(char.char), pinyin=esc(char.pinyin), score=score
    )


def render_result(result: AssessResult) -> str:
    """Результат оценки: общий балл, эталон и разбор по иероглифам."""
    blocks = [
        ru.PRON_RESULT_HEADER.format(overall="—" if result.overall is None else result.overall)
    ]
    if result.tone is not None or result.pronunciation is not None:
        blocks.append(
            ru.PRON_DETAILS.format(
                tone="—" if result.tone is None else result.tone,
                pronunciation="—" if result.pronunciation is None else result.pronunciation,
            )
        )
    blocks.append(
        ru.PRON_REF.format(
            text_zh=esc(result.ref_text),
            pinyin=esc(" ".join(c.pinyin for c in result.chars if c.pinyin)),
        )
    )
    if result.chars:
        blocks.append("\n".join(render_char(c) for c in result.chars))
    сбито = sum(1 for c in result.chars if c.tone_ok is False)
    blocks.append(ru.PRON_TONES_WRONG.format(count=сбито) if сбито else ru.PRON_TONES_OK)
    if result.integrity is not None and result.integrity < LOW_INTEGRITY:
        # Баллам при неполной фразе верить нельзя, и молчать об этом нечестно:
        # юзер решит, что у него провальное произношение, хотя его просто не
        # дослушали.
        blocks.append(ru.PRON_PARTIAL)
    return "\n\n".join(blocks)


def render_left(quota: Quota) -> str:
    """Строка остатка под ответом: «осталось N из M на сегодня».

    У подписчика — пустая строка. Счётчик показывается затем, чтобы человек
    спокойно решил про подписку; тому, кто уже решил и заплатил, напоминать об
    этом под каждым ответом незачем.
    """
    if quota.unlimited:
        return ""
    template = ru.LIMIT_LEFT_CHECKS if quota.kind == KIND_CHECK else ru.LIMIT_LEFT
    line = template.format(left=quota.left, limit=quota.limit)
    return line + (ru.LIMIT_TRIAL if quota.trial else "")


def render_practice(target: PracticeTarget) -> str:
    """Подпись к эталонному голосовому: что именно произносить."""
    blocks = [ru.PRACTICE_HEADER]
    if target.from_correction:
        blocks.append(ru.PRACTICE_FROM_CORRECTION)
    blocks.append(ru.PRON_REF.format(text_zh=esc(target.ref_text), pinyin=esc(target.pinyin)))
    if target.translation:
        blocks.append(esc(target.translation))
    blocks.append(ru.PRACTICE_ASK)
    return "\n\n".join(blocks)


def plural(count: int, one: str, few: str, many: str) -> str:
    """«1 день», «2 дня», «5 дней» — с числом.

    Стрик читается вслух («пять дней подряд»), и «5 день» ломает всю фразу.
    """
    hundred = count % 100
    if 11 <= hundred <= 14:
        return f"{count} {many}"
    last = count % 10
    if last == 1:
        return f"{count} {one}"
    if 2 <= last <= 4:
        return f"{count} {few}"
    return f"{count} {many}"


def calendar_rows(progress: Progress) -> list[str]:
    """Календарь месяца строками по семь дней.

    Слева диапазон чисел, справа квадраты: так строка ровно по неделе и не
    нужны пустые клетки в начале месяца, которые в переписке выглядят дырой.
    Рисуем прожитую часть месяца — будущие дни не «пропущены», их ещё не было.
    """
    last = progress.today.day
    rows: list[str] = []
    for start in range(1, last + 1, 7):
        end = min(start + 6, last)
        cells = "".join(
            ru.PROGRESS_DAY_DONE
            if date(progress.month.year, progress.month.month, day) in progress.month_days
            else ru.PROGRESS_DAY_MISS
            for day in range(start, end + 1)
        )
        rows.append(f"{start:02d}–{end:02d}  {cells}")
    return rows


def _score_line(progress: Progress) -> str:
    if progress.score_avg is None:
        return ru.PROGRESS_SCORE_NONE
    line = ru.PROGRESS_SCORE.format(score=progress.score_avg)
    delta = progress.score_delta
    if delta is None:
        return line
    if delta > 0:
        return line + ru.PROGRESS_SCORE_UP.format(delta=delta)
    if delta < 0:
        return line + ru.PROGRESS_SCORE_DOWN.format(delta=abs(delta))
    return line + ru.PROGRESS_SCORE_FLAT


def render_progress(progress: Progress) -> str:
    """Экран раздела «Прогресс»."""
    if progress.empty:
        # Нули и пустой календарь выглядят как поломка, а не как «вы ещё не
        # начали»: у человека нет способа отличить одно от другого.
        return ru.PROGRESS_EMPTY

    if not progress.streak:
        первая = ru.PROGRESS_STREAK_NONE
    elif progress.streak == 1:
        первая = ru.PROGRESS_STREAK_ONE
    else:
        первая = ru.PROGRESS_STREAK.format(days=plural(progress.streak, "день", "дня", "дней"))
    серия = [первая]
    if progress.best_streak > progress.streak:
        рекорд = plural(progress.best_streak, "день", "дня", "дней")
        серия.append(ru.PROGRESS_BEST.format(days=рекорд))
    if not progress.today_done:
        серия.append(ru.PROGRESS_TODAY_LEFT)

    календарь = [f"<b>{ru.MONTHS[progress.month.month - 1]} {progress.month.year}</b>"]
    календарь += calendar_rows(progress)

    итоги = [
        ru.PROGRESS_TOTALS.format(
            messages=plural(progress.messages, "сообщение", "сообщения", "сообщений"),
            checks=plural(progress.checks, "разбор", "разбора", "разборов"),
        ),
        _score_line(progress),
    ]

    return "\n\n".join(
        [ru.PROGRESS_HEADER, "\n".join(серия), "\n".join(календарь), "\n".join(итоги)]
    )


# --- админка -------------------------------------------------------------------


def money(value: float) -> str:
    """Доллары для админки.

    Мелкие суммы округлять до копеек нельзя: расход за сутки на паре юзеров —
    это центы, и «$0.00» вместо «$0.0184» выглядит как сломанный счётчик.
    """
    if value and abs(value) < 1:
        return f"{value:.4f}"
    return f"{value:,.2f}".replace(",", " ")


def render_stats(summary: Summary) -> str:
    """Экран статистики: юзеры, активность, деньги."""
    выручка = (
        " · ".join(
            f"{amount:,.0f} {cur}".replace(",", " ") for cur, amount in summary.revenue.items()
        )
        or ru.ADMIN_STATS_NO_REVENUE
    )
    return ru.ADMIN_STATS.format(
        users_total=summary.users_total,
        users_today=summary.users_today,
        users_week=summary.users_week,
        active_today=summary.active_today,
        active_week=summary.active_week,
        paying=summary.paying,
        conversion=f"{summary.conversion:.1f}",
        messages=plural(summary.messages_today, "сообщение", "сообщения", "сообщений"),
        checks=plural(summary.checks_today, "разбор", "разбора", "разборов"),
        revenue=выручка,
    )


def render_limits(limits: Limits) -> str:
    """Экран лимитов. Сами числа стоят на кнопках, здесь только смысл экрана."""
    строки = [ru.ADMIN_LIMITS]
    if not limits.messages or not limits.trial_messages:
        строки.append(ru.ADMIN_LIMITS_ZERO)
    return "\n\n".join(строки)


def render_price(plans: list) -> str:
    """Экран цены: сколько стоит каждый тариф и предупреждение про оффер."""
    if not plans:
        return "\n\n".join([ru.ADMIN_PRICE, ru.ADMIN_PRICE_EMPTY])
    строки = [
        ru.ADMIN_PRICE_ROW.format(
            title=esc(plan.title),
            price=f"{float(plan.price):,.0f}".replace(",", " "),
            currency=esc(plan.currency),
            offer="" if plan.offer_id else ru.ADMIN_PRICE_NO_OFFER,
        )
        for plan in plans
    ]
    return "\n\n".join([ru.ADMIN_PRICE, "\n".join(строки), ru.ADMIN_PRICE_WARNING])


def render_spending(spending: list[Spend], days: int) -> str:
    """Экран расхода по сервисам за период."""
    заголовок = ru.ADMIN_SPEND.format(period=ru.SPEND_PERIODS.get(days, f"за {days} дн"))
    if not spending:
        return "\n\n".join([заголовок, ru.ADMIN_SPEND_EMPTY])

    строки = []
    for spend in spending:
        строка = ru.ADMIN_SPEND_ROW.format(
            name=ru.PROVIDER_TITLES.get(spend.provider, esc(spend.provider)),
            cost=money(spend.cost),
            calls=plural(spend.calls, "вызов", "вызова", "вызовов"),
            units=f"{spend.units:,.0f}".replace(",", " "),
            unit=spend.unit,
        )
        if spend.errors:
            строка += ru.ADMIN_SPEND_ERRORS.format(errors=spend.errors)
        строки.append(строка)

    итог = ru.ADMIN_SPEND_TOTAL.format(cost=money(sum(s.cost for s in spending)))
    return "\n\n".join([заголовок, "\n".join(строки), итог, ru.ADMIN_SPEND_NOTE])
