-- Companies whose site refuses automated visitors (bot check): contact them by hand.
alter table companies add column if not exists manual_reason text;
