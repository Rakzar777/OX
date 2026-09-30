import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass
class Settings:
    bot_token: str = ""
    bot_username: str = ""
    public_base_url: str = "http://127.0.0.1:8000"
    shop_id: str = ""
    secret_key: str = ""
    test: bool = True
    payments_enabled: bool = True
    database_path: str = "data/bot.sqlite3"
    events_file: str = "events.json"
    manager: str = "Al_limm"
    rules_url: str = ""
    privacy_url: str = ""
    offer_url: str = ""
    vat_code: int = 1
    payment_mode: str = "full_payment"
    payment_subject: str = "service"
    tax_system_code: int | None = None
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def load(cls):
        load_dotenv()
        return cls(
            bot_token=os.getenv("BOT_TOKEN", ""),
            bot_username=os.getenv("BOT_USERNAME", "").lstrip("@"),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            shop_id=os.getenv("YOOKASSA_SHOP_ID", ""),
            secret_key=os.getenv("YOOKASSA_SECRET_KEY", ""),
            test=os.getenv("YOOKASSA_TEST", "true").lower() == "true",
            payments_enabled=os.getenv("PAYMENTS_ENABLED", "true").lower() == "true",
            database_path=os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
            events_file=os.getenv("EVENTS_FILE", "events.json"),
            manager=os.getenv("MANAGER_USERNAME", "Al_limm").lstrip("@"),
            rules_url=os.getenv("RULES_URL", ""),
            privacy_url=os.getenv("PRIVACY_URL", ""),
            offer_url=os.getenv("OFFER_URL", ""),
            vat_code=int(os.getenv("RECEIPT_VAT_CODE", "1")),
            payment_mode=os.getenv("RECEIPT_PAYMENT_MODE", "full_payment"),
            payment_subject=os.getenv("RECEIPT_PAYMENT_SUBJECT", "service"),
            tax_system_code=int(os.environ["RECEIPT_TAX_SYSTEM_CODE"]) if os.getenv("RECEIPT_TAX_SYSTEM_CODE") else None,
            host=os.getenv("HOST", "127.0.0.1"),
            port=int(os.getenv("PORT", "8000")),
        )

    def validate(self):
        if not self.payments_enabled:
            if not self.bot_token:
                raise ValueError("Заполните .env: BOT_TOKEN")
            if not Path(self.events_file).is_file():
                raise ValueError("Не найден файл мероприятий: " + self.events_file)
            return
        missing = [name for name, value in {
            "BOT_TOKEN": self.bot_token, "YOOKASSA_SHOP_ID": self.shop_id,
            "YOOKASSA_SECRET_KEY": self.secret_key,
        }.items() if not value]
        if missing:
            raise ValueError("Заполните .env: " + ", ".join(missing))
        url = urlparse(self.public_base_url)
        if (url.scheme != "https" or not url.hostname or url.hostname.endswith("example.com")
                or url.path or url.query or url.fragment or url.username or url.password):
            raise ValueError("PUBLIC_BASE_URL должен содержать реальный HTTPS-адрес сервера")
        if not self.test and not all((self.rules_url, self.privacy_url, self.offer_url)):
            raise ValueError("Для боевых продаж заполните RULES_URL, PRIVACY_URL, OFFER_URL")
        for value in (self.rules_url, self.privacy_url, self.offer_url):
            if value and urlparse(value).scheme != "https":
                raise ValueError("Ссылки на документы должны начинаться с https://")
        if not Path(self.events_file).is_file():
            raise ValueError("Не найден файл мероприятий: " + self.events_file)
