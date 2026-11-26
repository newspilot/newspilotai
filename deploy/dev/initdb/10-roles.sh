#!/bin/sh
# Runs once, when the dev database volume is first created. Passwords come from the container environment
# (deploy/dev/.env), never from this file.
set -eu

psql -v ON_ERROR_STOP=1 --username postgres \
  -v owner_pw="$NEWSPILOT_OWNER_PASSWORD" -v app_pw="$NEWSPILOT_APP_PASSWORD" <<'SQL'
CREATE ROLE newspilot_owner LOGIN PASSWORD :'owner_pw';
CREATE ROLE newspilot_app LOGIN PASSWORD :'app_pw' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
CREATE DATABASE newspilot OWNER newspilot_owner;
REVOKE ALL ON DATABASE newspilot FROM PUBLIC;
GRANT CONNECT ON DATABASE newspilot TO newspilot_app;
SQL

psql -v ON_ERROR_STOP=1 --username postgres --dbname newspilot <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO newspilot_owner;
SQL
