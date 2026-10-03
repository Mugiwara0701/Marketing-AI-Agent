"""GUI computer-use agent: a vision model drives the sandbox desktop (Chrome) through screenshots and
virtual mouse/keyboard, only as a fallback when the scripted path cannot find a contact.

executor_client  talks to sandbox/executor.py            (Phase 3)
actions          the structured step the model returns    (Phase 3)
loop             screenshot -> model -> action, repeated  (Phase 3)
policy           budgets, kill switch, key/URL rules, log (Phase 4)
lead             find a company's contact, then reuse the normal draft -> Slack approval flow (Phase 5)
"""
