"""The contact roles a campaign may put first (`preferred_roles` in a campaign from the Campaign Agent).

A closed vocabulary, never free text from the file (a pattern from a file would be a ReDoS and injection risk). Each name
stands for one or more ranks of contacts.ROLE_PRIORITY, so a campaign reorders the roles this agent already approaches; it
cannot add new ones. Same names as the Campaign Agent's contract (contract/model.py PreferredRole).
"""

PREFERRED_ROLES: dict[str, tuple[int, ...]] = {
    "cto": (0,),
    "vp_engineering": (1,),
    "engineering_director": (2, 4),
    "engineering_manager": (3,),
    "founder": (5, 6),
    "procurement": (8,),
}


def preferred_rank(base_rank: int | None, preferred: list[str]) -> int | None:
    """The rank to use for a person whose role has `base_rank`: before every other role when the campaign prefers it
    (in the campaign's order), otherwise unchanged."""
    if base_rank is None:
        return None
    for i, name in enumerate(preferred):
        if base_rank in PREFERRED_ROLES.get(name, ()):
            return i - len(preferred)  # negative: ahead of rank 0
    return base_rank
