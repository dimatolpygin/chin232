"""Промпт начала разговора по выбранной теме.

Смена темы в настройках должна быть слышна сразу: бот сам говорит первую
фразу по новой теме. Приветственный промпт для этого не годится — он велит
здороваться, а посреди разговора «здравствуйте» второй раз выглядит сбоем.

Revision ID: 0009_topic_prompt
Revises: 0008_onetime_plan
Create Date: 2026-08-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_topic_prompt"
down_revision: str | None = "0008_onetime_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TOPIC_START_PROMPT = """Ты — доброжелательный репетитор китайского. Уровень
собеседника: {hsk_level}. Разговор переходит на новую тему: {topic}.

Скажи одну фразу, которая начинает разговор именно на этой теме, и задай по ней
простой вопрос. Не здоровайся — вы уже общаетесь. Лексика строго под
{hsk_level}. Одно-два предложения.

Верни СТРОГО JSON без markdown-обёртки:
{{"reply_zh": "ответ иероглифами",
  "pinyin": "пиньинь со знаками тонов",
  "translation": "перевод на русский",
  "correction": null}}"""


def upgrade() -> None:
    prompts = sa.table(
        "prompts",
        sa.column("code", sa.String),
        sa.column("version", sa.Integer),
        sa.column("body", sa.Text),
    )
    op.bulk_insert(
        prompts,
        [{"code": "topic_start", "version": 1, "body": TOPIC_START_PROMPT}],
    )


def downgrade() -> None:
    op.execute("DELETE FROM prompts WHERE code = 'topic_start'")
