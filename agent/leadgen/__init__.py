"""B2B lead pipeline: find companies that may BUY AOSP / BSP / embedded Linux engineering, and pitch them only after
a person approved the email.

    strategy.py     search queries: technology x business intent x product domain
    browser/        browser control (http | chrome | desktop) behind one interface, with a politeness guard
    extract.py      page -> text, title, links
    intent.py       what a page is (shop, distributor, job board, docs, request, product company), no model
    qualify.py      rules + one LLM reading (tasks/assess.py) + code checks -> Verdict
    scoring.py      0-100 score in six dimensions with penalties
    identity.py     one company = one lead (domain, normalized name), canonical URLs
    contacts.py     business contact from the company's own site, decision makers first
    outreach.py     evidence-based email draft
    approval.py     Slack (or simulated) approval request; decisions recorded with who / when
    sender.py       the only send path; refuses anything not approved
    repository/     Postgres (production) or SQLite (dry-run, tests)
    pipeline.py     the orchestrator; service.py wires it for the CLI and the daily run

Lead states: DISCOVERED -> QUALIFIED -> CONTACT_FOUND -> EMAIL_DRAFTED -> PENDING_APPROVAL -> APPROVED -> SENT,
with REJECTED and FAILED (models.TRANSITIONS).
"""
