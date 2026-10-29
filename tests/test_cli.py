import anthropic
import httpx2 as httpx
import pytest

from patchpilot.cli import explain_api_error

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def status_error(cls, code, message):
    body = {"type": "error", "error": {"type": "x", "message": message}}
    return cls(message, response=httpx.Response(code, request=REQUEST), body=body)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            status_error(
                anthropic.BadRequestError, 400, "Your credit balance is too low to access the API."
            ),
            "out of credit",
        ),
        (status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), "key was rejected"),
        (status_error(anthropic.RateLimitError, 429, "slow down"), "rate-limiting"),
        (status_error(anthropic.InternalServerError, 500, "boom"), "(500): boom"),
        (anthropic.APIConnectionError(request=REQUEST), "internet connection"),
    ],
)
def test_api_errors_become_one_actionable_sentence(error, expected):
    assert expected in explain_api_error(error)
