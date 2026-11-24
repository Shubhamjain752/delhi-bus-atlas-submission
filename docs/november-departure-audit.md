# Could Atlas have picked a better bus at 5:30 p.m.?

On 24 November a passenger leaving Rohini Sector 22 for Azadpur had a 13-minute walk to the crossing. The original Atlas forecast gave a 6:35 p.m. arrival. Repeating the search with a full month of GPS history gave 6:46 p.m. This audit asks why the route changed and whether vehicle reports already available at 5:30 could have helped.

## Why the old connection disappeared

We ran both models on the same two scheduled buses. The walk and the 90 seconds allowed for changing buses stayed the same.

| On the original 972UP and 114DOWN trips | Four days of history | 31 days of history |
| --- | --- | --- |
| Passenger reaches the first stop | 5:43:05 p.m. | 5:43:05 p.m. |
| Estimated time the 972UP leaves that stop | 5:43:32 p.m. | 5:36:34 p.m. |
| Estimated time the 972UP reaches the change stop | 5:44:00 p.m. | 5:37:13 p.m. |
| Estimated time the 114DOWN leaves the change stop | 5:45:41 p.m. | 5:36:58 p.m. |

The old plan had 27 seconds to spare at pickup and 11 seconds beyond the transfer allowance. With the longer history the first bus was expected to leave about six and a half minutes before the passenger reached it. This explains why Atlas looked for another route. GPS did not establish when that particular 972UP reached the crossing.

## Where the 5:45 estimate came from

The replacement was an 808STDOWN scheduled to leave Rohini Sector 37 E Bus Depot at 5:25 p.m. The timetable put it at the crossing at 5:46:16. October travel times over the preceding nine stretches gave 5:44:46.

The recorder had received 41 vehicle-position snapshots by 5:30, the last at 5:29:56. The only 808STDOWN it saw in those snapshots carried a 4:45 p.m. start time. At the cutoff it was near Q Block Mangolpuri, farther along the route and about 4.1 km from the pickup. None of those reports identified the 5:25 run. Restoring all 41 snapshots left the 5:44:46 boarding estimate unchanged. It moved the predicted arrival in Azadpur from 6:46:27 to 6:46:41.

GPS later placed an 808STDOWN at the pickup between 6:29:06 and 6:29:28. Its feed label gave a 5:45 p.m. start. This establishes that an 808 served the stop. It leaves open whether the earlier run passed without reporting. There was no rider to record a final arrival for Atlas or for Google Maps.

## Could a different recommendation have helped?

We repeated seven November searches. For each departure we restored only packets the recorder had received by that time. Two rules were tested separately against the usual first choice. The first preferred a bus with a fresh vehicle match if the journey would arrive within five minutes of the fastest option. The second preferred fewer changes when the arrival times were within one minute. Later GPS was used only to check the choices.

| Departure in Delhi | What the test showed |
| --- | --- |
| Rohini, Tuesday 5:30 p.m. | None of the six options checked had a confirmed approaching first bus. The simpler rule selected the direct 102ADOWN, due to arrive 35 seconds after the two-bus option. Later GPS never established that it picked up at the terminal. |
| AIIMS, Sunday 3:00 p.m. | Atlas kept the 323UP first. The recorder had both vehicle positions and operator predictions. Neither established that the named bus would make the pickup. |
| Punjabi Bagh, Sunday 3:10 p.m. | The direct 153DOWN stayed first. Later GPS could not establish its pickup at the planned stop. |
| Kashmere Gate, Monday 8:30 a.m. | The first archived position report arrived at 8:36:46. Later GPS could trace part of the third option. That evidence was unavailable at departure. |
| Sarai Peepal Thala, Saturday 1:17 a.m. | There were no packets yet. The estimated arrival at GTK Depot was 1 minute 25 seconds later than the first recorded bus sighting there. |
| Adhchini, Saturday 1:18 a.m. | Atlas kept the 502UP as a conditional choice. GPS later placed the bus near the stop. Whether its doors were open is unknown. |
| Vasant Kunj, Saturday 1:20 a.m. | Atlas already showed the vehicle-supported 604UP first. Its estimated arrival at the exit stop was two seconds after the GPS sighting. |

The tracked-bus rule changed zero of seven first recommendations. The simpler rule changed one, at Rohini. We could not verify a better passenger arrival in any of the seven. The direct bus is worth displaying alongside the transfer, but these results do not justify making it the automatic first choice.

## What the October history learns

The deployed model uses 22 weekdays and nine weekend days. It groups GPS travel times by route, consecutive stops in travel order, weekday or weekend, and two-hour Delhi time window. It uses the middle observed time and gives broader route history more weight when a particular stop pair has few readings. If both are missing, it uses the timetable. Recent reports from the same stretch can adjust a current estimate.

For the 808STDOWN between the Rohini crossing and Sector 21, October supplied 86 weekday observations between 4 and 6 p.m. Their middle value was 33 seconds. Between 6 and 8 p.m. there were 82 observations, with a middle value of 54 seconds. After blending with the broader history, Atlas uses 33 and 53 seconds respectively. Those observations describe a bus already travelling. They cannot establish whether a particular departure left the depot today.

Training pools Monday through Friday and also pools Saturday and Sunday. It does not currently learn separate Tuesday, holiday, day-of-month or week-by-week trends. The next question to test on unseen dates is whether actual departures from the depot and gaps between buses can improve a passenger's wait estimate.

[Seven departure replays](november-departure-audit.json) · [All six Rohini options](november-rohini-six-options.json) · [Replay script](../tools/audit_november_departures.py) · [Original comparison with Google Maps](../submission/05-validation.pdf)

To reproduce locally with the original dated packets, run `python tools/audit_november_departures.py --receipts .whole-journey-witness --output november-audit.json`. Add `--case tuesday-transfer --limit 6` for the wider search. The overnight cases use the original packets in `data/nov21-samples`. The public repository includes the results. Raw receiver archives are held separately.
