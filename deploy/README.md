# Self-hosted PropClue demo stack

This stack runs the Streamlit app, the PropClue source PostgreSQL database, MongoDB, and a separate PostgreSQL database for demo workflow records. PostgreSQL and MongoDB are not published on public interfaces. Their host-side migration ports bind to `127.0.0.1`; the app port also binds to loopback and should sit behind an HTTPS reverse proxy or a private access gateway.

## Local preparation

1. Copy `.env.example` to `.env` and replace each example password with a long, random alphanumeric password. Keep `.env` private; it is ignored by Git.
2. Create local-only backups from your PropClue databases:

   ```powershell
   ..\.venv\Scripts\python.exe deploy\scripts\backup_local_sources.py
   ```

   The PostgreSQL password is requested without echoing it. The script exports the PostgreSQL database and MongoDB `area_dna` collection into `deploy/backups/`. Those files are excluded from Git.

3. Start the stack:

   ```powershell
   docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build
   ```

4. Restore PostgreSQL from the host-only SQL dump. The Postgres port is loopback-only:

   ```powershell
   Get-Content deploy\backups\propcluev1.sql -Raw | docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T postgres psql -U postgres -d propcluev1
   docker compose --env-file deploy/.env -f deploy/compose.yaml exec postgres psql -U postgres -d propcluev1 -c "GRANT USAGE ON SCHEMA public TO propclue_reader; GRANT SELECT ON ALL TABLES IN SCHEMA public TO propclue_reader; ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO propclue_reader;"
   ```

5. Restore MongoDB's area documents:

   ```powershell
   ..\.venv\Scripts\python.exe deploy\scripts\restore_mongo.py
   ```

6. Verify `http://127.0.0.1:8501`. The live catalogue should load from the containers; workflow records use the separate `propclue_demo_workflow` database.

## Move the stack to a cloud server

The destination needs Docker Engine and the Compose plugin, persistent disk, and a DNS name. Copy the repository and `deploy/.env` to the server over SSH. Transfer `deploy/backups/` separately over SSH/SCP; do not push database dumps or `.env` to GitHub. Run the Compose and restore commands on the server.

Configure a TLS reverse proxy or private access gateway on the server to proxy HTTPS to `127.0.0.1:8501`. Restrict the server firewall to SSH and the proxy's HTTPS ports. Do not expose PostgreSQL or MongoDB ports to the internet. The app currently has demo persona sign-in, so place it behind access control if it is not meant to be public.

Use a dedicated server account and encrypted disk backups. Keep PostgreSQL/MongoDB secrets and database backups out of Git. This stack does not create a cloud server, DNS record, TLS certificate, or provider account; those depend on the host and domain chosen for deployment.
