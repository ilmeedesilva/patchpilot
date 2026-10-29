from pydantic import BaseModel, root_validator, validator


class SignupForm(BaseModel):
    username: str
    email: str
    password: str
    password_confirm: str

    @validator("email")
    def normalize_email(cls, value):
        if "@" not in value:
            raise ValueError("not an email address")
        return value.strip().lower()

    @root_validator
    def passwords_match(cls, values):
        if values.get("password") != values.get("password_confirm"):
            raise ValueError("passwords do not match")
        return values


def to_public_dict(form: SignupForm) -> dict:
    return form.dict(exclude={"password", "password_confirm"})


def parse_signup(data: dict) -> SignupForm:
    return SignupForm.parse_obj(data)
