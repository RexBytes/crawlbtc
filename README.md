# crawlbtc

A Bitcoin blockchain data extraction pipeline. This project connects to a local Bitcoin Core node via RPC, extracts structured data (blocks, transactions, inputs/outputs), and stores it in a PostgreSQL database for indexing, analysis, or research.

---

## 🧰 Requirements

- Python 3.8+
- **Native PostgreSQL installation** (tested with PostgreSQL 13+)
- A synced Bitcoin Core node with RPC enabled

> 📝 This project assumes you're using a **native PostgreSQL installation** on your local system (e.g., via `apt`, `brew`, or the official installer), not Docker or cloud-hosted PostgreSQL.

Install Python dependencies:

```bash
pip install -r requirements.txt
````

---

## 🔧 Environment Variables

Create a `.env` file in the project root with the following content:

```
RPC_HOST=127.0.0.1
RPC_PORT=8332
RPC_USER=bitcoin
RPC_PASSWORD=yourpassword

PG_HOST=localhost
PG_PORT=5432
PG_DB=crawlbtc
PG_USER=blockchain
PG_PASSWORD=yourdbpassword

LOG_LEVEL=progress
```

---

## 🗄️ Database Setup

1. Create your PostgreSQL database (if not already created):

```bash
createdb -U pgadmin crawlbtc
```

2. Initialize the schema:

```bash
psql -U pgadmin -d crawlbtc -f db/init_all.sql
```

This will:

* Create the `blockchain` schema
* Set up all required tables, indexes, and permissions

> Make sure the PostgreSQL client tools (e.g. `psql`, `createdb`) are available on your system PATH.

---

## 🚀 Script Pipeline

| Step | Script                    | Description                                                           |
| ---- | ------------------------- | --------------------------------------------------------------------- |
| 1️⃣  | `01_extract_blocks.py`    | Extracts blocks, transactions, and output values into the database    |
| 2️⃣  | `02_decode_inputs.py`     | Resolves `vin` inputs and calculates `total_in` from previous outputs |
| 3️⃣  | `03_extract_addresses.py` | Extracts all unique public addresses from transaction outputs         |
| 4️⃣  | `04_compute_balances.py`  | Computes address balances from UTXO data (outputs – spent inputs)     |

Run each script in order once your database is initialized and your `.env` file is set.

