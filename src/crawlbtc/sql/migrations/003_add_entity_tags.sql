-- Known-entity reference tags (exchanges, mixers, sanctioned/OFAC, etc.).
-- Separate from watch_addresses: this is external reference data, not the
-- addresses you actively track. The (address, source) primary key lets the
-- same address be labelled by several sources (e.g. OFAC AND your own note).
-- Safe to run repeatedly. Created by the owner; the schema's default
-- privileges grant the app role SELECT/INSERT/UPDATE/DELETE automatically.
CREATE TABLE IF NOT EXISTS blockchain.entity_tags (
    address      text NOT NULL,
    entity_name  text NOT NULL,
    category     text NOT NULL,        -- exchange|mixer|gambling|sanctioned|mining|service|other
    source       text NOT NULL,        -- OFAC | builtin | user | <tagpack name> ...  ("which list")
    confidence   real,                 -- 0..1; OFAC=1.0, community lower
    reference    text,                 -- URL / citation / program
    added_at     timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT entity_tags_pkey PRIMARY KEY (address, source)
);
CREATE INDEX IF NOT EXISTS entity_tags_address_idx  ON blockchain.entity_tags (address);
CREATE INDEX IF NOT EXISTS entity_tags_category_idx ON blockchain.entity_tags (category);
CREATE INDEX IF NOT EXISTS entity_tags_source_idx   ON blockchain.entity_tags (source);
