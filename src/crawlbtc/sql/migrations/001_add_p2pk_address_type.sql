-- Adds the 'p2pk' address_type for Satoshi-era raw-pubkey outputs.
-- Safe to run repeatedly. Must run outside a transaction block
-- (crawlbtc migrate executes migrations with autocommit).
ALTER TYPE blockchain.address_type ADD VALUE IF NOT EXISTS 'p2pk';
