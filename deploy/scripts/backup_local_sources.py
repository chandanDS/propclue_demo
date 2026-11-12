"""Create local-only PostgreSQL and MongoDB source backups for a hosted stack."""
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from pathlib import Path

from bson import json_util
from pymongo import MongoClient
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "deploy" / "backups"
DEFAULT_PG_URL = "postgresql+psycopg://postgres:postgres@localhost:5433/propcluev1"
DEFAULT_MONGO_URI = "mongodb://localhost:27017/propcluev1"


def find_pg_dump() -> str:
    path = shutil.which("pg_dump")
    if path:
        return path
    windows_default = Path(r"C:\Program Files\PostgreSQL\18\bin\pg_dump.exe")
    if windows_default.exists():
        return str(windows_default)
    raise SystemExit("pg_dump was not found. Install PostgreSQL Database Tools or add pg_dump to PATH.")


def backup_postgres() -> Path:
    url = make_url(os.environ.get("PROPCLUE_SOURCE_DATABASE_URL", DEFAULT_PG_URL))
    password = url.password or getpass.getpass("Local PostgreSQL password: ")
    output = BACKUP_DIR / "propcluev1.sql"
    command = [
        find_pg_dump(), "--format=plain", "--no-owner", "--no-privileges",
        "--host", url.host or "localhost", "--port", str(url.port or 5432),
        "--username", url.username or "postgres", "--dbname", url.database or "propcluev1",
        "--file", str(output),
    ]
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    subprocess.run(command, check=True, env=env)
    return output


def backup_mongo() -> tuple[Path, int]:
    uri = os.environ.get("PROPCLUE_SOURCE_MONGODB_URI", DEFAULT_MONGO_URI)
    output = BACKUP_DIR / "area_dna.jsonl"
    client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    collection = client.get_default_database()["area_dna"]
    count = 0
    with output.open("w", encoding="utf-8") as stream:
        for document in collection.find({}):
            stream.write(json_util.dumps(document, ensure_ascii=False) + "\n")
            count += 1
    client.close()
    return output, count


def main() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    pg_file = backup_postgres()
    mongo_file, mongo_count = backup_mongo()
    print(f"PostgreSQL backup: {pg_file.name} ({pg_file.stat().st_size:,} bytes)")
    print(f"MongoDB area_dna backup: {mongo_file.name} ({mongo_count:,} documents)")
    print("Backups stay under deploy/backups/ and are excluded from Git.")


if __name__ == "__main__":
    main()
