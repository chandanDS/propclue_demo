"""Restore the local PropClue PostgreSQL source snapshot into Supabase."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "deploy" / ".env.local"
BACKUP_FILE = ROOT / "deploy" / "backups" / "propcluev1.sql"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def main() -> None:
    if not ENV_FILE.exists() or not BACKUP_FILE.exists():
        raise SystemExit("Expected deploy/.env.local and deploy/backups/propcluev1.sql.")
    settings = read_env_file(ENV_FILE)
    password = settings.get("SUPABASE_SOURCE_PASSWORD", "")
    host = settings.get("SUPABASE_SOURCE_PG_HOST", "")
    username = settings.get("SUPABASE_SOURCE_PG_USER", "")
    database = settings.get("SUPABASE_SOURCE_PG_DATABASE", "postgres")
    port = settings.get("SUPABASE_SOURCE_PG_PORT", "5432")
    if not all((password, host, username)):
        raise SystemExit("Missing Supabase connection values in ignored deploy/.env.local.")
    psql = shutil.which("psql")
    if not psql:
        candidate = Path(r"C:\Program Files\PostgreSQL\18\bin\psql.exe")
        if candidate.exists():
            psql = str(candidate)
    if not psql:
        raise SystemExit("psql was not found. Install PostgreSQL command-line tools or add psql to PATH.")

    env = os.environ.copy()
    env.update({"PGPASSWORD": password, "PGSSLMODE": "require"})
    subprocess.run(
        [psql, "--set=ON_ERROR_STOP=1", "--host", host, "--port", port,
         "--username", username, "--dbname", database, "--file", str(BACKUP_FILE)],
        check=True,
        env=env,
    )
    print("Restored the PropClue PostgreSQL source snapshot into the Supabase project.")


if __name__ == "__main__":
    main()
