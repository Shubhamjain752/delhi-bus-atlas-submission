"use strict";
const el = id => document.getElementById(id);
const clock = (v, seconds = true) => v ? new Intl.DateTimeFormat("en-GB", {
  timeZone:"Asia/Kolkata",hour:"2-digit",minute:"2-digit",...(seconds ? {second:"2-digit"} : {})
}).format(new Date(v)) : "—";
function duration(seconds) {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes + " min" + (remainder ? " " + remainder + " s" : "");
}
function cells(body, values) {
  const tr = body.insertRow();
  for (const value of values) tr.insertCell().textContent = value;
  return tr;
}
const findings = {
  "285":"The first local feed arrived after the rider’s query. Atlas used the timetable. The bus appeared near the origin two seconds after the query, leaving the actual boarding opportunity uncertain.",
  "2383":"A live forecast put departure three seconds before the requested start at the platform. GPS placed the bus near the platform 23 seconds after the query. The next planned bus arrives in the following hour.",
  "8065":"Two vehicles were reporting the same scheduled trip ID. GPS traced the observed vehicle through the four consecutive stops from Sec B11 to Sec B 8/9."
};
function show(item, index, scroll) {
  const option = item.options[0];
  const bus = option?.legs.find(leg => leg.mode === "bus");
  const boarding = bus ? new Date(new Date(bus.arrival).getTime() - bus.seconds * 1000) : null;
  el("case-title").textContent = item.observed.route;
  el("pair").textContent = item.origin.name + " → " + item.destination.name + " · Requested " + clock(item.issue, false);
  const body = el("case-times");
  body.replaceChildren();
  const alert=item.boarding_alerts?.find(entry=>entry.conditional_itinerary);
  const conditional=alert?.conditional_itinerary;
  if (conditional) {
    cells(body,["Delhi Bus Atlas · if still boarding",clock(alert.ready_at),clock(conditional.arrival),duration(conditional.duration_seconds)]);
  }
  cells(body,[conditional?"Delhi Bus Atlas · later bus":"Delhi Bus Atlas",clock(boarding),clock(option?.arrival),option ? duration(option.duration_seconds) : "—"]);
  cells(body,["Google Maps",clock(item.google.boarding_ist,false),clock(item.google.final_arrival_ist,false),duration(item.google.elapsed_from_request_seconds)]);
  cells(body,["Observed bus GPS",clock(item.observed.origin_passage_ist),clock(item.observed.destination_passage_ist),duration(item.observed.elapsed_from_request_seconds)]);
  el("finding").textContent = findings[item.origin.id] || "";
  el("boarding-replay").hidden = String(item.origin.id)!=="2383";
  if (conditional) el("finding").textContent += " The conditional arrival assumes boarding at "+clock(alert.ready_at)+".";
  if (bus?.live_run_id) el("finding").textContent += " Atlas used the boarding and alighting predictions from the same position-confirmed vehicle.";
  const previous = item.original_forecast;
  el("previous").hidden = !previous || previous.arrival === option?.arrival;
  if (!el("previous").hidden) el("previous").textContent = "The earlier release predicted " + clock(previous.arrival) + ". Both releases used the same feed cutoff.";
  const walk = item.walking_alternative;
  el("walking").hidden = !walk;
  if (walk) el("walking").textContent = "From these pins, the mapped walk is " + walk.distance_m.toLocaleString() + " m. At the planner’s assumed pace it takes " + duration(walk.seconds) + ".";
  el("google").href = item.google.url;
  const rows = Array.from(el("summary").rows);
  rows.forEach((r,i) => {
    r.classList.toggle("selected", i === index);
    r.querySelector("button").setAttribute("aria-pressed",String(i === index));
  });
  el("case").hidden = false;
  if (scroll) el("case").scrollIntoView({block:"nearest"});
}
async function loadChecks() {
  try {
    const result = await fetch("/validation?day_type=matched");
    if (!result.ok) throw Error();
    const items = await result.json();
    for (const [i,item] of items.entries()) {
      const conditional=item.boarding_alerts?.find(entry=>entry.conditional_itinerary)?.conditional_itinerary;
      const atlas=conditional?clock(conditional.arrival)+" if boarded / "+clock(item.options[0]?.arrival)+" later bus":clock(item.options[0]?.arrival);
      const row = cells(el("summary"),["",clock(item.issue,false),atlas,clock(item.google.final_arrival_ist,false),clock(item.observed.destination_passage_ist)]);
      const b = document.createElement("button");
      b.type = "button"; b.textContent = item.observed.route; b.setAttribute("aria-controls","case");
      b.addEventListener("click",() => show(item,i,true));
      row.cells[0].append(b);
    }
    el("comparisons").hidden = false;
    show(items[0],0,false);
    el("load-status").textContent = "";
  } catch {
    el("load-status").textContent = "The comparison data could not be loaded. Refresh this page after the local planner starts.";
  }
}
loadChecks();
async function loadPeakCheck() {
  try {
    const response=await fetch("/validation?day_type=near_miss");
    if (!response.ok) return;
    const data=await response.json();
    const peaks=["AM","PM"].map(key=>data.test_by_peak?.[key]?.thresholds?.fallback_5min?.["30"]);
    if (peaks.some(peak=>!peak)) return;
    const rows=el("peak-rows");
    for (const [key,label] of [["offered","Cases meeting the timetable rule"],["approaching","Still approaching"],
      ["gone","Already gone"],["uncertain","Timing uncertain"],["unverified","Unverified"],
      ["precision_among_definitive","Approaching among classified buses"]]) {
      const row=rows.insertRow(), heading=document.createElement("th");
      heading.scope="row"; heading.textContent=label; row.append(heading);
      for (const peak of peaks) {
        const value=peak[key];
        row.insertCell().textContent=value==null?"—":key==="precision_among_definitive"?(100*value).toFixed(1)+"%":value.toLocaleString("en-IN");
      }
    }
    el("peak-timetable").hidden=false;
    el("peak-check").hidden=false;
  } catch {}
}
loadPeakCheck();
async function loadPeakRouter() {
  try {
    const response=await fetch("/validation?day_type=near_miss_router");
    if (!response.ok) return;
    const {overall}=await response.json();
    const outcomes=overall?.emitted_outcomes;
    if (!outcomes) return;
    const {attempted,statically_matched_and_replayed:matched,conditional_emitted:offered}=overall;
    const {approaching,gone,uncertain,unverified}=outcomes;
    if (![attempted,matched,offered,approaching,gone,uncertain,unverified].every(Number.isInteger)) return;
    const count=n=>n.toLocaleString("en-IN");
    el("peak-router-offers").textContent="Of "+count(attempted)+" cases, the planner matched "+count(matched)+
      " to published trips and replayed them. It offered a conditional journey in "+count(offered)+(offered===1?" case.":" cases.");
    el("peak-router-outcomes").textContent=offered?
      "Among those offers, later GPS showed the bus still approaching in "+count(approaching)+" and already gone in "+count(gone)+
      ". The remaining "+count(uncertain+unverified)+" were unresolved ("+count(uncertain)+" uncertain, "+count(unverified)+" unverified).":
      "There were no offered journeys to check against later GPS.";
    el("peak-router").hidden=false;
    el("peak-check").hidden=false;
  } catch {}
}
loadPeakRouter();
async function loadPeakConfirmation() {
  try {
    const response=await fetch("/validation?day_type=near_miss_confirmation");
    if (!response.ok) return;
    const data=await response.json();
    if (data.date!=="2026-10-29" || !data.router) return;
    const {baseline,revised,suppressed}=data.router;
    const keys=["offered","approaching","gone","uncertain","unverified"];
    if (![baseline,revised,suppressed].every(group=>group && keys.every(k=>Number.isInteger(group[k])))) return;
    const outcomes=group=>group.approaching+" approaching, "+group.gone+" already gone, "+
      group.uncertain+" uncertain and "+group.unverified+" unverified.";
    const examined=Number.isInteger(data.positive_gap_1_30)?"In "+data.positive_gap_1_30+" cases, the forecast preceded the platform time by 1–30 seconds. ":"";
    el("confirmation-offers").textContent=examined+"The earlier rule offered "+baseline.offered+" conditional "+(baseline.offered===1?"journey":"journeys")+
      ". The revised rule offered "+revised.offered+"."+(revised.offered?" GPS outcomes for those offers were "+outcomes(revised):"");
    el("confirmation-suppressed").textContent=suppressed.offered?
      "The GPS check withheld "+suppressed.offered+(suppressed.offered===1?" option.":" options.")+" Later GPS outcomes for these were "+outcomes(suppressed):
      "The GPS check withheld no options on this date."+(baseline.offered===revised.offered?" Both versions offered the same journeys.":"");
    el("peak-confirmation").hidden=false;
    el("peak-check").hidden=false;
  } catch {}
}
loadPeakConfirmation();
