"use strict";
const form = document.querySelector("#example");
const output = document.querySelector("#output");
const statusLine = document.querySelector("#response-status");
const delhiClock = new Intl.DateTimeFormat("en-GB", {timeZone:"Asia/Kolkata", hour:"2-digit", minute:"2-digit"});

function refreshLink() {
  const params = new URLSearchParams(new FormData(form));
  params.set("limit", "3");
  const path = "/journeys?" + params.toString();
  document.querySelector("#raw").href = path;
  document.querySelector("#path").textContent = path;
  return path;
}
form.addEventListener("input", () => {
  refreshLink();
  output.hidden = true;
  statusLine.textContent = "";
});
let requestNumber = 0;
form.addEventListener("submit", async event => {
  event.preventDefault();
  const path = refreshLink();
  const number = ++requestNumber;
  output.hidden = true;
  statusLine.textContent = "Searching Delhi bus services…";
  try {
    const response = await fetch(path);
    const payload = await response.json();
    if (number !== requestNumber || path !== refreshLink()) return;
    if (!response.ok) {
      const error = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
      statusLine.textContent = "HTTP " + response.status + " · " + error;
      return;
    }
    const rows = document.querySelector("#rows");
    rows.replaceChildren();
    for (const trip of payload.journeys) {
      const line = rows.insertRow();
      const buses = trip.legs.filter(leg => leg.mode === "bus").map(leg => leg.route).join(" → ");
      const values = [buses || "Same location", delhiClock.format(new Date(trip.arrival)),
        ...[trip.duration_seconds, trip.walk_seconds, trip.wait_seconds, trip.ride_seconds].map(s => (s / 60).toFixed(1) + " min")];
      for (const value of values) line.insertCell().textContent = value;
    }
    document.querySelector("#json").textContent = JSON.stringify(payload, null, 2);
    output.hidden = false;
    statusLine.textContent = ["HTTP 200", payload.journeys.length + " options", ...payload.warnings].join(" · ");
  } catch (error) {
    if (number === requestNumber) statusLine.textContent = "Could not reach the planner. Check that the local server is running.";
  }
});
refreshLink();
