# What Atlas learned from October GPS

Delhi Bus Atlas uses GPS for two questions. **How long will the ride take?** We fitted travel times from recorded bus movements and tested whether using more history helps. **Is a bus that appears due still worth showing?** We checked that separately by replaying boarding decisions across October.

| Question | Dates and coverage | What we found |
| --- | --- | --- |
| How long to the next stop on a weekday? | Trained on 12 and 13 October, tested on 15 October. The first fit used 160 route IDs with high GPS reporting volume in selected morning, midday and evening hours. | Across 82,581 stop-to-stop movements on the test day, the average absolute error was 56 seconds with the timetable, 38 with past GPS and 36 with recent reports added where available. |
| How long on a weekend? | Trained on 10 and 11 October, tested on 17 October. This fit selected 160 route IDs and also sampled the late evening. | Across 108,758 movements on the test Saturday, the corresponding errors were 56, 40 and 37 seconds, rounded to the nearest second. |
| Does a longer history give better next-stop times? | The experimental model learned from 1–24 October, including overnight hours. Both models then predicted the same 5,941,620 movements on 25–31 October, using history alone. | Average error was 55.34 seconds with the timetable, 50.96 with the deployed history and 34.13 with the longer history. The share within a minute rose from 77.4% to 87.4%. |
| Could a bus already have passed? | The archive contains 912,877,061 location records across all 31 dates. An initial study sampled 47 routes with usable decisions. A later fleet-wide replay included all 2,045 route IDs for which a dated bus could be matched in an eligible case. | The initial study compared the old and revised advice on 12 previously unused dates. Of 31 suggestions later shown to be too late, the revised check removed 13 and retained 18. It kept the five suggestions where GPS showed the bus was still approaching. |

The travel-time comparisons measure time between two bus stops. Each early or late prediction counts by the size of its error in seconds. The boarding studies check the position of the bus at a passenger’s estimated arrival at the stop. A position near a stop cannot tell us whether its doors were open.

## Where the public app gets its estimates

Training groups observed journeys by route, ordered pair of stops and two-hour band. Each group keeps an observation count and a typical duration (the median). The service loads the weekday profile from `data/eta_model.json.gz` and the weekend profile from `data/weekend/eta_model.json.gz`. Both files ship with the Vercel deployment. The passenger’s Delhi service date selects the profile, including a trip continuing after midnight. The model uses route-level history or the timetable when it lacks a good local sample.

Recent, completed movements on the same stretch can adjust that estimate for up to 30 minutes. The adjustment uses only data available when the prediction was issued. Lasting trends enter the historical profiles when the model is retrained and deployed.

The four training dates kept the first experiment manageable and left separate weekday and weekend tests. We have since fitted a candidate on all 31 dates for use in November experiments. Its earlier, 24-day version beat the deployed history on all seven test dates, including after midnight. The public planner still uses the four-day profiles. [Full comparison and how to reproduce it](october-eta-candidate.md)

[Weekday test results](../data/eta_metrics.json) · [Weekend test results](../data/weekend/eta_metrics.json) · [How Atlas reads its profiles](../transit/eta.py) · [Validation report](https://github.com/Shubhamjain752/delhi-bus-atlas-submission/blob/main/submission/05-validation.pdf)
