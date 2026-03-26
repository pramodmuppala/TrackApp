from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "TrackApp"
    app_env: str = "development"
    secret_key: str = "change-me"
    database_url: str = "sqlite:///./usps_tracker.db"
    base_url: str = "http://localhost:8000"

    usps_client_id: str | None = None
    usps_client_secret: str | None = None
    usps_base_url: str = "https://apis.usps.com"
    usps_oauth_path: str = "/oauth2/v3/token"
    usps_tracking_path_template: str = "/tracking/v3/tracking/{tracking_number}"

    fedex_client_id: str | None = None
    fedex_client_secret: str | None = None
    fedex_base_url: str = "https://apis.fedex.com"
    fedex_oauth_path: str = "/oauth/token"
    fedex_tracking_path: str = "/track/v1/trackingnumbers"

    ups_client_id: str | None = None
    ups_client_secret: str | None = None
    ups_base_url: str = "https://onlinetools.ups.com"
    ups_oauth_path: str = "/security/v1/oauth/token"
    ups_tracking_path_template: str = "/api/track/v1/details/{tracking_number}"

    tracking_refresh_minutes: int = 60
    worker_poll_seconds: int = 45

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
