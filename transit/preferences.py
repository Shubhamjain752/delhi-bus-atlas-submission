"""Choose which feasible bus journeys to show together."""


def balanced_selection(choices, limit):
    """Input is sorted by arrival, then walking. Times are seconds from departure."""
    if not choices:
        return [], {"primary_count": 0}

    fastest, fastest_walk, changes = choices[0]
    less_walk = next((i for i, (_, walk, n) in enumerate(choices)
                      if fastest_walk - walk >= 300 and n <= changes), None)
    primary = [0]
    policy = {}
    if less_walk is not None:
        alternative, alternative_walk, _ = choices[less_walk]
        gain = alternative - fastest
        if 10 * gain < alternative:
            practical = []
            for i in range(less_walk, len(choices)):
                duration, walk, n = choices[i]
                if 10 * (duration - fastest) >= duration:
                    break
                if fastest_walk-walk < 300 or n > changes:
                    continue
                # Skip this route if an earlier choice already saves as much
                # walking with the same number of changes or fewer.
                if any(choices[j][1] <= walk and choices[j][2] <= n for j in practical):
                    continue
                practical.append(i)
                if len(practical) == 3:
                    break
            primary = practical if len(practical) == 3 else [less_walk, 0, *practical[1:]]
        else:
            primary = [0, less_walk]
        policy.update(time_saved_seconds=gain, extra_walk_seconds=fastest_walk-alternative_walk)

    direct = next((i for i, (_, _, n) in enumerate(choices) if n == 0), None)
    if len(primary) < 3 and direct is not None and all(choices[i][2] for i in primary):
        primary.append(direct)
    # Give a distinct walking or transfer trade-off priority before filling
    # the remaining places in order of arrival.
    for i, (_, walk, n) in enumerate(choices):
        if len(primary) == 3:
            break
        duration = choices[i][0]
        if 10 * (duration-fastest) < duration and all(walk <= choices[j][1] - 300 and n <= choices[j][2] for j in primary):
            primary.append(i)
    priority = dict.fromkeys([*primary, 0, direct, *range(len(choices))])
    selected = [i for i in priority if i is not None][:min(limit, 5)]
    policy["primary_count"] = min(len(selected), 3)
    if less_walk is not None:
        if 0 in selected:
            policy["faster_index"] = selected.index(0)
        if less_walk in selected:
            policy["less_walking_index"] = selected.index(less_walk)
    return selected, policy
