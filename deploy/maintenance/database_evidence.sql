\set ON_ERROR_STOP on
SET statement_timeout = '60s';
SET lock_timeout = '3s';
SET timezone = 'UTC';
SET extra_float_digits = 3;
SET bytea_output = 'hex';

-- Emit counts and aggregate fingerprints only; never emit application rows,
-- role definitions, role/password hashes, connection strings or secrets.
SELECT format(
  'SELECT %L || ''|'' || count(*) || ''|'' || md5(COALESCE(string_agg(h, '''' ORDER BY h), '''')) FROM (SELECT md5(row_to_json(t)::text) AS h FROM %I.%I AS t) AS fingerprints;',
  'TABLE|' || n.nspname || '.' || c.relname, n.nspname, c.relname
)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm')
  AND n.nspname !~ '^(pg_|_timescaledb_|timescaledb_)'
  AND n.nspname <> 'information_schema'
  AND NOT EXISTS (
    SELECT 1 FROM pg_depend d
    WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
      AND d.refclassid = 'pg_extension'::regclass AND d.deptype = 'e'
  )
ORDER BY n.nspname, c.relname
\gexec

SELECT 'INDEX|' || n.nspname || '.' || c.relname || '|' || i.relname || '|' || x.indisvalid || '|' || x.indisready || '|' || md5(pg_get_indexdef(i.oid))
FROM pg_index x
JOIN pg_class c ON c.oid = x.indrelid
JOIN pg_class i ON i.oid = x.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname !~ '^(pg_|_timescaledb_|timescaledb_)'
  AND n.nspname <> 'information_schema'
  AND NOT EXISTS (
    SELECT 1 FROM pg_depend d
    WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
      AND d.refclassid = 'pg_extension'::regclass AND d.deptype = 'e'
  )
ORDER BY n.nspname, c.relname, i.relname;

SELECT 'CONSTRAINT|' || n.nspname || '.' || c.relname || '|' || x.conname || '|' || x.convalidated || '|' || md5(pg_get_constraintdef(x.oid))
FROM pg_constraint x
JOIN pg_class c ON c.oid = x.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname !~ '^(pg_|_timescaledb_|timescaledb_)'
  AND n.nspname <> 'information_schema'
  AND NOT EXISTS (
    SELECT 1 FROM pg_depend d
    WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
      AND d.refclassid = 'pg_extension'::regclass AND d.deptype = 'e'
  )
ORDER BY n.nspname, c.relname, x.conname;

SELECT 'HYPERTABLE|' || hypertable_schema || '.' || hypertable_name || '|' || num_dimensions || '|' || num_chunks || '|' || compression_enabled
FROM timescaledb_information.hypertables
ORDER BY hypertable_schema, hypertable_name;

SELECT 'CHUNK|' || hypertable_schema || '.' || hypertable_name || '|' || chunk_schema || '.' || chunk_name || '|' || is_compressed
FROM timescaledb_information.chunks
ORDER BY hypertable_schema, hypertable_name, chunk_schema, chunk_name;

SELECT 'SEQUENCE_DEFINITION|' || n.nspname || '.' || c.relname || '|' || md5(concat_ws('|', s.seqstart, s.seqincrement, s.seqmax, s.seqmin, s.seqcache, s.seqcycle))
FROM pg_sequence s
JOIN pg_class c ON c.oid = s.seqrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname !~ '^(pg_|_timescaledb_|timescaledb_)'
ORDER BY n.nspname, c.relname;

SELECT 'LARGE_OBJECTS|' || count(*) FROM pg_largeobject_metadata;
