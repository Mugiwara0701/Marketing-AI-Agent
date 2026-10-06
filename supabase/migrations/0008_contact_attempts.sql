-- How often contact discovery failed for a lead. The pipeline stops retrying after contacts.max_attempts
-- (config/leadgen.yaml) instead of reopening a dead or contact-less site on every run.

alter table companies add column if not exists contact_attempts int not null default 0;
