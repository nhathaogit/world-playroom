"""Bounded output allowance for JSON scenes, including hidden thinking tokens."""


def json_output_budget(model, purpose, requested, options, exhausted=False):
    # Namespaced APIZ routes and official routes share the same Gemini policy.
    thinking_scene = (purpose in {'scene', 'scene repair'}
                      and model.removeprefix('apiz:').startswith('gemini-')
                      and options.get('reasoning_effort') in {'low', 'medium', 'high'})
    if not thinking_scene:
        return requested
    # This is a ceiling, not a prose target or pre-billed allocation. Preserve
    # the user's thinking level and leave room for the complete rich scene JSON.
    budget = max(requested, 12288)
    if exhausted:
        budget = max(budget, min(requested * 2, 16384))
    return budget
