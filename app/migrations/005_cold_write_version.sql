-- Version 1 is reserved for existing ClickHouse rows copied by migration 002
-- and for the fresh-table default. Every future archive or cold enrichment
-- INSERT must allocate a fresh value with nextval() while holding the archive
-- advisory lock. Sequence gaps are expected: nextval() is not transactional.
-- Never reset this sequence while a versioned cold table is in use.
CREATE SEQUENCE public.cold_write_version_seq
    AS BIGINT
    START WITH 2
    INCREMENT BY 1
    MINVALUE 2
    MAXVALUE 9223372036854775807
    CACHE 1
    NO CYCLE;
