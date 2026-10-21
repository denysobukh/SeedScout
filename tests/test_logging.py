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


def test_http_redaction_preserves_useful_fields_and_duplicate_headers():
    from tg_torrent_downloader.logging import safe_headers, safe_post_data, safe_url

    headers = safe_headers(
        [
            ("User-Agent", "SeedScoutBot/0.1"),
            ("Cookie", "cookie-secret"),
            ("Set-Cookie", "first-secret"),
            ("Set-Cookie", "second-secret"),
            ("Authorization", "Bearer token-secret"),
            ("X-Api-Key", "api-secret"),
            ("Location", "https://rutracker.org/forum/?sid=session-secret&t=21"),
        ]
    )
    text = str(headers)
    assert "SeedScoutBot/0.1" in text
    assert sum(key == "Set-Cookie" for key, _ in headers) == 2
    for value in (
        "cookie-secret",
        "first-secret",
        "second-secret",
        "token-secret",
        "api-secret",
        "session-secret",
    ):
        assert value not in text
    data = safe_post_data(
        b"login_username=user-secret&login_password=pw-secret&"
        b"cap_code_challenge-secret=answer-secret&cap_sid=challenge-secret&"
        b"login=Submit&nm=query&custom=unknown-secret"
    )
    assert ("login", "Submit") in data and ("nm", "query") in data
    assert "secret" not in str(data)
    assert "password" not in safe_url("https://user:password@rutracker.org/forum/?t=21#private")


def test_sensitive_user_input_is_redacted_before_log_record_creation(caplog):
    import json

    from tg_torrent_downloader.logging import log_user_input

    with caplog.at_level(logging.INFO):
        log_user_input(1, 2, "query\nforged event")
        log_user_input(1, 2, "captcha-secret", sensitive=True)
    records = [json.loads(r.message) for r in caplog.records]
    assert records[0]["input"] == "query\nforged event"
    assert "\n" not in caplog.records[0].message  # JSON escapes prevent forged log lines
    assert records[1]["input"] == "[REDACTED]"
    assert "captcha-secret" not in caplog.text


def test_private_file_logging_rotates_and_redacts(tmp_path):
    import io

    from tg_torrent_downloader.logging import _PrivateRotatingFileHandler

    path = tmp_path / "bot.log"
    file_handler = _PrivateRotatingFileHandler(path, maxBytes=200, backupCount=2, encoding="utf-8")
    stream = io.StringIO()
    stream_handler = logging.StreamHandler(stream)
    formatter = RedactingFormatter(("private-password",))
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)
    logger = logging.Logger("isolated")
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    for i in range(15):
        logger.info("event=%s password=%s", i, "private-password")
    file_handler.close()
    files = list(tmp_path.glob("bot.log*"))
    assert len(files) == 3
    assert "event=14" in path.read_text()
    for file in files:
        assert file.stat().st_mode & 0o777 == 0o600
        assert "private-password" not in file.read_text()
    assert "private-password" not in stream.getvalue()


def test_form_encoding_is_readable_and_invalid_body_not_dumped():
    from urllib.parse import urlencode

    from tg_torrent_downloader.logging import safe_post_data

    form = urlencode({"nm": "Тест", "login_password": "secret"}, encoding="cp1251").encode()
    assert ("nm", "Тест") in safe_post_data(form)
    assert "secret" not in str(safe_post_data(form))
    assert safe_post_data(b"\xffsecret") == [("unparsed_body", "[REDACTED]")]


def test_url_encoded_and_json_escaped_secrets_are_redacted():
    import json
    from urllib.parse import quote

    secret = 'Пароль"\\test'
    formatter = RedactingFormatter((secret,))
    message = json.dumps({"input": secret}, ensure_ascii=False) + quote(secret, encoding="cp1251")
    record = logging.LogRecord("test", logging.INFO, "", 1, message, (), None)
    text = formatter.format(record)
    assert json.dumps(secret, ensure_ascii=False)[1:-1] not in text
    assert quote(secret, encoding="cp1251") not in text


def test_rotation_error_does_not_dump_original_record(tmp_path, monkeypatch, capsys):
    from tg_torrent_downloader.logging import _PrivateRotatingFileHandler

    handler = _PrivateRotatingFileHandler(tmp_path / "bot.log", maxBytes=100, backupCount=1)

    def fail(record):
        raise OSError("disk unavailable")

    monkeypatch.setattr(handler, "shouldRollover", fail)
    handler.emit(logging.LogRecord("test", logging.INFO, "", 1, "private-password", (), None))
    handler.close()
    captured = capsys.readouterr()
    assert "Diagnostic log write failed" in captured.err
    assert "private-password" not in captured.err


def test_sensitive_tokens_in_nonstandard_url_paths_and_redirect_forms_are_redacted():
    from tg_torrent_downloader.logging import safe_fields, safe_url

    assert "path-secret" not in safe_url("https://rutracker.org/captcha/path-secret.png")
    assert "password" not in str(safe_fields([("redirect", "//user:password@host/path")]))


def test_credentials_registered_after_startup_are_redacted():
    from io import StringIO

    from tg_torrent_downloader.logging import register_secrets

    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(RedactingFormatter(()))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        register_secrets("new-private/password")
        root.warning("credential %s", quote("new-private/password", safe=""))
        assert "new-private" not in output.getvalue()
        assert "[REDACTED]" in output.getvalue()
    finally:
        root.removeHandler(handler)
