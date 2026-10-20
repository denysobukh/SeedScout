import logging
import sys
from urllib.parse import quote

from tg_torrent_downloader.logging import RedactingFormatter


def test_redacts_message_arguments_exceptions_and_encoded_secrets():
    secret = "private/password@123"
    formatter = RedactingFormatter((secret,))
    try:
        raise RuntimeError(f"failed with {secret}")
    except RuntimeError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            "",
            1,
            "request %s %s",
            (secret, quote(secret, safe="")),
            sys.exc_info(),
        )
    text = formatter.format(record)
    assert secret not in text
    assert quote(secret, safe="") not in text
    assert "RuntimeError" in text
    assert "[REDACTED]" in text


def test_redacts_tokens_and_authentication_headers():
    formatter = RedactingFormatter(())
    record = logging.LogRecord(
        "test",
        logging.WARNING,
        "",
        1,
        "Cookie: session=private\nAuthorization: Basic private\n"
        "https://api.telegram.org/bot12345:abcdefghijklmnopqrstuvwxyz/getMe",
        (),
        None,
    )
    text = formatter.format(record)
    assert "private" not in text
    assert "12345:abcdefghijklmnopqrstuvwxyz" not in text
    assert "getMe" in text
