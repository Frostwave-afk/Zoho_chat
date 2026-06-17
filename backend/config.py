from pydantic_settings import BaseSettings
from pydantic import field_validator
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str
    SECRET_KEY: str

    # LLM APIs
    GEMINI_API_KEY: str
    GROQ_API_KEY: str

    # Google / Gmail OAuth
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/gmail/callback"

    # Zoho OAuth
    ZOHO_CLIENT_ID: str
    ZOHO_CLIENT_SECRET: str
    ZOHO_REGION: str = "in"
    ZOHO_REDIRECT_URI: str = "http://localhost:8000/auth/zoho/callback"

    @field_validator("ZOHO_REGION", mode="before")
    @classmethod
    def clean_region(cls, v: str) -> str:
        # Strip inline comments that may appear in .env
        return v.strip().split()[0]

    @property
    def zoho_auth_base(self) -> str:
        return {
            "com": "https://accounts.zoho.com",
            "in": "https://accounts.zoho.in",
            "eu": "https://accounts.zoho.eu",
            "au": "https://accounts.zoho.com.au",
            "jp": "https://accounts.zoho.jp",
        }.get(self.ZOHO_REGION, "https://accounts.zoho.com")

    @property
    def zoho_api_base(self) -> str:
        return {
            "com": "https://www.zohoapis.com/invoice/v3",
            "in": "https://www.zohoapis.in/invoice/v3",
            "eu": "https://www.zohoapis.eu/invoice/v3",
            "au": "https://www.zohoapis.com.au/invoice/v3",
            "jp": "https://www.zohoapis.jp/invoice/v3",
        }.get(self.ZOHO_REGION, "https://www.zohoapis.com/invoice/v3")

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
