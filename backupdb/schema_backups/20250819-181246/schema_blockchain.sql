--
-- PostgreSQL database dump
--

-- Dumped from database version 16.9 (Ubuntu 16.9-0ubuntu0.24.04.1)
-- Dumped by pg_dump version 16.9 (Ubuntu 16.9-0ubuntu0.24.04.1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

DROP TRIGGER IF EXISTS "watch_addresses_touch" ON "blockchain"."watch_addresses";
DROP INDEX IF EXISTS "blockchain"."watch_addresses_tags_gin";
DROP INDEX IF EXISTS "blockchain"."watch_addresses_status_idx";
DROP INDEX IF EXISTS "blockchain"."watch_addresses_last_seen_idx";
DROP INDEX IF EXISTS "blockchain"."watch_addresses_addr_type_idx";
DROP INDEX IF EXISTS "blockchain"."txio_address_io_idx";
DROP INDEX IF EXISTS "blockchain"."transactions_block_hash_idx";
DROP INDEX IF EXISTS "blockchain"."spends_spent_time_idx";
DROP INDEX IF EXISTS "blockchain"."spends_spent_height_idx";
DROP INDEX IF EXISTS "blockchain"."spends_spending_idx";
DROP INDEX IF EXISTS "blockchain"."idx_txio_address_io";
DROP INDEX IF EXISTS "blockchain"."idx_transactions_received_time";
DROP INDEX IF EXISTS "blockchain"."idx_transactions_block_hash";
DROP INDEX IF EXISTS "blockchain"."idx_transaction_io_type_io";
DROP INDEX IF EXISTS "blockchain"."idx_transaction_io_txid_io_idx";
DROP INDEX IF EXISTS "blockchain"."idx_transaction_io_txid_idx";
DROP INDEX IF EXISTS "blockchain"."idx_transaction_io_txid";
DROP INDEX IF EXISTS "blockchain"."idx_transaction_io_io_type";
DROP INDEX IF EXISTS "blockchain"."idx_transaction_io_address_type";
DROP INDEX IF EXISTS "blockchain"."idx_transaction_io_address_and_type";
DROP INDEX IF EXISTS "blockchain"."idx_transaction_io_address";
DROP INDEX IF EXISTS "blockchain"."idx_block_jobs_updated_at";
DROP INDEX IF EXISTS "blockchain"."idx_block_jobs_updated";
DROP INDEX IF EXISTS "blockchain"."idx_block_jobs_status_features";
DROP INDEX IF EXISTS "blockchain"."idx_block_jobs_status";
DROP INDEX IF EXISTS "blockchain"."idx_block_jobs_height_status";
DROP INDEX IF EXISTS "blockchain"."idx_block_jobs_features";
DROP INDEX IF EXISTS "blockchain"."block_jobs_address_status_idx";
DROP INDEX IF EXISTS "blockchain"."block_jobs_addr_sched_idx";
ALTER TABLE IF EXISTS ONLY "blockchain"."watch_addresses" DROP CONSTRAINT IF EXISTS "watch_addresses_pkey";
ALTER TABLE IF EXISTS ONLY "blockchain"."transactions" DROP CONSTRAINT IF EXISTS "transactions_txid_key";
ALTER TABLE IF EXISTS ONLY "blockchain"."transactions" DROP CONSTRAINT IF EXISTS "transactions_pkey";
ALTER TABLE IF EXISTS ONLY "blockchain"."transaction_io" DROP CONSTRAINT IF EXISTS "transaction_io_unique_txid_type_idx";
ALTER TABLE IF EXISTS ONLY "blockchain"."transaction_io" DROP CONSTRAINT IF EXISTS "transaction_io_pkey";
ALTER TABLE IF EXISTS ONLY "blockchain"."spends" DROP CONSTRAINT IF EXISTS "spends_spending_txid_spending_vin_key";
ALTER TABLE IF EXISTS ONLY "blockchain"."spends" DROP CONSTRAINT IF EXISTS "spends_pkey";
ALTER TABLE IF EXISTS ONLY "blockchain"."blocks" DROP CONSTRAINT IF EXISTS "blocks_pkey";
ALTER TABLE IF EXISTS ONLY "blockchain"."blocks" DROP CONSTRAINT IF EXISTS "blocks_height_key";
ALTER TABLE IF EXISTS ONLY "blockchain"."blocks" DROP CONSTRAINT IF EXISTS "blocks_block_hash_key";
ALTER TABLE IF EXISTS ONLY "blockchain"."block_jobs" DROP CONSTRAINT IF EXISTS "block_jobs_pkey";
DROP TABLE IF EXISTS "blockchain"."watch_addresses";
DROP TABLE IF EXISTS "blockchain"."transactions";
DROP SEQUENCE IF EXISTS "blockchain"."transactions_id_seq";
DROP TABLE IF EXISTS "blockchain"."transaction_io";
DROP SEQUENCE IF EXISTS "blockchain"."transaction_io_id_seq";
DROP TABLE IF EXISTS "blockchain"."spends";
DROP TABLE IF EXISTS "blockchain"."blocks";
DROP SEQUENCE IF EXISTS "blockchain"."blocks_id_seq";
DROP TABLE IF EXISTS "blockchain"."block_jobs";
DROP FUNCTION IF EXISTS "blockchain"."touch_updated_at"();
DROP TYPE IF EXISTS "blockchain"."tx_type";
DROP TYPE IF EXISTS "blockchain"."tx_io_type";
DROP TYPE IF EXISTS "blockchain"."block_job_status";
DROP TYPE IF EXISTS "blockchain"."block_feature";
DROP TYPE IF EXISTS "blockchain"."balance_job_status";
DROP TYPE IF EXISTS "blockchain"."address_type";
DROP SCHEMA IF EXISTS "blockchain";
--
-- Name: blockchain; Type: SCHEMA; Schema: -; Owner: pgadmin
--

CREATE SCHEMA "blockchain";


ALTER SCHEMA "blockchain" OWNER TO "pgadmin";

--
-- Name: address_type; Type: TYPE; Schema: blockchain; Owner: pgadmin
--

CREATE TYPE "blockchain"."address_type" AS ENUM (
    'p2pkh',
    'p2sh',
    'bech32',
    'taproot',
    'unknown'
);


ALTER TYPE "blockchain"."address_type" OWNER TO "pgadmin";

--
-- Name: balance_job_status; Type: TYPE; Schema: blockchain; Owner: pgadmin
--

CREATE TYPE "blockchain"."balance_job_status" AS ENUM (
    'pending',
    'in_progress',
    'done',
    'failed'
);


ALTER TYPE "blockchain"."balance_job_status" OWNER TO "pgadmin";

--
-- Name: block_feature; Type: TYPE; Schema: blockchain; Owner: pgadmin
--

CREATE TYPE "blockchain"."block_feature" AS ENUM (
    'vin',
    'vout',
    'both',
    'none',
    'op_return_only',
    'coinbase_only'
);


ALTER TYPE "blockchain"."block_feature" OWNER TO "pgadmin";

--
-- Name: block_job_status; Type: TYPE; Schema: blockchain; Owner: pgadmin
--

CREATE TYPE "blockchain"."block_job_status" AS ENUM (
    'pending',
    'in_progress',
    'done',
    'skipped',
    'failed'
);


ALTER TYPE "blockchain"."block_job_status" OWNER TO "pgadmin";

--
-- Name: tx_io_type; Type: TYPE; Schema: blockchain; Owner: pgadmin
--

CREATE TYPE "blockchain"."tx_io_type" AS ENUM (
    'in',
    'out'
);


ALTER TYPE "blockchain"."tx_io_type" OWNER TO "pgadmin";

--
-- Name: tx_type; Type: TYPE; Schema: blockchain; Owner: pgadmin
--

CREATE TYPE "blockchain"."tx_type" AS ENUM (
    'standard',
    'coinbase',
    'segwit',
    'unknown'
);


ALTER TYPE "blockchain"."tx_type" OWNER TO "pgadmin";

--
-- Name: touch_updated_at(); Type: FUNCTION; Schema: blockchain; Owner: pgadmin
--

CREATE FUNCTION "blockchain"."touch_updated_at"() RETURNS "trigger"
    LANGUAGE "plpgsql"
    AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END$$;


ALTER FUNCTION "blockchain"."touch_updated_at"() OWNER TO "pgadmin";

SET default_tablespace = '';

SET default_table_access_method = "heap";

--
-- Name: block_jobs; Type: TABLE; Schema: blockchain; Owner: pgadmin
--

CREATE TABLE "blockchain"."block_jobs" (
    "height" integer NOT NULL,
    "status" "blockchain"."block_job_status" DEFAULT 'pending'::"blockchain"."block_job_status" NOT NULL,
    "updated_at" timestamp without time zone DEFAULT "now"(),
    "features" "blockchain"."block_feature" DEFAULT 'none'::"blockchain"."block_feature" NOT NULL,
    "vout_status" "blockchain"."block_job_status" DEFAULT 'pending'::"blockchain"."block_job_status" NOT NULL,
    "vin_status" "blockchain"."block_job_status" DEFAULT 'pending'::"blockchain"."block_job_status" NOT NULL,
    "address_status" "blockchain"."block_job_status" DEFAULT 'pending'::"blockchain"."block_job_status" NOT NULL
);


ALTER TABLE "blockchain"."block_jobs" OWNER TO "pgadmin";

--
-- Name: blocks_id_seq; Type: SEQUENCE; Schema: blockchain; Owner: pgadmin
--

CREATE SEQUENCE "blockchain"."blocks_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "blockchain"."blocks_id_seq" OWNER TO "pgadmin";

--
-- Name: blocks; Type: TABLE; Schema: blockchain; Owner: pgadmin
--

CREATE TABLE "blockchain"."blocks" (
    "id" bigint DEFAULT "nextval"('"blockchain"."blocks_id_seq"'::"regclass") NOT NULL,
    "block_hash" "text" NOT NULL,
    "height" integer NOT NULL,
    "timestamp" timestamp without time zone NOT NULL,
    "processed_at" timestamp without time zone DEFAULT "now"()
);


ALTER TABLE "blockchain"."blocks" OWNER TO "pgadmin";

--
-- Name: spends; Type: TABLE; Schema: blockchain; Owner: pgadmin
--

CREATE TABLE "blockchain"."spends" (
    "prev_txid" "text" NOT NULL,
    "prev_vout" integer NOT NULL,
    "spending_txid" "text" NOT NULL,
    "spending_vin" integer NOT NULL,
    "spent_height" integer,
    "spent_block" "text",
    "spent_time" timestamp with time zone
);


ALTER TABLE "blockchain"."spends" OWNER TO "pgadmin";

--
-- Name: transaction_io_id_seq; Type: SEQUENCE; Schema: blockchain; Owner: pgadmin
--

CREATE SEQUENCE "blockchain"."transaction_io_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "blockchain"."transaction_io_id_seq" OWNER TO "pgadmin";

--
-- Name: transaction_io; Type: TABLE; Schema: blockchain; Owner: pgadmin
--

CREATE TABLE "blockchain"."transaction_io" (
    "id" bigint DEFAULT "nextval"('"blockchain"."transaction_io_id_seq"'::"regclass") NOT NULL,
    "txid" "text" NOT NULL,
    "address" "text",
    "amount" bigint,
    "io_type" "blockchain"."tx_io_type" NOT NULL,
    "idx" integer NOT NULL,
    "address_type" "blockchain"."address_type" DEFAULT 'unknown'::"blockchain"."address_type"
);


ALTER TABLE "blockchain"."transaction_io" OWNER TO "pgadmin";

--
-- Name: transactions_id_seq; Type: SEQUENCE; Schema: blockchain; Owner: pgadmin
--

CREATE SEQUENCE "blockchain"."transactions_id_seq"
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE "blockchain"."transactions_id_seq" OWNER TO "pgadmin";

--
-- Name: transactions; Type: TABLE; Schema: blockchain; Owner: pgadmin
--

CREATE TABLE "blockchain"."transactions" (
    "id" bigint DEFAULT "nextval"('"blockchain"."transactions_id_seq"'::"regclass") NOT NULL,
    "txid" "text" NOT NULL,
    "block_hash" "text",
    "received_time" timestamp without time zone NOT NULL,
    "total_in" bigint,
    "total_out" bigint,
    "tx_type" "blockchain"."tx_type" DEFAULT 'unknown'::"blockchain"."tx_type"
);


ALTER TABLE "blockchain"."transactions" OWNER TO "pgadmin";

--
-- Name: watch_addresses; Type: TABLE; Schema: blockchain; Owner: pgadmin
--

CREATE TABLE "blockchain"."watch_addresses" (
    "address" "text" NOT NULL,
    "address_type" "blockchain"."address_type",
    "public_key_hex" "text",
    "private_key_wif" "text",
    "private_key_hex" "text",
    "derivation_path" "text",
    "label" "text",
    "tags" "jsonb" DEFAULT '[]'::"jsonb" NOT NULL,
    "first_seen" timestamp with time zone,
    "last_seen" timestamp with time zone,
    "tx_count" integer DEFAULT 0 NOT NULL,
    "utxo_count" integer DEFAULT 0 NOT NULL,
    "balance_sats" bigint DEFAULT 0 NOT NULL,
    "last_scanned_height" integer,
    "last_scanned_time" timestamp with time zone,
    "last_scanned_txid" "text",
    "balance_status" "blockchain"."balance_job_status" DEFAULT 'pending'::"blockchain"."balance_job_status" NOT NULL,
    "created_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    "updated_at" timestamp with time zone DEFAULT "now"() NOT NULL,
    CONSTRAINT "watch_addresses_balance_sats_check" CHECK (("balance_sats" >= 0)),
    CONSTRAINT "watch_addresses_tags_check" CHECK (("jsonb_typeof"("tags") = 'array'::"text")),
    CONSTRAINT "watch_addresses_tx_count_check" CHECK (("tx_count" >= 0)),
    CONSTRAINT "watch_addresses_utxo_count_check" CHECK (("utxo_count" >= 0))
);


ALTER TABLE "blockchain"."watch_addresses" OWNER TO "pgadmin";

--
-- Name: block_jobs block_jobs_pkey; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."block_jobs"
    ADD CONSTRAINT "block_jobs_pkey" PRIMARY KEY ("height");


--
-- Name: blocks blocks_block_hash_key; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."blocks"
    ADD CONSTRAINT "blocks_block_hash_key" UNIQUE ("block_hash");


--
-- Name: blocks blocks_height_key; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."blocks"
    ADD CONSTRAINT "blocks_height_key" UNIQUE ("height");


--
-- Name: blocks blocks_pkey; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."blocks"
    ADD CONSTRAINT "blocks_pkey" PRIMARY KEY ("id");


--
-- Name: spends spends_pkey; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."spends"
    ADD CONSTRAINT "spends_pkey" PRIMARY KEY ("prev_txid", "prev_vout");


--
-- Name: spends spends_spending_txid_spending_vin_key; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."spends"
    ADD CONSTRAINT "spends_spending_txid_spending_vin_key" UNIQUE ("spending_txid", "spending_vin");


--
-- Name: transaction_io transaction_io_pkey; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."transaction_io"
    ADD CONSTRAINT "transaction_io_pkey" PRIMARY KEY ("id");


--
-- Name: transaction_io transaction_io_unique_txid_type_idx; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."transaction_io"
    ADD CONSTRAINT "transaction_io_unique_txid_type_idx" UNIQUE ("txid", "io_type", "idx");


--
-- Name: transactions transactions_pkey; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."transactions"
    ADD CONSTRAINT "transactions_pkey" PRIMARY KEY ("id");


--
-- Name: transactions transactions_txid_key; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."transactions"
    ADD CONSTRAINT "transactions_txid_key" UNIQUE ("txid");


--
-- Name: watch_addresses watch_addresses_pkey; Type: CONSTRAINT; Schema: blockchain; Owner: pgadmin
--

ALTER TABLE ONLY "blockchain"."watch_addresses"
    ADD CONSTRAINT "watch_addresses_pkey" PRIMARY KEY ("address");


--
-- Name: block_jobs_addr_sched_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "block_jobs_addr_sched_idx" ON "blockchain"."block_jobs" USING "btree" ("height") WHERE (("address_status" = 'pending'::"blockchain"."block_job_status") AND ("vout_status" = ANY (ARRAY['done'::"blockchain"."block_job_status", 'skipped'::"blockchain"."block_job_status"])) AND ("vin_status" = 'done'::"blockchain"."block_job_status"));


--
-- Name: block_jobs_address_status_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "block_jobs_address_status_idx" ON "blockchain"."block_jobs" USING "btree" ("address_status", "height");


--
-- Name: idx_block_jobs_features; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_block_jobs_features" ON "blockchain"."block_jobs" USING "btree" ("features");


--
-- Name: idx_block_jobs_height_status; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_block_jobs_height_status" ON "blockchain"."block_jobs" USING "btree" ("height", "status");


--
-- Name: idx_block_jobs_status; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_block_jobs_status" ON "blockchain"."block_jobs" USING "btree" ("status");


--
-- Name: idx_block_jobs_status_features; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_block_jobs_status_features" ON "blockchain"."block_jobs" USING "btree" ("status", "features");


--
-- Name: idx_block_jobs_updated; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_block_jobs_updated" ON "blockchain"."block_jobs" USING "btree" ("updated_at" DESC);


--
-- Name: idx_block_jobs_updated_at; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_block_jobs_updated_at" ON "blockchain"."block_jobs" USING "btree" ("updated_at");


--
-- Name: idx_transaction_io_address; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transaction_io_address" ON "blockchain"."transaction_io" USING "btree" ("address");


--
-- Name: idx_transaction_io_address_and_type; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transaction_io_address_and_type" ON "blockchain"."transaction_io" USING "btree" ("address", "address_type");


--
-- Name: idx_transaction_io_address_type; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transaction_io_address_type" ON "blockchain"."transaction_io" USING "btree" ("address", "io_type");


--
-- Name: idx_transaction_io_io_type; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transaction_io_io_type" ON "blockchain"."transaction_io" USING "btree" ("io_type");


--
-- Name: idx_transaction_io_txid; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transaction_io_txid" ON "blockchain"."transaction_io" USING "btree" ("txid");


--
-- Name: idx_transaction_io_txid_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transaction_io_txid_idx" ON "blockchain"."transaction_io" USING "btree" ("txid", "idx");


--
-- Name: idx_transaction_io_txid_io_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transaction_io_txid_io_idx" ON "blockchain"."transaction_io" USING "btree" ("txid", "io_type", "idx");


--
-- Name: idx_transaction_io_type_io; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transaction_io_type_io" ON "blockchain"."transaction_io" USING "btree" ("address_type", "io_type");


--
-- Name: idx_transactions_block_hash; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transactions_block_hash" ON "blockchain"."transactions" USING "btree" ("block_hash");


--
-- Name: idx_transactions_received_time; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_transactions_received_time" ON "blockchain"."transactions" USING "btree" ("received_time");


--
-- Name: idx_txio_address_io; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "idx_txio_address_io" ON "blockchain"."transaction_io" USING "btree" ("address", "io_type");


--
-- Name: spends_spending_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "spends_spending_idx" ON "blockchain"."spends" USING "btree" ("spending_txid", "spending_vin");


--
-- Name: spends_spent_height_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "spends_spent_height_idx" ON "blockchain"."spends" USING "btree" ("spent_height");


--
-- Name: spends_spent_time_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "spends_spent_time_idx" ON "blockchain"."spends" USING "btree" ("spent_time");


--
-- Name: transactions_block_hash_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "transactions_block_hash_idx" ON "blockchain"."transactions" USING "btree" ("block_hash");


--
-- Name: txio_address_io_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "txio_address_io_idx" ON "blockchain"."transaction_io" USING "btree" ("address", "io_type");


--
-- Name: watch_addresses_addr_type_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "watch_addresses_addr_type_idx" ON "blockchain"."watch_addresses" USING "btree" ("address_type");


--
-- Name: watch_addresses_last_seen_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "watch_addresses_last_seen_idx" ON "blockchain"."watch_addresses" USING "btree" ("last_seen" DESC);


--
-- Name: watch_addresses_status_idx; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "watch_addresses_status_idx" ON "blockchain"."watch_addresses" USING "btree" ("balance_status", "last_scanned_height");


--
-- Name: watch_addresses_tags_gin; Type: INDEX; Schema: blockchain; Owner: pgadmin
--

CREATE INDEX "watch_addresses_tags_gin" ON "blockchain"."watch_addresses" USING "gin" ("tags");


--
-- Name: watch_addresses watch_addresses_touch; Type: TRIGGER; Schema: blockchain; Owner: pgadmin
--

CREATE TRIGGER "watch_addresses_touch" BEFORE UPDATE ON "blockchain"."watch_addresses" FOR EACH ROW EXECUTE FUNCTION "blockchain"."touch_updated_at"();


--
-- Name: SCHEMA "blockchain"; Type: ACL; Schema: -; Owner: pgadmin
--

GRANT USAGE ON SCHEMA "blockchain" TO "blockchain";


--
-- Name: TABLE "block_jobs"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "blockchain"."block_jobs" TO "blockchain";


--
-- Name: SEQUENCE "blocks_id_seq"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,USAGE ON SEQUENCE "blockchain"."blocks_id_seq" TO "blockchain";


--
-- Name: TABLE "blocks"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "blockchain"."blocks" TO "blockchain";


--
-- Name: TABLE "spends"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "blockchain"."spends" TO "blockchain";


--
-- Name: SEQUENCE "transaction_io_id_seq"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,USAGE ON SEQUENCE "blockchain"."transaction_io_id_seq" TO "blockchain";


--
-- Name: TABLE "transaction_io"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "blockchain"."transaction_io" TO "blockchain";


--
-- Name: SEQUENCE "transactions_id_seq"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,USAGE ON SEQUENCE "blockchain"."transactions_id_seq" TO "blockchain";


--
-- Name: TABLE "transactions"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "blockchain"."transactions" TO "blockchain";


--
-- Name: TABLE "watch_addresses"; Type: ACL; Schema: blockchain; Owner: pgadmin
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE "blockchain"."watch_addresses" TO "blockchain";


--
-- Name: DEFAULT PRIVILEGES FOR SEQUENCES; Type: DEFAULT ACL; Schema: blockchain; Owner: pgadmin
--

ALTER DEFAULT PRIVILEGES FOR ROLE "pgadmin" IN SCHEMA "blockchain" GRANT SELECT,USAGE ON SEQUENCES TO "blockchain";


--
-- Name: DEFAULT PRIVILEGES FOR TABLES; Type: DEFAULT ACL; Schema: blockchain; Owner: pgadmin
--

ALTER DEFAULT PRIVILEGES FOR ROLE "pgadmin" IN SCHEMA "blockchain" GRANT SELECT,INSERT,DELETE,UPDATE ON TABLES TO "blockchain";


--
-- PostgreSQL database dump complete
--

