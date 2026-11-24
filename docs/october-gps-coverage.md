# What Atlas learned from October GPS

Delhi Bus Atlas uses GPS for two questions. **How long will the ride take?** We fitted travel times from recorded bus movements and tested whether using more history helps. **Is a bus that appears due still worth showing?** We checked that separately by replaying boarding decisions across October.

| Question | Dates and coverage | What we found |
| --- | --- | --- |
| How long to the next stop on a weekday? | Trained on 12 and 13 October, tested on 15 October. The first fit used 160 route IDs with high GPS reporting volume in selected morning, midday and evening hours. | Across 82,581 stop-to-stop movements on the test day, the average absolute error was 56 seconds with the timetable, 38 with past GPS and 36 with recent reports added where available. |
| How long on a weekend? | Trained on 10 and 11 October, tested on 17 October. This fit selected 160 route IDs and also sampled the late evening. | Across 108,758 movements on the test Saturday, the corresponding errors were 56, 40 and 37 seconds, rounded to the nearest second. |
| Does a longer history give better next-stop times? | The test model learned from 1–24 October, including overnight hours. Both models then predicted the same 5,941,620 movements on 25–31 October, using history alone. | Average error was 55.34 seconds with the timetable, 50.96 with four days of GPS and 34.13 with 24 days. The share within a minute rose from 77.4% to 87.4%. |
| Could a bus already have passed? | The archive contains 912,877,061 location records across all 31 dates. An initial study sampled 47 routes with usable decisions. A later fleet-wide replay included all 2,045 route IDs for which a dated bus could be matched in an eligible case. | The initial study compared the old and revised advice on 12 previously unused dates. Of 31 suggestions later shown to be too late, the revised check removed 13 and retained 18. It kept the five suggestions where GPS showed the bus was still approaching. |

The travel-time comparisons measure time between two bus stops. Each early or late prediction counts by the size of its error in seconds. The boarding studies check the position of the bus at a passenger’s estimated arrival at the stop. A position near a stop cannot tell us whether its doors were open.

## Where the public app gets its estimates

Training groups observed journeys by route, ordered pair of stops and two-hour period. Each group keeps the number of observations and its typical travel time. For November service the app loads `data/from-2026-11/eta_model.json.gz` for weekdays and `data/from-2026-11/weekend/eta_model.json.gz` for weekends. Both ship with Vercel. A trip continuing after midnight may still belong to the previous service date. Where GPS is sparse, the app uses the wider route history or the timetable.

Recent, completed movements on the same stretch can adjust that estimate for up to 30 minutes. The adjustment uses only data available when the prediction was issued. Lasting trends enter the historical profiles when the model is retrained and deployed.

We originally sampled two weekdays and two weekend days to build and test the first planner. After the seven-day comparison, we trained the production model on all 31 dates. The original files still support replaying October with only the history available then. [Full comparison and how to reproduce it](october-eta-candidate.md)

[Weekday test results](../data/eta_metrics.json) · [Weekend test results](../data/weekend/eta_metrics.json) · [How Atlas reads its profiles](../transit/eta.py) · [Validation report](https://github.com/Shubhamjain752/delhi-bus-atlas-submission/blob/main/submission/05-validation.pdf)
