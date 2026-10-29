import pytest
from app.config import Settings
from app.models import parse_signup, to_public_dict
from pydantic import ValidationError

VALID = {
    "username": "ilmee",
    "email": "  Ilmee@Example.COM ",
    "password": "s3cret",
    "password_confirm": "s3cret",
}


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("DEMO_DEBUG", "true")
    monkeypatch.setenv("DEMO_MAX_USERS", "5")
    settings = Settings()
    assert settings.debug is True
    assert settings.max_users == 5
    assert settings.app_name == "signup-service"


def test_email_is_normalized():
    assert parse_signup(VALID).email == "ilmee@example.com"


def test_invalid_email_is_rejected():
    with pytest.raises(ValidationError):
        parse_signup({**VALID, "email": "nope"})


def test_passwords_must_match():
    with pytest.raises(ValidationError, match="passwords do not match"):
        parse_signup({**VALID, "password_confirm": "other"})


def test_public_dict_hides_passwords():
    assert to_public_dict(parse_signup(VALID)) == {
        "username": "ilmee",
        "email": "ilmee@example.com",
    }
