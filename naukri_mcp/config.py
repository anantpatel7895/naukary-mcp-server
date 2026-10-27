"""Runtime configuration, loaded from the environment / a .env file.

Defaults are deliberately conservative: nothing is sent outbound and no scraping
happens until you explicitly opt in.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HOME = Path.home() / ".naukri-mcp"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- storage -------------------------------------------------------------
    database_url: str = Field(default="", description="SQLAlchemy URL; blank means local SQLite.")
    data_dir: Path = Field(default=DEFAULT_HOME, description="Where the DB and rendered resumes live.")

    # --- safety switches -----------------------------------------------------
    dry_run: bool = Field(
        default=True,
        description="When true, emails are logged to the DB but never handed to an SMTP server.",
    )

    # --- Naukri provider (opt-in; see docs/naukri-session.md) ----------------
    naukri_enabled: bool = False
    #: The whole Cookie request header copied from your logged-in browser.
    naukri_cookie: str = ""
    #: Optional Authorization header value. Naukri's internal jobapi sends a bearer
    #: token on some accounts and relies on the cookie alone on others, so both are
    #: supported and either may be what makes requests authenticate.
    naukri_auth_token: str = ""
    naukri_rate_limit_rps: float = Field(
        default=0.33, gt=0, description="Requests per second. 0.33 is ~1 request every 3s."
    )
    naukri_cache_ttl_seconds: int = 900

    # --- SMTP ---------------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    from_email: str = ""
    from_name: str = ""

    # --- optional headless LLM providers ------------------------------------
    anthropic_api_key: str = ""
    groq_api_key: str = ""

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        return Path(str(v)).expanduser() if v else v

    @field_validator("naukri_cookie", "naukri_auth_token", mode="before")
    @classmethod
    def _clean_header_value(cls, v: object) -> object:
        """Tolerate the shapes a copy-paste actually arrives in.

        Browsers and devtools hand you the value with a `Cookie:` / `Authorization:`
        label, wrapping quotes, or embedded newlines from a multi-line copy. Any of those
        makes the request fail later with an opaque 403, so normalize here instead.
        """
        if not isinstance(v, str):
            return v
        text = v.strip().strip('"').strip("'")
        for label in ("cookie:", "authorization:"):
            if text.lower().startswith(label):
                text = text[len(label) :].strip()
        # A header value cannot contain raw newlines; a wrapped paste is still usable.
        return " ".join(text.split())

    # --- derived ------------------------------------------------------------
    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            # Allow ~ in a sqlite URL, which SQLAlchemy will not expand itself.
            if self.database_url.startswith("sqlite") and "~" in self.database_url:
                prefix, _, path = self.database_url.partition("///")
                return f"{prefix}///{Path(path).expanduser()}"
            return self.database_url
        return f"sqlite:///{self.data_dir / 'jobs.db'}"

    @property
    def renders_dir(self) -> Path:
        return self.data_dir / "renders"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.from_email)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.renders_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
