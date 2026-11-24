"""Choose a small set of practical journeys for the passenger map."""

MAX_LATER_SECONDS = 15 * 60
EXTRA_WALK_SECONDS = 5 * 60
MIN_SAVING_SECONDS = 2 * 60
MODERATE_WALK_SECONDS = 15 * 60


def shorter_walk_choices(choices, eligible):
    cutoff = min(choices[i][1] for i in eligible) + EXTRA_WALK_SECONDS
    return [i for i in eligible if choices[i][1] < cutoff], cutoff


def balanced_selection(choices, limit):
    """Input is sorted by arrival, then walking. Times are seconds from departure."""
    if not choices:
        return [], {"primary_count": 0}

    fastest = choices[0][0]
    latest = fastest + MAX_LATER_SECONDS
    fallback_latest = fastest + min(45 * 60, max(30 * 60, fastest // 2))
    nearby_in_time = [i for i, (duration, _, _) in enumerate(choices) if duration <= latest]
    short, walk_cutoff = shorter_walk_choices(choices, nearby_in_time)
    if len(short) < 3:
        # Infrequent routes may have few departures in the first 15 minutes.
        later = [i for i, (duration, walk, _) in enumerate(choices)
                 if latest < duration <= fallback_latest and walk < walk_cutoff]
        if later:
            short, _ = shorter_walk_choices(choices, [*short, *later])
    reference = short[0]
    ref_duration, ref_walk, _ = choices[reference]

    # A longer walk needs to buy time. A second one must offer something
    # different from the fastest one, such as fewer changes or less walking.
    earlier = []
    for i in nearby_in_time:
        if i >= reference:
            break
        duration, walk, changes = choices[i]
        gain = ref_duration - duration
        if gain <= 0 or (gain < MIN_SAVING_SECONDS and 10 * gain < ref_duration):
            continue
        if earlier:
            _, first_walk, first_changes = choices[earlier[0]]
            if first_walk - walk < EXTRA_WALK_SECONDS and changes >= first_changes:
                continue
        earlier.append(i)
        if len(earlier) == 2:
            break

    fast_first = bool(earlier and 10 * (ref_duration - choices[earlier[0]][0]) >= ref_duration)
    head = short[:2 if fast_first else 3]
    direct = next((i for i in short if choices[i][0] <= latest and choices[i][2] == 0), None)
    if direct is not None and all(choices[i][2] for i in head):
        head[-1] = direct

    rest = [i for i in short if i not in head]
    if fast_first:
        # Keep room for three shorter-walk services across the whole list.
        order = [earlier[0], *head, *rest[:1], *earlier[1:], *rest[1:]]
    else:
        order = [*head, *earlier, *rest]

    # A middle ground can be useful with very few short options. It must
    # arrive within the initial window and walk less than a faster choice.
    if len(short) < 3 and earlier:
        moderate = [i for i in nearby_in_time if i > reference and i not in short
                    and choices[i][1] <= MODERATE_WALK_SECONDS
                    and any(choices[i][1] < choices[j][1] for j in earlier)]
        order.extend(moderate[:3-len(short)])

    if len(order) < 3:
        # On sparse services, a later bus with an ordinary walk can still
        # help. Use it only to fill an empty place among the first three.
        backups = [i for i, (duration, walk, _) in enumerate(choices)
                   if i not in order and duration <= fallback_latest and walk <= MODERATE_WALK_SECONDS]
        order.extend(backups[:3-len(order)])

    selected = order[:min(limit, 5)]
    policy = {"primary_count": min(len(selected), 3)}
    # The cutoff uses the minimum walk in the whole group. Label the two
    # displayed options only if their own walking times differ meaningfully.
    if earlier and choices[earlier[0]][1] - ref_walk >= EXTRA_WALK_SECONDS:
        faster = earlier[0]
        duration, walk, _ = choices[faster]
        policy.update(time_saved_seconds=ref_duration-duration, extra_walk_seconds=walk-ref_walk)
        if faster in selected:
            policy["faster_index"] = selected.index(faster)
        if reference in selected:
            policy["less_walking_index"] = selected.index(reference)
    return selected, policy
