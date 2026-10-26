from types import SimpleNamespace

from app.sales_team import assignable_sales_users


def test_excludes_anastasia_by_id_and_name():
    settings = SimpleNamespace(
        excluded_sales_user_id_set={"26"},
        excluded_sales_user_name_set={"анастасия коравчук"},
    )
    users = [
        {"ID": "26", "NAME": "Настя", "LAST_NAME": "Руководитель"},
        {"ID": "99", "NAME": "Анастасия", "LAST_NAME": "Коравчук"},
        {"ID": "110", "NAME": "Иван", "LAST_NAME": "Продажников"},
    ]

    assert assignable_sales_users(users, settings) == [users[2]]


def test_default_rule_excludes_bitrix_user_26():
    settings = SimpleNamespace()
    users = [
        {"ID": "26", "NAME": "Анастасия", "LAST_NAME": "Коравчук"},
        {"ID": "110", "NAME": "Иван", "LAST_NAME": "Продажников"},
    ]

    assert assignable_sales_users(users, settings) == [users[1]]
