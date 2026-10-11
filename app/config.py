"""Environment-based configuration with explicit resource budgets."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path = Path("data/measurements.sqlite3")
    max_upload_bytes: int = 20 * 1024 * 1024
    max_archive_bytes: int = 100 * 1024 * 1024
    max_archive_members: int = 100
    max_features: int = 10_000
    max_coordinates: int = 500_000

    def __post_init__(self):
        for name in (
            "max_upload_bytes",
            "max_archive_bytes",
            "max_archive_members",
            "max_features",
            "max_coordinates",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")

    @classmethod
    def from_environment(cls):
        values = {}
        for name in cls.__dataclass_fields__:
            value = os.getenv(f"GEO_{name.upper()}")
            if value is not None:
                values[name] = Path(value) if name == "database_path" else int(value)
        return cls(**values)
