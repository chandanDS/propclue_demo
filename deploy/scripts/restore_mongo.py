"""Restore the local area_dna JSONL snapshot into the stack's MongoDB."""
from __future__ import annotations

from pathlib import Path

from bson import json_util
from pymongo import MongoClient, ReplaceOne


ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / "deploy" / ".env"
BACKUP_FILE = ROOT / "deploy" / "backups" / "area_dna.jsonl"


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
        raise SystemExit("Expected deploy/.env and deploy/backups/area_dna.jsonl. Copy the deployment env file and run the backup script first.")
    env = read_env_file(ENV_FILE)
    port = int(env.get("PROPCLUE_MONGO_HOST_PORT", "27018"))
    client = MongoClient(
        "127.0.0.1", port,
        username=env["PROPCLUE_MONGO_ADMIN_USER"],
        password=env["PROPCLUE_MONGO_ADMIN_PASSWORD"],
        authSource="admin",
        serverSelectionTimeoutMS=5000,
    )
    collection = client["propcluev1"]["area_dna"]
    batch: list[ReplaceOne] = []
    total = 0
    with BACKUP_FILE.open(encoding="utf-8") as stream:
        for line in stream:
            document = json_util.loads(line)
            batch.append(ReplaceOne({"_id": document["_id"]}, document, upsert=True))
            if len(batch) >= 250:
                collection.bulk_write(batch, ordered=False)
                total += len(batch)
                batch.clear()
    if batch:
        collection.bulk_write(batch, ordered=False)
        total += len(batch)
    print(f"Restored or updated {total:,} area_dna documents. Existing other collections were left untouched.")
    client.close()


if __name__ == "__main__":
    main()
