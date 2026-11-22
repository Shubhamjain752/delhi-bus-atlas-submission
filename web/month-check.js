"use strict";

async function loadMonthBoarding() {
  const endpoint="/validation?day_type=month_boarding";
  const node=id=>document.getElementById(id);
  const fields=[
    ["offered","Conditional offers"],["approaching","Bus still approaching"],["gone","Bus already gone"],
    ["uncertain","Timing uncertain"],["unverified","Insufficient GPS"]
  ];
  const policies=["baseline","revised","suppressed"];
  const number=value=>value.toLocaleString("en-IN");
  const count=value=>Number.isInteger(value) && value>=0;
  const size=value=>Array.isArray(value)?value.length:value;
  const validPolicies=group=>policies.every(key=>fields.every(([field])=>count(group?.[key]?.[field])));
  const fmtDate=new Intl.DateTimeFormat("en-GB",{timeZone:"UTC",day:"numeric",month:"short",year:"numeric"});
  const date=value=>fmtDate.format(new Date(value+"T12:00:00Z"));
  const dateList=dates=>{
    const days=[...new Set(dates.map(d=>Number(d.slice(8,10))))].sort((a,b)=>a-b);
    const ranges=[];
    for (const day of days) {
      const range=ranges.at(-1);
      if (range && day===range[1]+1) range[1]=day;
      else ranges.push([day,day]);
    }
    return ranges.map(([start,end])=>start===end?String(start):start+"–"+end).join(", ")+" October";
  };
  try {
    const response=await fetch(endpoint);
    if (!response.ok) return;
    const report=await response.json();
    const {calendar,census,cohort}=report, fresh=report.groups?.fresh;
    const days=Array.from({length:31},(_,i)=>"2026-10-"+String(i+1).padStart(2,"0"));
    if (report.status!=="complete" || calendar?.first!=="2026-10-01" || calendar.last!=="2026-10-31" ||
        size(calendar.expected_days)!==31 || size(calendar.processed_days)!==31 || !days.every(day=>report.by_day?.[day]) ||
        !Array.isArray(fresh?.dates) || !fresh.dates.length || !fresh.dates.every(day=>days.includes(day)) ||
        !validPolicies(fresh.policies) || !validPolicies(report.policies) ||
        ![census?.raw_records,size(census?.route_ids),cohort?.opportunities,cohort?.positive_gap_1_30,cohort?.matched,cohort?.reconstructed_calls].every(count)) return;
    const original=fresh.policies.baseline, revised=fresh.policies.revised, removed=fresh.policies.suppressed;
    const freshCount=new Set(fresh.dates).size;
    const unresolved=revised.uncertain+revised.unverified;
    node("month-finding").textContent="Across "+freshCount+" previously unused dates after model training, GPS showed the bus had already gone for "+
      number(original.gone)+" of "+number(original.offered)+" offers under the earlier rule and "+number(revised.gone)+" of "+
      number(revised.offered)+" under the revised rule. The departure check withheld "+number(removed.offered)+
      " offers, including "+number(removed.approaching)+" where the bus was still approaching. The revised offers included "+
      number(unresolved)+" unresolved GPS outcomes.";
    function showGroup() {
      const heldOut=node("month-population").value==="fresh";
      const data=heldOut?fresh.policies:report.policies;
      node("month-dates").textContent=heldOut?dateList(fresh.dates)+" 2026.":date(calendar.first)+" to "+date(calendar.last)+". Includes dates used in earlier work.";
      const body=node("month-results").tBodies[0];
      body.replaceChildren();
      for (const [key,label] of fields) {
        const row=body.insertRow(), heading=document.createElement("th");
        heading.scope="row"; heading.textContent=label; row.append(heading);
        for (const policy of policies) row.insertCell().textContent=number(data[policy][key]);
      }
    }
    node("month-population").addEventListener("change",showGroup);
    showGroup();
    const sampled=count(cohort.distinct_buses) && count(cohort.observed_routes)?
      " from "+number(cohort.distinct_buses)+" distinct buses on "+number(cohort.observed_routes)+" routes":"";
    node("month-coverage").textContent="Across all 31 dates, the archive contained "+number(census.raw_records)+" GPS records and "+
      number(size(census.route_ids))+" route IDs. The targeted sample produced "+number(cohort.opportunities)+" simulated rider decisions"+sampled+". In "+
      number(cohort.positive_gap_1_30)+" cases the forecast preceded the rider’s platform time by 1–30 seconds. "+
      number(cohort.matched)+" cases matched the timetable, and the router accepted "+number(cohort.reconstructed_calls)+" reconstructed stop forecasts.";
    if (count(census.hour_bins_with_records) && count(census.calendar_hour_bins)) {
      const empty=census.calendar_hour_bins-census.hour_bins_with_records;
      const gapText=["At least one GPS record was received in "+number(census.hour_bins_with_records)+" of "+
        number(census.calendar_hour_bins)+" calendar hours. "+number(empty)+" hours had no records."];
      const last=report.by_day?.["2026-10-07"]?.last_receipt_clock, next=report.by_day?.["2026-10-08"]?.first_receipt_clock;
      if (last && next) gapText.push("The last receipt on 7 October was at "+last+" IST and the first on 8 October at "+next+" IST.");
      const windows=census.windows_with_no_eligible_decision;
      if (windows) {
        const missingWindows=typeof windows==="number"?windows:Object.values(windows).flat().length;
        gapText.push(number(missingWindows)+" of the 124 study windows produced no eligible rider decision.");
      }
      node("month-gaps").textContent=gapText.join(" ");
      node("month-gaps").hidden=false;
    }
    node("month-sampling").textContent="The test selected 40 heavily sampled routes and 12 additional routes. Each physical bus could contribute once per date, in one of four 90-minute windows beginning at 08:00, 12:00, 17:00 or 22:00 IST. A bus could reappear on another date. The month study ends at 23:30 each night. The separate 01:18 November replay covers one after-midnight case. The primary comparison uses post-training dates set aside from earlier evaluations.";
    if (census.raw_records>0 && count(census.selected_route_records_all_hours)) {
      const share=(100*census.selected_route_records_all_hours/census.raw_records).toFixed(1);
      node("month-sampling").append(" Across all hours, the 52 selected route IDs accounted for "+number(census.selected_route_records_all_hours)+
        " GPS records ("+share+"% of the archive).");
    }
    node("month-limitations").textContent="The source is Delhi’s DTC bus GPS archive. The timetable labels 45 selected routes DTC and seven DIMTS. Results for other operators or cities require their own data. The timetable runs one calendar every day with no holiday exceptions. Weekday and weekend GPS running times can differ. The departure check and the later outcome use the same GPS stream and stop geometry. Their agreement cannot independently establish whether the doors opened or a passenger boarded.";
    const check502=report.by_route_group?.["502_diagnostic"];
    if (check502 && [check502.opportunities,check502.positive_gap_1_30].every(count) && validPolicies(check502.policies)) {
      const offers=check502.policies.revised;
      node("month-502").textContent="Across both directions of route 502, the four windows yielded "+number(check502.opportunities)+
        " rider decisions, "+number(check502.positive_gap_1_30)+" within the 1–30 second gap and "+number(offers.offered)+
        " revised conditional offers. GPS showed the bus already gone for "+number(offers.gone)+" of those offers.";
      node("month-502").hidden=false;
    }
    for (const example of (report.examples || [])) {
      if (!example?.day || !example.route || !example.description) continue;
      const item=document.createElement("li"), label=document.createElement("strong");
      const instant=typeof example.ready==="number"?new Date(example.ready*1000):new Date(example.ready || "");
      const moment=Number.isFinite(instant.getTime())?instant.toLocaleTimeString("en-GB",{timeZone:"Asia/Kolkata",hour12:false}):
        typeof example.ready==="string" && /^\d{2}:\d{2}/.test(example.ready)?example.ready:"";
      label.textContent=date(example.day)+(moment?" "+moment+" IST":"")+" · "+example.route;
      const action=example.offered===true?"The option was kept. ":example.offered===false?"The option was withheld. ":"";
      item.append(label,". "+action+example.description);
      node("month-examples").append(item);
    }
    node("month-check").hidden=false;
    node("month-jump").hidden=false;
    if (location.hash==="#month-check") node("month-check").scrollIntoView({block:"start"});
  } catch {}
}
loadMonthBoarding();
