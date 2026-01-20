CREATE TABLE IF NOT EXISTS accounts (
    account_id SERIAL PRIMARY KEY,
    account_name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS sales (
    sale_id SERIAL PRIMARY KEY,
    account_id INT REFERENCES accounts(account_id),
    sale_date DATE NOT NULL,
    amount NUMERIC(12,2) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_account_date ON sales(account_id, sale_date);
