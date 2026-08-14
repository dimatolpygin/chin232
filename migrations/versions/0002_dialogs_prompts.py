"""Голосовой круг: dialogs, prompts + системный промпт

Revision ID: 0002_dialogs_prompts
Revises: 0001_initial
Create Date: 2026-08-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_dialogs_prompts"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Промпт лежит в базе, а не в коде: его правят без деплоя. Плейсхолдеры
# подставляются сервисом диалога.
SYSTEM_PROMPT = """Ты — доброжелательный собеседник и репетитор китайского языка.
Собеседник учит китайский, его уровень: {hsk_level}. Тема разговора: {topic}.

Правила:
1. Отвечай ТОЛЬКО на упрощённом китайском (简体字). Уровень лексики и грамматики
   строго под {hsk_level}: для HSK 1-2 короткие фразы из простых слов, для HSK 5-6
   развёрнутая живая речь с идиомами.
2. Собеседник может писать или говорить по-русски. Понимай русский, но отвечай
   по-китайски.
3. Держи разговор живым: отвечай по существу и задавай встречный вопрос.
4. Длина ответа — 1-2 предложения, это устная речь.
5. Если в реплике собеседника есть ошибка в грамматике или словоупотреблении,
   мягко объясни её по-русски в поле correction. Если ошибок нет — null.

Верни СТРОГО JSON без markdown-обёртки:
{{"reply_zh": "ответ иероглифами",
  "pinyin": "пиньинь со знаками тонов",
  "translation": "перевод ответа на русский",
  "correction": "объяснение ошибки по-русски или null"}}"""

GREETING_PROMPT = """Ты — доброжелательный репетитор китайского. Уровень
собеседника: {hsk_level}. Тема: {topic}.

Поздоровайся по-китайски и задай один простой вопрос, чтобы завязать разговор.
Лексика строго под {hsk_level}. Одно-два предложения.

Верни СТРОГО JSON без markdown-обёртки:
{{"reply_zh": "ответ иероглифами",
  "pinyin": "пиньинь со знаками тонов",
  "translation": "перевод на русский",
  "correction": null}}"""


def upgrade() -> None:
    op.create_table(
        "dialogs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("text_zh", sa.Text(), nullable=True),
        sa.Column("pinyin", sa.Text(), nullable=True),
        sa.Column("translation", sa.Text(), nullable=True),
        sa.Column("correction", sa.Text(), nullable=True),
        sa.Column("audio_file_id", sa.String(length=256), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dialogs_user_id", "dialogs", ["user_id"])
    op.create_index("ix_dialogs_created_at", "dialogs", ["created_at"])

    op.create_table(
        "prompts",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("code"),
    )

    prompts = sa.table(
        "prompts",
        sa.column("code", sa.String),
        sa.column("version", sa.Integer),
        sa.column("body", sa.Text),
    )
    op.bulk_insert(
        prompts,
        [
            {"code": "dialog_system", "version": 1, "body": SYSTEM_PROMPT},
            {"code": "greeting", "version": 1, "body": GREETING_PROMPT},
        ],
    )


def downgrade() -> None:
    op.drop_table("prompts")
    op.drop_index("ix_dialogs_created_at", table_name="dialogs")
    op.drop_index("ix_dialogs_user_id", table_name="dialogs")
    op.drop_table("dialogs")
