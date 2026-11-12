set -eu

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=workflow_password="$PROPCLUE_WORKFLOW_PASSWORD" \
  --set=source_password="$PROPCLUE_SOURCE_READONLY_PASSWORD" <<'SQL'
CREATE ROLE propclue_app LOGIN PASSWORD :'workflow_password';
CREATE DATABASE propclue_demo_workflow OWNER propclue_app;
CREATE ROLE propclue_reader LOGIN PASSWORD :'source_password';
GRANT CONNECT ON DATABASE propcluev1 TO propclue_reader;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT SELECT ON TABLES TO propclue_reader;
SQL
