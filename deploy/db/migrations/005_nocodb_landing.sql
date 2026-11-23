-- Records pulled from NocoDB as-is (one row per record, all fields as JSON),
-- the input of the NocoDB -> core transform (birddog/nocodb_import.py).
-- Kept after the import as the original copy.
CREATE TABLE IF NOT EXISTS raw.nocodb_record (
    table_name  text   NOT NULL,          -- pages / documents / ftp / locations
    nocodb_id   bigint NOT NULL,
    fields      jsonb  NOT NULL,
    landed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_name, nocodb_id)
);
