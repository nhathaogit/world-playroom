"""One deterministic identity contract; no fuzzy assignment or invented speakers."""
import re
import unicodedata


def identity_key(value):
    value = unicodedata.normalize('NFKD', str(value).casefold().replace('đ', 'd'))
    return re.sub(r'[^a-z0-9]', '', ''.join(c for c in value if not unicodedata.combining(c)))


def resolve_speaker(reference, roster, profiled_ids, configured_aliases=None):
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError(f'Missing/invalid speaker reference: {reference!r}')
    reference = reference.strip()
    if reference in roster:
        if reference not in profiled_ids:
            raise ValueError(f'Speaker {reference!r} exists but was not in the supplied speaker manifest')
        return reference
    key = identity_key(reference)
    candidates = set()
    for npc_id in profiled_ids:
        npc = roster.get(npc_id)
        if not npc:
            continue
        aliases = [npc_id, npc.get('name', '')] + (configured_aliases or {}).get(npc_id, [])
        aliases += npc.get('aliases', []) if isinstance(npc.get('aliases'), list) else []
        keys = {identity_key(alias) for alias in aliases if isinstance(alias, str) and alias.strip()}
        # Legacy/generated 'npc-' prefix is an ID decoration, not a new identity.
        if key in keys or (reference.casefold().startswith('npc-') and identity_key(reference[4:]) in keys):
            candidates.add(npc_id)
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        raise ValueError(f'Ambiguous speaker reference {reference!r}; use one exact ID: {sorted(candidates)}')
    raise ValueError(f'Unknown speaker reference {reference!r}; supplied speaker IDs: {sorted(profiled_ids)}')
