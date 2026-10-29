from pydantic import BaseSettings


class Settings(BaseSettings):
    app_name: str = "signup-service"
    debug: bool = False
    max_users: int = 100

    class Config:
        env_prefix = "DEMO_"
