"""Atomic SQLite persistence; each operation owns and closes its connection."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.schemas import FeatureResult, FileInfo


class Repository:
    def __init__(self, database_path: Path):
        self.path = database_path

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    info TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS features (
                    file_id TEXT NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    feature_index INTEGER NOT NULL,
                    result TEXT NOT NULL,
                    PRIMARY KEY (file_id, feature_index)
                );
            """)

    def save(self, info: FileInfo, features: list[FeatureResult]):
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT INTO files (id, info) VALUES (?, ?)",
                (str(info.id), info.model_dump_json()),
            )
            connection.executemany(
                "INSERT INTO features (file_id, feature_index, result) VALUES (?, ?, ?)",
                ((str(info.id), f.index, f.model_dump_json()) for f in features),
            )

    def get(self, file_id: str) -> FileInfo | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT info FROM files WHERE id = ?", (file_id,)).fetchone()
        return FileInfo.model_validate_json(row[0]) if row else None

    def features(self, file_id: str, offset: int, limit: int) -> list[FeatureResult]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT result FROM features WHERE file_id = ? ORDER BY feature_index LIMIT ? OFFSET ?",
                (file_id, limit, offset),
            ).fetchall()
        return [FeatureResult.model_validate(json.loads(row[0])) for row in rows]
