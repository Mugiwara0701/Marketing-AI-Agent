-- What kind of organisation a lead is (agent/leadgen/classify.py) and how strong a customer it looks:
-- result_type POTENTIAL_CUSTOMER / JOB_POSTING / FREELANCE_PROJECT / HARDWARE_MANUFACTURER ...,
-- customer_tier high (builds a product AND shows an engineering need) / potential / investigate.

alter table companies
  add column if not exists result_type text,
  add column if not exists customer_tier text;
