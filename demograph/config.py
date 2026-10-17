import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader so we don't need python-dotenv. Real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    host: str = field(default_factory=lambda: os.getenv("FALKORDB_HOST", "localhost"))
    port: int = field(default_factory=lambda: int(os.getenv("FALKORDB_PORT", "6379")))
    password: str | None = field(default_factory=lambda: os.getenv("FALKORDB_PASSWORD") or None)
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DEMOGRAPH_DATA", "data")))


settings = Settings()
