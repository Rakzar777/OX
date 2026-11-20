import pytest

from app.config import Settings


def test_missing_keys_reported_without_secret():
    with pytest.raises(ValueError,match="BOT_TOKEN"):
        Settings().validate()


def test_menu_can_start_without_payment_keys():
    Settings(bot_token="test",payments_enabled=False).validate()


@pytest.mark.parametrize("url",["http://tickets.test","https://tickets.test/path","https://user:pass@tickets.test","https://tickets.test?x=1"])
def test_public_address_must_be_https_origin(url):
    with pytest.raises(ValueError,match="PUBLIC_BASE_URL"):
        Settings(bot_token="test",shop_id="1",secret_key="test",public_base_url=url).validate()


def test_live_requires_real_documents():
    with pytest.raises(ValueError,match="RULES_URL"):
        Settings(bot_token="test",shop_id="1",secret_key="test",public_base_url="https://tickets.test",test=False).validate()


def test_existing_database_guard_rejects_missing_or_empty_volume(tmp_path):
    path = tmp_path / "missing.sqlite3"
    settings = Settings(bot_token="test", payments_enabled=False,
                        database_path=str(path), require_existing_database=True)
    with pytest.raises(ValueError, match="Рабочая база отсутствует"):
        settings.validate()
    path.touch()
    with pytest.raises(ValueError, match="Рабочая база отсутствует"):
        settings.validate()


def test_partial_volunteer_configuration_rejected_without_printing_values():
    settings = Settings(bot_token="test", payments_enabled=False,
                        volunteer_username="private-volunteer-value")
    with pytest.raises(ValueError, match="VOLUNTEER_USERNAME") as error:
        settings.validate()
    assert "private-volunteer-value" not in str(error.value)
