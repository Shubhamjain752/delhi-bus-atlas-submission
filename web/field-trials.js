"use strict";

const providers = [["atlas", "Delhi Bus Atlas"], ["google", "Google Maps"]];
const timezone = "Asia/Kolkata";
const clock = new Intl.DateTimeFormat("en-GB", {timeZone:timezone, hour:"2-digit", minute:"2-digit", second:"2-digit"});
const minuteClock = new Intl.DateTimeFormat("en-GB", {timeZone:timezone, hour:"2-digit", minute:"2-digit"});
const dateLabel = new Intl.DateTimeFormat("en-GB", {timeZone:timezone, day:"numeric", month:"short", year:"numeric"});
const datedClock = new Intl.DateTimeFormat("en-GB", {timeZone:timezone, day:"numeric", month:"short", hour:"2-digit", minute:"2-digit", second:"2-digit"});
const statuses = {
  independent_walk_grossly_circuitous:"The mapped walk to the platform makes an implausible detour",
  independent_final_walk_grossly_circuitous:"The mapped final walk makes an implausible detour",
  no_independently_mapped_walk_to_platform:"No mapped walk to the boarding platform",
  unresolved_route_to_map_landmarks:"The published bus route could not be matched to the mapped stops",
  no_later_witnessed_boarding:"No later bus passage at the boarding stop was verified",
  boarding_interval_overlaps_readiness:"GPS timing overlaps the rider’s modelled arrival at the stop",
  first_approaching_run_has_ambiguous_branch:"The first bus’s branch could not be identified",
  ordering_of_first_two_buses_unresolved:"GPS cannot establish which bus reached the stop first",
  same_bus_alighting_not_witnessed:"GPS did not confirm the same bus at the alighting stop",
  no_independently_mapped_final_walk:"No mapped walk from the final stop to the destination"
};

function node(tag, text, cls) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
}

function time(value, formatter = clock) {
  const stamp = Date.parse(value);
  return Number.isFinite(stamp) ? formatter.format(stamp) : "Unavailable";
}

function seconds(value) {
  if (!Number.isFinite(value) || value < 0) return "Unavailable";
  const n = Math.round(value), minutes = Math.floor(n / 60), remainder = n % 60;
  if (!minutes) return remainder + " s";
  return minutes + " min" + (remainder ? " " + remainder + " s" : "");
}

function totalWalk(forecast) {
  const parts = [forecast.access_walk_seconds, forecast.transfer_walk_seconds, forecast.egress_walk_seconds];
  return parts.every(Number.isFinite) ? seconds(parts.reduce((a,b) => a+b, 0)) : "Unavailable";
}

function comparisonTable(label, rows) {
  const table = node("table");
  table.setAttribute("aria-label", label);
  const header = table.createTHead().insertRow();
  for (const title of [label, ...providers.map(p => p[1])]) {
    const th = node("th", title);
    th.scope = "col";
    header.append(th);
  }
  const tbody = table.createTBody();
  for (const [title, values] of rows) {
    const row = tbody.insertRow(), th = node("th", title);
    th.scope = "row";
    row.append(th);
    for (const value of values) row.insertCell().textContent = value;
  }
  return table;
}

function forecastView(test, arms) {
  const rendered = (f) => arms.map((arm,i) => arm?.forecast ? f(arm.forecast, i) : "Unavailable");
  const from = Date.parse(test.departure);
  const table = comparisonTable("Forecast", [
    ["Recorded", arms.map(arm => {
      if (!arm) return "No saved forecast";
      const captured = Date.parse(arm.captured_at);
      return time(arm.captured_at, datedClock) + (captured >= from ? " · at or after departure" : "");
    })],
    ["Buses", rendered(f => f.legs.map(leg => leg.route).join(" → "))],
    ["Arrival", rendered((f,i) => time(f.arrival, i === 1 ? minuteClock : clock))],
    ["Whole journey", rendered(f => seconds((Date.parse(f.arrival) - from) / 1000))],
    ["Walk", rendered(totalWalk)], ["Wait", rendered(f => seconds(f.wait_seconds))],
    ["Ride", rendered(f => seconds(f.ride_seconds))]
  ]);
  const stops = node("details", undefined, "stops");
  stops.append(node("summary", "Boarding stops, transfers and walking times"));
  for (const [i,arm] of arms.entries()) {
    const f = arm?.forecast;
    if (!f) continue;
    stops.append(node("h4", providers[i][1]));
    const list = node("ol");
    for (const leg of f.legs) list.append(node("li", leg.route + " from " + (leg.board_name || leg.board) + " to " + (leg.alight_name || leg.alight)));
    stops.append(list, node("p", "Walk to the first stop " + seconds(f.access_walk_seconds) + ". Between buses " +
      seconds(f.transfer_walk_seconds) + ". From the last stop " + seconds(f.egress_walk_seconds) + "."));
  }
  return [table, stops];
}

function nearView(test) {
  const entry = test.near_departure;
  const forecasts = providers.map(([name]) => entry.providers?.[name]);
  const details = node("details");
  details.append(node("summary", "Forecasts checked again near departure"));
  const state = {pending:"Waiting for the capture window", captured:"Saved", no_bus_estimate:"No bus estimate returned",
    partial_or_failed:"Capture incomplete", failed:"Capture failed", incomplete_after_departure:"Departure passed without a complete pair",
    outside_capture_window:"Outside the pre-departure window", mismatched_query:"Query could not be verified"};
  details.append(node("p", "This separate check targets ten minutes before departure. A planner may suggest a different bus at that point. The travel-date comparison below follows the original saved routes."));
  const rows = [["Capture",forecasts.map(f => f?.captured_at ? time(f.captured_at,datedClock) :
    (state[f?.state] || state[entry.state] || "No saved capture"))]];
  if (forecasts.some(f => f?.state === "captured" || f?.state === "no_bus_estimate")) {
    rows.push(["Buses", forecasts.map(f => f?.routes?.join(" → ") || state[f?.state] || "Unavailable")],
      ["Updated arrival", forecasts.map((f,i) => f?.arrival ? time(f.arrival,i===1 ? minuteClock : clock) : "Unavailable")],
      ["Whole journey", forecasts.map(f => seconds(f?.duration_seconds))],
      ["Change from first forecast",forecasts.map(f => {
        const delta=f?.arrival_change_seconds;
        if (!Number.isFinite(delta)) return "Unavailable";
        return delta===0 ? "Same arrival" : seconds(Math.abs(delta)) + (delta<0 ? " earlier" : " later");
      })]);
  }
  details.append(comparisonTable("Later forecast",rows));
  return details;
}

function outcomeStatus(test, arm, sim) {
  if (sim?.status === "complete_simulation") return "Reconstructed from dated bus GPS and modelled walks";
  if (sim) return (sim.leg_reached ? "Leg " + sim.leg_reached + ". " : "") + (statuses[sim.status] || "Evidence incomplete");
  if (arm?.simulations?.length) return "No result for these walking assumptions";
  if (!arm?.forecast) return "No saved bus forecast to score";
  const pending = {recording:"GPS collection in progress", recording_with_errors:"GPS collector reported missing data",
    scoring:"GPS data being scored", ended_with_gaps:"Collection ended with gaps", scored:"No dated score for this forecast"};
  if (pending[test.collection]) return pending[test.collection];
  if (Date.parse(test.departure) > Date.now()) return "Awaiting travel date and GPS evidence";
  return "Dated GPS result unavailable";
}

function difference(arrival, bounds) {
  if (!Array.isArray(bounds) || bounds.length !== 2) return "Unavailable";
  const forecast = Date.parse(arrival), [lower,upper] = bounds.map(Date.parse);
  if (![forecast,lower,upper].every(Number.isFinite) || upper < lower) return "Unavailable";
  if (forecast >= lower && forecast <= upper) return "Within the simulated arrival interval";
  const early = forecast < lower;
  const nearest = early ? lower-forecast : forecast-upper, furthest = early ? upper-forecast : forecast-lower;
  return seconds(nearest/1000) + (nearest === furthest ? "" : " to " + seconds(furthest/1000)) + (early ? " early" : " late");
}

function passageWindow(event) {
  const lo = Date.parse(event?.lower), hi = Date.parse(event?.upper);
  if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi < lo) return null;
  return lo === hi ? time(event.lower) : time(event.lower) + "–" + time(event.upper);
}

function platformName(event, leg, end) {
  if (event.stop_label) return event.stop_label;
  if (leg && String(leg[end]) === String(event.stop_id)) return leg[end + "_name"] || leg[end];
  if (!event.stop_id) return "the platform";
  return String(event.stop_id).startsWith("google:") ? event.stop_id.slice(7) : "stop " + event.stop_id;
}

function timeline(sim, arm, provider, showDurationRange = false) {
  if (!Array.isArray(sim?.events) || !sim.events.length) return null;
  const panel = node("div",undefined,"stage-panel"), list = node("ol");
  panel.setAttribute("role","group");
  panel.setAttribute("aria-label",provider + " stages");
  panel.append(node("h4",provider),list);
  let legIndex = 0, previous;
  for (const event of sim.events) {
    if (!event || !Number.isFinite(Date.parse(event.at))) break;
    const leg = arm?.forecast?.legs?.[legIndex];
    const elapsed = (Date.parse(event.at)-Date.parse(previous?.at))/1000;
    let title, detail;
    if (event.kind === "depart_origin" && event.source === "scenario_clock" && !previous) {
      title = "Leave origin";
      detail = "Scenario starts at " + time(event.at);
    } else if ((event.kind === "ready_at_stop" || event.kind === "arrive_destination") && event.source === "walk_model") {
      const finalWalk = event.kind === "arrive_destination";
      if (finalWalk ? previous?.kind !== "alight" : !["depart_origin","alight"].includes(previous?.kind)) break;
      title = finalWalk ? "Walk to destination" :
        (previous.kind === "alight" ? "Transfer walk to " : "Walk to ") + platformName(event,leg,"board");
      const parts = [];
      if (Number.isFinite(event.walk_metres)) parts.push(Math.round(event.walk_metres) + " m");
      if (Number.isFinite(event.walking_seconds)) parts.push(seconds(event.walking_seconds));
      if (parts.length) parts.push("modelled");
      detail = (parts.length ? parts.join(" · ") + ". " : "") +
        "Modelled " + (finalWalk ? "arrival " : "platform arrival ") + (passageWindow(event) || time(event.at));
    } else if (event.source === "bus_gps" && Number.isFinite(elapsed) && elapsed >= 0) {
      const boarding = event.kind === "board" && previous?.kind === "ready_at_stop";
      if (!boarding && !(event.kind === "alight" && previous?.kind === "board")) break;
      const bounds = passageWindow(event);
      if (!bounds) break;
      const route = event.route_label || event.route || leg?.route || "bus";
      const stop = platformName(event,leg,boarding ? "board" : "alight");
      title = boarding ? "Wait for " + route + " at " + stop : "Ride " + route + " to " + stop;
      detail = seconds(elapsed) + (boarding ? " to the first witnessed passage" : " between passage midpoints") +
        (boarding ? " (midpoint estimate). " : ". ") + "GPS passage window " + bounds;
      if (showDurationRange && passageWindow(previous)) {
        const low = (Date.parse(event.lower) - Date.parse(previous.upper)) / 1000;
        const high = (Date.parse(event.upper) - Date.parse(previous.lower)) / 1000;
        if (low >= 0 && high >= low) detail += ". " + (boarding ? "Wait to this passage " : "Ride time ") +
          seconds(low) + (high === low ? "" : " to " + seconds(high));
      }
      if (!boarding) legIndex += 1;
    } else break;
    const item = node("li");
    item.append(node("strong",title),node("span",detail));
    list.append(item);
    previous = event;
  }
  if (!list.childElementCount) return null;
  const overlap = sim.boundary_witness;
  if (sim.status === "boarding_interval_overlaps_readiness" && overlap && passageWindow(overlap)) {
    panel.append(node("p","A GPS passage at " + passageWindow(overlap) +
      " overlaps the modelled arrival at the platform. Boarding is uncertain.","stage-note"));
  }
  return panel;
}

function outcomesView(test, arms) {
  const box = node("div");
  box.append(node("h3", "Travel-date GPS comparison"));
  const inProgress = arms.map((arm,i) => arm?.interim ? providers[i][1] : null).filter(Boolean);
  if (inProgress.length) {
    box.append(node("p", "Interim observations for the " + inProgress.join(" and ") + " routes. The GPS collection window is still open. A later bus may appear as more positions arrive."));
    arms = arms.map(arm => arm?.interim ? {...arm,...arm.interim} : arm);
  }
  const choices = new Map([["1.25/0", [1.25,0]]]);
  for (const arm of arms) for (const s of arm?.simulations || []) {
    if (s.walking_pace_mps > 0 && Number.isFinite(s.transfer_allowance_seconds)) {
      choices.set(s.walking_pace_mps + "/" + s.transfer_allowance_seconds, [s.walking_pace_mps,s.transfer_allowance_seconds]);
    }
  }
  const select = node("select");
  select.id = "assumptions-" + test.id;
  for (const [value,[pace,buffer]] of choices) {
    const option = node("option", pace + " m/s walking · " + (buffer ? seconds(buffer) + " transfer allowance" : "no extra transfer time"));
    option.value = value;
    select.append(option);
  }
  if (choices.size > 1) {
    const label = node("label", "Walking and transfer assumptions");
    label.htmlFor = select.id;
    box.append(label, select);
  }
  const output = node("div");
  output.setAttribute("aria-live", "polite");
  function update() {
    const [pace,buffer] = choices.get(select.value);
    const sims = arms.map(arm => arm?.simulations?.find(s => s.walking_pace_mps===pace && s.transfer_allowance_seconds===buffer));
    const rows = [["Evidence", sims.map((s,i) => outcomeStatus(test,arms[i],s))]];
    const values = (fn) => sims.map((s,i) => s?.status === "complete_simulation" ? fn(s,arms[i]) : "Unscored");
    if (sims.some(s => s?.status === "complete_simulation")) {
      rows.push(["Arrival interval", values(s => s.arrival_interval_ist?.map(t => time(t)).join(" – ") || "Unavailable")],
        ["Whole journey", values(s => seconds(s.door_to_door_seconds_proxy))],
        ["Modelled walk", values(s => seconds(s.walking_seconds))],
        ["Wait after modelled walk", values(s => seconds(s.waiting_seconds))],
        ["Ride between GPS passages", values(s => seconds(s.riding_seconds))],
        ["Forecast compared with interval", values((s,arm) => Date.parse(arm.captured_at) < Date.parse(test.departure) ?
          difference(arm.forecast.arrival,s.arrival_interval_ist) : "Forecast was unavailable before departure")]);
    }
    output.replaceChildren(comparisonTable("Simulation", rows));
    const timelines = sims.map((s,i) => timeline(s,arms[i],providers[i][1])).filter(Boolean);
    if (timelines.length) {
      const panels = node("div",undefined,"stage-panels");
      panels.append(...timelines);
      output.append(node("h4","Journey stages","stages-heading"),
        node("p","Walks use the selected pace. Bus times use GPS passage intervals. A gap in coverage can hide an earlier bus and lengthen the estimated wait.","stage-note"),
        panels);
    }
    const methods = [["shape_crossings","Allow a crossing between GPS fixes"],
      ["published_walk_distances","Use the planner’s walk distance for an unmapped or implausible walk"],
      ["shape_crossings_and_published_walks","Allow crossings and use the planner’s walk distance"]];
    const diagnosticRows=[];
    for (const [id,label] of methods) {
      const checks=arms.map(arm=>arm?.diagnostics?.[id]?.find(s=>s.walking_pace_mps===pace && s.transfer_allowance_seconds===buffer));
      if (checks.some(Boolean)) diagnosticRows.push([label,checks.map(s=>{
        if (!s) return "Unavailable";
        if (s.status==="complete_simulation") {
          const arrival = "Modelled arrival " + s.arrival_interval_ist.map(t=>time(t)).join(" – ");
          if (![s.walking_seconds, s.waiting_seconds, s.riding_seconds].every(Number.isFinite)) return arrival;
          return arrival + ". Walk " + seconds(s.walking_seconds) + ", wait to first witnessed bus " + seconds(s.waiting_seconds) +
            ", ride " + seconds(s.riding_seconds) + ".";
        }
        return (s.leg_reached ? "Leg " + s.leg_reached + ". " : "") + (statuses[s.status] || "Evidence incomplete");
      })]);
    }
    if (diagnosticRows.length) {
      const diagnostics=node("details");
      diagnostics.append(node("summary","Other ways to interpret incomplete GPS or walking paths"),
        node("p","These checks relax the main test’s evidence requirements. A crossing can be inferred between two GPS fixes, or a missing walk can use the planner’s own published distance. Their arrival intervals are sensitivity checks, so they are excluded from the forecast error above."),
        comparisonTable("Alternative assumptions",diagnosticRows));
      output.append(diagnostics);
    }
  }
  select.addEventListener("change",update);
  update();
  box.append(output);
  for (const [i,arm] of arms.entries()) if (arm?.source_window?.first_receipt && arm.source_window.last_receipt) {
    box.append(node("p", providers[i][1] + " bus GPS receipts ran from " + time(arm.source_window.first_receipt,datedClock) + " to " +
      time(arm.source_window.last_receipt,datedClock) + ".", "source-window"));
    if (arm.passages_by_leg?.length) {
      const counts=arm.passages_by_leg.map((leg,j)=>"leg "+(j+1)+" had "+leg.boarding_stop+" at the boarding stop and "+leg.also_at_alighting_stop+" also at the alighting stop");
      box.append(node("p", providers[i][1]+" GPS candidates across the recording window. "+counts.join(". ")+".", "source-window"));
    }
  }
  return box;
}

function trial(test) {
  const section = node("section", undefined, "trial");
  const title = node("h2", test.title);
  title.id = "journey-" + test.id;
  section.setAttribute("aria-labelledby", title.id);
  const arms = providers.map(([id]) => test.providers[id]);
  section.append(node("p", test.stratum + " · " + time(test.departure,dateLabel) + " at " + time(test.departure,minuteClock), "case-meta"),
    title, ...forecastView(test,arms));
  if (test.near_departure) section.append(nearView(test));
  section.append(outcomesView(test,arms));
  return section;
}

function controlView(control) {
  const section = node("section", undefined, "supplemental-control");
  const heading = node("h2",control.title || "Punjabi Bagh Terminal to Wazirpur Depot");
  heading.id = "punjabi-bagh-control";
  section.setAttribute("aria-labelledby", heading.id);
  section.append(node("p","Supplemental control · separate from the three planned journeys","case-meta"),heading);
  if (control.state === "unverified") {
    section.append(node("p","The control’s source files could not be verified. Its results are unavailable."));
    return section;
  }
  section.append(node("p","Departure " + time(control.departure,datedClock) + " IST. Atlas first showed 159UP and Google first showed 938A. " +
    "Google’s 159 was saved separately to compare the same line. The planners use different alighting poles."));
  const available = ["interim","final"].includes(control.state);
  if (available) {
    section.append(node("p",(control.state === "final" ? "Final observations" : "Provisional observations") + " through " +
      time(control.receipt_cutoff_ist) + " IST · " + control.source_position_receipts + " GPS receipts · observation window ends " +
      time(control.horizon_end_ist) + ".", "control-status"));
  } else {
    section.append(node("p",control.state === "updating" ? "New GPS receipts are being checked. Refresh for the next verified replay." :
      "Awaiting the first GPS replay. The pre-departure forecasts are saved.","control-status"));
  }
  const label = node("label","Passage and branch rules");
  const select = node("select");
  label.htmlFor = select.id = "control-evidence";
  for (const [key,text] of [["strict_radius","Direct stop entry · strict branch"],["strict_crossings","Add directed road crossings · strict branch"],
      ["confirmed_radius","Direct stop entry · allow downstream branch confirmation"],
      ["confirmed_crossings","Add directed road crossings · allow downstream branch confirmation"]]) {
    const option = node("option",text);
    option.value = key;
    select.append(option);
  }
  if (available) section.append(label,select);
  const cards = node("div",undefined,"control-cards"), paired = node("p",undefined,"control-pairing");
  cards.setAttribute("aria-live","polite");
  section.append(cards,paired);
  const definitions = [["atlas","Atlas 159UP"],["google","Google 938A"],["google-159","Google 159"]];
  function update() {
    cards.replaceChildren();
    for (const [key,name] of definitions) {
      const arm = control.arms[key];
      if (!arm) continue;
      const card = node("article",undefined,"control-card"), title=node("h3",name);
      title.id = "control-" + key;
      card.setAttribute("aria-labelledby",title.id);
      card.append(node("p",arm.role === "matched_alternative" ? "Matched alternative" : "First choice","case-meta"),title);
      card.append(node("p",arm.board + " → " + arm.alight));
      const keyTime = node("p",undefined,"control-forecast");
      keyTime.append(node("span","Forecast arrival"),node("strong",time(arm.forecast_arrival,key === "atlas" ? clock : minuteClock)));
      card.append(keyTime,node("p","Recorded " + time(arm.captured_at) + " IST.","source-window"));
      const sim = arm.simulations[select.value];
      if (sim) {
        const complete = sim.status === "complete_simulation";
        card.append(node("p",complete ? "Modelled arrival " + sim.arrival_interval_ist.map(t => time(t)).join("–") :
          (sim.status === "same_bus_alighting_not_witnessed" ? "The same bus has no verified passage at " + arm.alight + "." :
            statuses[sim.status] || "Evidence incomplete"),"control-outcome"));
        if (complete) card.append(node("p","Forecast " + difference(arm.forecast_arrival, sim.arrival_interval_ist) + "."));
        const crossing = arm.simulations.strict_crossings;
        if (!complete && select.value === "strict_radius" && crossing?.status === "complete_simulation") {
          card.append(node("p","Two GPS fixes bracket a passage along the road. With that assumption the modelled arrival is " +
            crossing.arrival_interval_ist.map(t => time(t)).join("–") + ". Select the directed road crossing rule for its stages."));
        }
        const leg = {route:arm.route, board:arm.board_id, board_name:arm.board, alight:arm.alight_id, alight_name:arm.alight};
        const stages = timeline(sim,{forecast:{legs:[leg]}},"Walking and GPS stages",true);
        if (stages) card.append(stages);
      }
      cards.append(card);
    }
    paired.textContent = available ? (control.paired_159?.[select.value]
      ? "Both 159 arrival intervals follow the same first witnessed vehicle."
      : "A paired 159 accuracy result requires seeing the same first bus at both planners’ registered alighting poles. This rule has no complete pair at this cutoff.") : "";
  }
  select.addEventListener("change",update);
  update();
  const notes = node("details");
  notes.append(node("summary","Walking assumptions and GPS coverage"));
  const walks = control.arms.google?.access_egress_metres || [];
  if (walks.length === 2) notes.append(node("p","For both Google routes, the FOSSGIS pedestrian router supplies " + walks[0].toFixed(1) +
    " m to the stop and " + walks[1].toFixed(1) + " m from the last stop, including the offsets to the mapped road. " +
    "At 1.25 m/s those walks take " + seconds(Math.round(walks[0]/1.25)) + " and " + seconds(Math.round(walks[1]/1.25)) +
    ". The exact side of the kerb is uncertain. Atlas starts and ends at its official stop pins, so its two modelled walks are zero."));
  notes.append(node("p","The waits end at the first GPS-witnessed bus after the modelled platform arrival. Earlier buses may have missed the feed. " +
    "The GPS intervals establish vehicle passages. Passenger boarding, walking and signal delays were not observed. The downstream rule can resolve a branch only when the same first vehicle is seen later at its destination pole."));
  section.append(notes);
  return section;
}

const results = document.getElementById("trials"), statusLine = document.getElementById("status"), refresh = document.getElementById("refresh");
async function load() {
  refresh.disabled = true;
  results.setAttribute("aria-busy", "true");
  try {
    const response = await fetch("/field-trials/results", {cache:"no-store"});
    if (!response.ok) throw Error("Unable to retrieve results");
    const data = await response.json();
    if (!Array.isArray(data.cases)) throw Error("Missing cases");
    results.replaceChildren(...data.cases.map(trial));
    if (data.supplemental_control) results.append(controlView(data.supplemental_control));
    statusLine.textContent = data.cases.length ? "" : "No registered trials available.";
  } catch {
    results.replaceChildren();
    statusLine.textContent = "Journey evidence could not be loaded. Refresh to try again.";
  } finally {
    results.setAttribute("aria-busy", "false");
    refresh.disabled = false;
  }
}
refresh.addEventListener("click",load);
load();
