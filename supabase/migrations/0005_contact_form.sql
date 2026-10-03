-- Companies with no public email but a contact form: remember the form so a human can submit it.
alter table companies add column if not exists contact_form_url text;
