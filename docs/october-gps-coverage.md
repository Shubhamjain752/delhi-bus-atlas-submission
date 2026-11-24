# What Atlas learned from October GPS

Delhi Bus Atlas uses GPS for two different questions. **How long will the ride take?** To answer that, we fitted travel times from recorded bus movements. **Is a bus that appears due still worth showing?** To check that, we replayed boarding decisions across October. The deployed travel-time profiles and the boarding tests used different parts of the archive.

| Question | Dates and coverage | What we found |
| --- | --- | --- |
| How long to the next stop on a weekday? | Trained on 12 and 13 October, tested on 15 October. The first fit used 160 route IDs with high GPS reporting volume in selected morning, midday and evening hours. | Across 82,581 stop-to-stop movements on the test day, the average absolute error was 56 seconds with the timetable, 38 with past GPS and 36 with recent reports added where available. |
| How long on a weekend? | Trained on 10 and 11 October, tested on 17 October. This fit selected 160 route IDs and also sampled the late evening. | Across 108,758 movements on the test Saturday, the corresponding errors were 56, 40 and 37 seconds, rounded to the nearest second. |
| Could a bus already have passed? | The archive contains 912,877,061 location records across all 31 dates. An initial study sampled 47 routes with usable decisions. A later fleet-wide replay included all 2,045 route IDs for which a dated bus could be matched in an eligible case. | The initial study compared the old and revised advice on 12 previously unused dates. Of 31 suggestions later shown to be too late, the revised check removed 13 and retained 18. It kept the five suggestions where GPS showed the bus was still approaching. |

The first two comparisons measure time between two bus stops. Each early or late prediction counts by the size of its error in seconds. The boarding studies instead check the position of the bus at a passenger’s estimated arrival at the stop. A position near a stop cannot tell us whether its doors were open.

## Where the public app gets its estimates

Training groups observed journeys by route, ordered pair of stops and two-hour band. Each group keeps an observation count and a typical duration (the median). The service loads the weekday profile from `data/eta_model.json.gz` and the weekend profile from `data/weekend/eta_model.json.gz`. Both files ship with the Vercel deployment. The passenger’s Delhi service date selects the profile, including a trip continuing after midnight. The model uses route-level history or the timetable when it lacks a good local sample.

Recent, completed movements on the same stretch can adjust that estimate for up to 30 minutes. The adjustment uses only data available when the prediction was issued. Lasting trends enter the historical profiles when the model is retrained and deployed.

The four training days were chosen for a bounded first experiment with separate weekday and weekend tests. A month-wide fit would cover more dates and hours. To judge whether it improves November trips, it needs a later test period that was kept out of training. The original October results belong to the model and dates shown above.

[Weekday test results](../data/eta_metrics.json) · [Weekend test results](../data/weekend/eta_metrics.json) · [How Atlas reads its profiles](../transit/eta.py) · [Validation report](https://github.com/Shubhamjain752/delhi-bus-atlas-submission/blob/main/submission/05-validation.pdf)
