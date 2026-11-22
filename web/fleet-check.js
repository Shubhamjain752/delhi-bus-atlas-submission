"use strict";

async function loadFleetCheck() {
  const el = id => document.getElementById("fleet-" + id);
  const format = n => n.toLocaleString("en-IN");
  const counted = n => Number.isSafeInteger(n) && n >= 0;
  const outcomes = [
    ["offered", "Conditional offers"], ["approaching", "Still approaching"],
    ["gone", "Already gone"], ["uncertain", "Timing uncertain"], ["unverified", "Insufficient GPS"]
  ];
  const policies = ["baseline", "revised", "removed", "added"];
  function validCounts(group) {
    return group && [group.cases, group.matched, group.replayed].every(counted) &&
      group.cases >= group.matched && group.matched >= group.replayed && [...policies, "revised_default", "revised_secondary"].every(key => {
        const table = group[key];
        return table && outcomes.every(([field]) => counted(table[field])) &&
          table.offered === table.approaching + table.gone + table.uncertain + table.unverified;
      });
  }
  function addRow(body, label, values) {
    const row = body.insertRow(), title = document.createElement("th");
    title.scope = "row";
    title.textContent = label;
    row.append(title);
    for (const value of values) row.insertCell().textContent = format(value);
  }
  try {
    const response = await fetch("/validation?day_type=fleet_boarding");
    if (!response.ok) throw new Error("Report unavailable");
    const report = await response.json(), t = report.totals;
    const groups = ["all", "development", "holdout", "overnight", "holdout_overnight"];
    const states = [["before_exit", "Earlier rule confirmed departure"], ["after_exit", "Revised rule confirmed departure"],
      ["revised_approach", "Revised rule signalled approach"], ["unconfirmed", "Revised rule could confirm neither"]];
    const labels = ["approaching", "gone", "uncertain", "unverified"];
    const bands = [["0_15", "0–15 seconds"], ["16_30", "16–30 seconds"], ["31_60", "31–60 seconds"]];
    const completeLabels = obj => labels.every(k => counted(obj?.[k]));
    if (report.schema_version !== 1 || report.complete !== true ||
        !groups.every(key => validCounts(report.groups?.[key])) ||
        !groups.every(key => report.platform?.groups?.[key]?.matched === report.groups[key].matched &&
          states.every(([field]) => completeLabels(report.platform.groups[key][field]))) ||
        !bands.every(([key]) => completeLabels(report.platform?.holdout_approach_by_age?.[key])) ||
        report.by_hour?.length !== 24 || !report.by_hour.every((hour, i) => hour.hour === i && validCounts(hour)) ||
        !Array.isArray(report.by_route) || !report.by_route.every(validCounts) ||
        ![t?.raw_records,t?.reported_routes,t?.official_observed_routes,t?.routes_with_candidate,
          t?.routes_with_matched_case,t?.routes_replayed].every(counted)) throw new Error("Incomplete report");
    el("coverage").textContent = "The 31 October files contain " + format(t.raw_records) + " GPS records on " +
      format(t.reported_routes) + " reported route IDs. " + format(t.official_observed_routes) +
      " matched the official timetable. The all-hour screen found a borderline case on " +
      format(t.routes_with_candidate) + " routes. " + format(t.routes_with_matched_case) +
      " had a usable named trip, and " + format(t.routes_replayed) + " reached the paired journey-planner test.";
    const coverage = report.route_coverage_union?.route_counts;
    if (coverage && counted(coverage.short_issued_beyond_named) && counted(coverage.short_verified_beyond_named)) {
      el("coverage").append(" A separate short-route test issued forecasts on " + format(coverage.short_issued_beyond_named) +
        " additional routes. Later GPS confirmed a downstream arrival on " + format(coverage.short_verified_beyond_named) + " of those routes.");
    }
    function showPeriod() {
      const group = report.groups[el("period").value], result = group.revised;
      const platform = report.platform.groups[el("period").value], table = el("platform").tBodies[0];
      table.replaceChildren();
      for (const [key, description] of states) {
        const values = labels.map(k => platform[key][k]);
        addRow(table, description, [values.reduce((sum, count) => sum + count, 0), ...values]);
      }
      el("period-summary").textContent = format(group.cases) + " screened cases, " + format(group.matched) +
        " matched cases and " + format(group.replayed) + " decisions replayed through both planners.";
      const service = report.service_days.groups[el("period").value], prior = service.previous;
      const late = service.previous_after_static_end, end = report.service_days.next_civil_day_cutoff_seconds;
      const cutoff = [Math.floor(end / 3600), Math.floor(end / 60) % 60, end % 60]
        .map(value => String(value).padStart(2, "0")).join(":");
      el("service-days").textContent = format(prior.matched) + " matches belonged to the previous service day across " +
        format(prior.civil_dates.length) + " query dates. " + format(prior.replayed) + " received a paired journey replay. After the last scheduled departure at " + cutoff +
        " IST, " + format(late.matched) + " such cases matched and " + format(late.replayed) + " were replayed. " +
        "Earlier and revised planners offered " + format(late.baseline.offered) + " and " +
        format(late.revised.offered) + " conditional journeys in those replays (" + format(late.added.offered) +
        " added, " + format(late.removed.offered) + " removed).";
      const body = el("comparison").tBodies[0];
      const defaults = el("defaults").tBodies[0];
      body.replaceChildren();
      defaults.replaceChildren();
      for (const [key, label] of outcomes) {
        addRow(body, label, policies.map(p => group[p][key]));
        addRow(defaults, label, ["baseline", "revised_default", "revised_secondary"].map(p => group[p][key]));
      }
      if (result.offered) {
        const percentage = (100 * result.gone / result.offered).toFixed(1);
        el("results-note").textContent = "GPS confirms the bus had left for " + format(result.gone) + " of " +
          format(result.offered) + " revised offers (" + percentage + "%). " +
          format(result.uncertain + result.unverified) + " more offers had an unresolved outcome.";
        const timing = report.gone_offer_receipts?.[el("period").value]?.revised;
        if (result.gone > 0 && timing) {
          el("results-note").append(" Of the confirmed departures, " + format(timing.entry_or_exit_received_late || 0) +
            " had an entry or exit report arrive after the query. Both reports had arrived in time for " +
            format(timing.both_received_by_ready || 0) + ".");
        }
      } else el("results-note").textContent = "The revised planner made no conditional offers in this group.";
    }
    el("period").addEventListener("change", showPeriod);
    showPeriod();
    for (const [key, description] of bands) {
      const v = report.platform.holdout_approach_by_age[key];
      const total = labels.reduce((sum, k) => sum + v[k], 0);
      const body = el("age").tBodies[0];
      addRow(body, description, [total, v.approaching, v.gone, v.uncertain + v.unverified]);
      body.rows[body.rows.length - 1].insertCell().textContent = total ? (100 * v.gone / total).toFixed(1) + "%" : "—";
    }
    for (const h of report.by_hour) {
      const r = h.revised;
      addRow(el("hours").tBodies[0], String(h.hour).padStart(2, "0") + ":00",
        [h.cases, h.replayed, r.offered, r.approaching, r.gone, r.uncertain + r.unverified]);
    }
    if (validCounts(report.route_502up_hour_01)) {
      const h = report.route_502up_hour_01;
      el("502").textContent = "502UP, 01:00–01:59 IST across October: " + format(h.gps?.raw || 0) +
        " GPS records, " + format(h.cases) + " screened cases, " +
        format(h.matched) + " matched, " + format(h.replayed) + " replayed and " + format(h.revised.offered) +
        " revised conditional offers. ";
      const replay = document.createElement("a");
      replay.href = "/?replay=502";
      replay.textContent = "Open the original 01:18 November journey";
      el("502").append(replay);
    }
    function showRoutes() {
      const q = el("route").value.trim().toLowerCase(), list = el("route-list");
      list.replaceChildren();
      if (!q) {
        el("route-message").textContent = "Search the " + format(report.by_route.length) + " GPS route IDs in the archive.";
        return;
      }
      const hits = report.by_route.filter(row => row.route_id === q || (row.route || "").toLowerCase().includes(q));
      hits.sort((a, b) => Number(b.route_id === q) - Number(a.route_id === q) ||
        Number((b.route || "").toLowerCase() === q) - Number((a.route || "").toLowerCase() === q));
      el("route-message").textContent = hits.length ? "Showing " + format(Math.min(12, hits.length)) +
        " of " + format(hits.length) + " matching route IDs, all dates and hours." : "No matching route ID or name.";
      for (const hit of hits.slice(0, 12)) {
        const item = document.createElement("li"), title = document.createElement("strong"), r = hit.revised;
        title.textContent = (hit.route || "Timetable name unavailable") + " · ID " + hit.route_id;
        item.append(title, ". " + format(hit.raw_records) + " records, " + format(hit.cases) + " screened, " +
          format(hit.matched) + " matched, " + format(hit.replayed) + " replayed. Revised offers: " + format(r.offered) +
          ", including " + format(r.approaching) + " approaching, " + format(r.gone) + " gone and " +
          format(r.uncertain + r.unverified) + " unresolved.");
        if (!hit.cases && hit.gps) {
          const why = !hit.operator ? "The timetable has no matching route ID." :
            !hit.gps.valid ? "No report met the location, identity and freshness checks." :
            !hit.gps.forward_stop_forecasts ? "No trace qualified for a forward stop forecast." :
            "No forecast met the 1–30 second boarding-gap rule.";
          item.append(" " + why);
        }
        const exclusions = Object.entries(hit.exclusions || {}).sort((a, b) => b[1] - a[1]);
        if (exclusions.length) item.append(" Most frequent exclusion: " +
          exclusions[0][0].replaceAll("_", " ") + " (" + format(exclusions[0][1]) + ").");
        list.append(item);
      }
    }
    el("route").addEventListener("input", showRoutes);
    showRoutes();
    el("limitations").textContent = "Rider readiness was simulated at an official stop. The study reconstructed stop " +
      "forecasts from an upstream GPS fix and timetable running time. Each route counted separately in the screening, " +
      "irrespective of ridership. These outcomes describe the tested stops and buses in this archive.";
    const routeNames = new Map(report.by_route.map(r => [r.route_id, r.route]));
    const errors = Object.entries(report.platform.holdout_approach_by_route)
      .filter(([, outcomes]) => outcomes.gone > 0)
      .sort(([a, x], [b, y]) => y.gone - x.gone || a.localeCompare(b));
    if (errors.length) {
      const examples = errors.slice(0, 3).map(([id, v]) => (routeNames.get(id) || id) + " (" +
        format(v.gone) + " gone / " + format(labels.reduce((n, k) => n + v[k], 0)) + " signals)");
      el("error-routes").textContent = "On 24–31 October, " + format(errors.length) +
        " routes had an approach signal for a bus GPS later showed had left. The largest counts were on " +
        examples.join(", ") + ". These counts cover platform signals before an itinerary search.";
    } else {
      el("error-routes").textContent = "GPS established no confirmed departure among the held-out approach signals.";
    }
    for (const x of report.examples || []) {
      const item = document.createElement("li");
      const clock = x.ready_ist?.slice(11, 19) || "";
      item.textContent = x.day + " at " + clock + " IST · " + (x.route || x.route_id) + ", " +
        (x.stop_name || x.stop) + ". " + (x.revised ? (x.vehicle_approaching === true ?
          "The revised planner put this bus first. " : "The revised planner showed this as a secondary option. ") :
        "The revised planner withheld this bus. ") + ({
          gone: "GPS showed it had already left.", approaching: "GPS showed it was still approaching.",
          uncertain: "GPS could not resolve its departure.", unverified: "GPS did not confirm the passage."
        })[x.outcome];
      if (x.outcome === "gone" && counted(x.exit_receipt_delta_seconds)) {
        item.append(x.exit_receipt_delta_seconds ? " The exit report arrived " +
          format(x.exit_receipt_delta_seconds) + " seconds after the query." : " The exit report arrived at query time.");
      } else if (x.outcome === "gone" && Number.isInteger(x.exit_receipt_delta_seconds)) {
        item.append(" The exit report arrived " + format(-x.exit_receipt_delta_seconds) + " seconds before the query.");
      }
      el("examples").append(item);
    }
    el("report").hidden = false;
    el("status").textContent = "";
    if (location.hash === "#fleet-check") el("check").scrollIntoView({block: "start"});
  } catch {
    el("status").textContent = "The complete October fleet report is unavailable.";
  }
}
loadFleetCheck();
