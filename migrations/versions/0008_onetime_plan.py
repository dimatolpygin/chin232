"""Разовый тариф под СБП: способ оплаты у тарифа и напоминание об окончании.

СБП не умеет автосписания — это ограничение самой системы, а не платёжки:
в спецификации lava.top все примеры с `paymentMethod: SBP` помечены как
покупка продукта, а не подписки, и подписочный оффер API отвергает с
`Restricted payment method type`.

Поэтому тарифов становится два: подписка с автопродлением (карта) и разовая
покупка доступа на 30 дней (СБП). Способ оплаты перестаёт быть свойством кода
и становится свойством тарифа, а тарифу без автопродления нужно напоминание
перед окончанием — иначе человек молча потеряет доступ.

Revision ID: 0008_onetime_plan
Revises: 0007_billing
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_onetime_plan"
down_revision = "0007_billing"
branch_labels = None
depends_on = None

# Разовый доступ на 30 дней. offer_id заполняется, когда заказчик заведёт
# цифровой товар в кабинете: пока он пуст, тариф в боте не показывается.
ONETIME_CODE = "once30"


def upgrade() -> None:
    op.add_column("plans", sa.Column("payment_provider", sa.String(32), nullable=True))
    op.add_column("plans", sa.Column("payment_method", sa.String(32), nullable=True))
    op.add_column(
        "plans",
        sa.Column("autorenew", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    # Когда напомнили об окончании. Пусто — ещё не напоминали.
    op.add_column(
        "subscriptions", sa.Column("reminded_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Действующий тариф — подписка картой. Провайдер не задаём: платёжка сама
    # ставит SMART_GLOCAL для рублей, и лишний параметр только сузит выбор.
    op.execute("UPDATE plans SET autorenew = true WHERE code = 'monthly'")

    op.execute(
        """
        INSERT INTO plans (code, title, price, currency, duration_days, periodicity,
                           offer_id, payment_provider, payment_method, autorenew, active)
        VALUES ('once30', 'Доступ на 30 дней', 549, 'RUB', 30, 'ONE_TIME',
                NULL, 'PAY2ME', 'SBP', false, true)
        ON CONFLICT (code) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM subscriptions WHERE plan_code = 'once30'")
    op.execute("DELETE FROM payments WHERE plan_code = 'once30'")
    op.execute("DELETE FROM plans WHERE code = 'once30'")
    op.drop_column("subscriptions", "reminded_at")
    op.drop_column("plans", "autorenew")
    op.drop_column("plans", "payment_method")
    op.drop_column("plans", "payment_provider")
