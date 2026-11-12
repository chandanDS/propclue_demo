"""Upsert the local PropClue area_dna snapshot into MongoDB Atlas."""
from __future__ import annotations

import argparse
from pathlib import Path

from bson import json_util
from pymongo import MongoClient, ReplaceOne


ROOT = Path(__file__).resolve().parents[2]
BACKUP_FILE = ROOT / "deploy" / "backups" / "area_dna.jsonl"


def read_atlas_uri(path: Path) -> str:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("MONGODB_URI="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("The credentials file does not contain MONGODB_URI.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-file", required=True, type=Path)
    args = parser.parse_args()
    if not BACKUP_FILE.exists():
        raise SystemExit(f"Missing local snapshot: {BACKUP_FILE}")

    client = MongoClient(read_atlas_uri(args.credentials_file), serverSelectionTimeoutMS=20000)
    client.admin.command("ping")
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

    stored = collection.count_documents({})
    client.close()
    print(f"Upserted {total:,} area_dna documents; Atlas now contains {stored:,} documents in propcluev1.area_dna.")


if __name__ == "__main__":
    main()
