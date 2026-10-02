-- Why a classified reply needs a closer look (failed checks or low confidence), shown in Slack.
alter table replies add column if not exists review_note text;
