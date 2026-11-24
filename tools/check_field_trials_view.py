"""Verify the dated comparison view using its real records and labelled UI fixtures."""
import argparse
from datetime import datetime, timedelta

from playwright.sync_api import expect, sync_playwright


def verify(page, base):
    page.goto(base + "/field-checks")
    link = page.get_by_role("link",name="Whole-journey trials with walking, waiting and bus changes")
    with page.expect_response(lambda response:response.url.endswith("/field-trials/results")) as got:
        link.click()
    assert got.value.ok
    result = got.value.json()
    expect(page.locator(".trial")).to_have_count(len(result["cases"]))
    assert len(result["cases"]) >= 3
    for case in result["cases"]:
        section = page.get_by_role("region", name=case["title"])
        forecasts = section.get_by_role("table",name="Forecast",exact=True)
        row = forecasts.get_by_role("row",name="Buses",exact=False)
        for i,name in enumerate(("atlas","google")):
            forecast = case["providers"][name]["forecast"]
            if forecast:
                expect(row.locator("td").nth(i)).to_have_text(" → ".join(l["route"] for l in forecast["legs"]))
        expect(section.get_by_role("table",name="Simulation",exact=True)).to_be_visible()
        if case.get("observed_legs"):
            observations = case["observed_legs"]
            expect(section.get_by_role("heading", name="Which buses did GPS confirm?")).to_be_visible()
            for text in [observations["headline"], *observations["registered"].values(), observations["near_google"]]:
                expect(section).to_contain_text(text)
            if observations.get("routes"):
                section.get_by_text("GPS sightings by bus and stop", exact=True).click()
                for group in observations["routes"].values():
                    for leg in group:
                        expect(section).to_contain_text(leg["route"] + " from " + leg["boarding_stop"] + " to " + leg["exit_stop"])
                section.get_by_text("GPS sightings by bus and stop", exact=True).click()
        for provider, title in (("atlas","Delhi Bus Atlas"),("google","Google Maps")):
            arm = case["providers"].get(provider,{})
            if "interim" in arm:
                expect(section.get_by_text("Interim observations", exact=False)).to_be_visible()
                arm = arm["interim"]
            primary = next((s for s in arm.get("simulations",[]) if s["walking_pace_mps"]==1.25
                            and s["transfer_allowance_seconds"]==0), {})
            if primary.get("events"):
                stages = section.get_by_role("group",name=title+" stages")
                expect(stages.get_by_role("listitem")).to_have_count(len(primary["events"]))
        section.get_by_text("Boarding stops, transfers and walking times",exact=True).click()
    control = result.get("supplemental_control")
    if control and control["state"] in ("interim", "final"):
        view = page.get_by_role("region", name=control["title"])
        expect(view.get_by_role("article")).to_have_count(3)
        matched = view.get_by_role("article", name="Google 159", exact=True)
        expect(matched).to_contain_text("Matched alternative")
        sim = control["arms"]["google-159"]["simulations"]["strict_radius"]
        if sim["status"] == "complete_simulation":
            expect(matched).to_contain_text("Modelled arrival")
            expect(matched).to_contain_text("GPS passage window")
        view.get_by_text("Walking assumptions and GPS coverage", exact=True).click()
        expect(view).to_contain_text("10 s and 28 s")
        view.get_by_label("Passage and branch rules").select_option("strict_crossings")
        expect(view.get_by_role("article")).to_have_count(3)
    assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")

    # Fixture cases exercise states that a future trip cannot yet supply during a release check.
    case = result["cases"][0]
    arrival = datetime.fromisoformat(case["providers"]["atlas"]["forecast"]["arrival"])
    start = datetime.fromisoformat(case["departure"])
    elapsed = int((arrival-start).total_seconds())+40
    def stamp(sec):
        return (start+timedelta(seconds=sec)).isoformat()
    def walk(kind, sec, metres, duration, stop=None):
        return {"kind":kind,"source":"walk_model","at":stamp(sec),"walk_metres":metres,"walking_seconds":duration,
                **({"stop_id":stop,"stop_label":stop} if stop else {})}
    def passage(kind, sec, route, stop):
        return {"kind":kind,"source":"bus_gps","at":stamp(sec),"lower":stamp(sec-10),"upper":stamp(sec+10),
                "route":route,"stop_id":stop,"stop_label":stop}
    events = [{"kind":"depart_origin","source":"scenario_clock","at":stamp(0)},
              walk("ready_at_stop",180,225,180,"Platform A"),passage("board",300,"701","Platform A"),
              passage("alight",600,"701","Change A"),walk("ready_at_stop",660,75,60,"Change B"),
              passage("board",720,"901","Change B"),passage("alight",elapsed-60,"901","Platform C"),
              walk("arrive_destination",elapsed,75,60)]
    complete = {"walking_pace_mps":1.25, "transfer_allowance_seconds":0,"status":"complete_simulation",
                "events":events,"walking_seconds":300,"waiting_seconds":180,"riding_seconds":elapsed-480,"door_to_door_seconds_proxy":elapsed,
                "arrival_interval_ist":[(arrival+timedelta(seconds=i)).isoformat() for i in (30,50)]}
    boundary = {"walking_pace_mps":1.0,"transfer_allowance_seconds":0,"status":"boarding_interval_overlaps_readiness",
                "leg_reached":2,"events":events[:5],"boundary_witness":passage("board",660,"901","Change B")}
    atlas = {**case["providers"]["atlas"],"simulations":[complete,boundary]}
    google = {**case["providers"]["google"],"simulations":[{**boundary,"walking_pace_mps":1.25}],
              "diagnostics":{"published_walk_distances":[complete]}}
    atlas.pop("interim", None)
    google.pop("interim", None)
    near = {"state":"partial_or_failed", "providers":{"atlas":{"state":"captured","routes":["Trial route"],"captured_at":(datetime.fromisoformat(case["departure"])-timedelta(minutes=9)).isoformat(),
                                                       "arrival":arrival.isoformat(),"duration_seconds":1080,"arrival_change_seconds":0}}}
    fixture = {"cases":[{**case,"providers":{"atlas":atlas,"google":google},"near_departure":near}]}
    page.route("**/field-trials/results",lambda route:route.fulfill(json=fixture))
    page.get_by_role("button",name="Refresh results").click()
    expect(page.locator(".trial")).to_have_count(1)
    sim = page.get_by_role("table", name="Simulation",exact=True)
    expect(sim).to_contain_text("30 s to 50 s early")
    expect(sim).to_contain_text("Leg 2")
    expect(sim.get_by_role("row",name="Forecast compared with interval").locator("td").last).to_have_text("Unscored")
    atlas_stages=page.get_by_role("group",name="Delhi Bus Atlas stages")
    google_stages=page.get_by_role("group",name="Google Maps stages")
    expect(atlas_stages.get_by_role("listitem")).to_have_count(8)
    expect(atlas_stages).to_contain_text("Walk to destination")
    expect(google_stages.get_by_role("listitem")).to_have_count(5)
    expect(google_stages).to_contain_text("225 m · 3 min · modelled")
    expect(google_stages).to_contain_text("2 min to the first witnessed passage (midpoint estimate)")
    expect(google_stages).to_contain_text("GPS passage window " + (start+timedelta(seconds=290)).strftime("%H:%M:%S") +
                                          "–" + (start+timedelta(seconds=310)).strftime("%H:%M:%S"))
    expect(google_stages).to_contain_text("5 min between passage midpoints")
    expect(google_stages).to_contain_text("Transfer walk to Change B")
    expect(google_stages).to_contain_text("Boarding is uncertain")
    expect(google_stages).not_to_contain_text("Walk to destination")
    if page.viewport_size["width"] < 500:
        rectangles=[stages.bounding_box() for stages in (atlas_stages,google_stages)]
        assert rectangles[1]["y"] >= rectangles[0]["y"]+rectangles[0]["height"]-1
        page.set_viewport_size({"width":320,"height":740})
        assert page.evaluate("document.documentElement.scrollWidth<=window.innerWidth+1")
    page.get_by_text("Other ways to interpret incomplete GPS or walking paths",exact=True).click()
    expect(page.get_by_role("table",name="Alternative assumptions")).to_contain_text("Modelled arrival")
    page.get_by_text("Forecasts checked again near departure",exact=True).click()
    expect(page.get_by_role("table",name="Later forecast")).to_contain_text("Trial route")
    page.get_by_label("Walking and transfer assumptions").select_option("1/0")
    expect(sim).not_to_contain_text("30 s to 50 s early")
    expect(sim).to_contain_text("No result for these walking assumptions")
    expect(atlas_stages.get_by_role("listitem")).to_have_count(5)
    expect(google_stages).to_have_count(0)
    page.unroute("**/field-trials/results")
    page.route("**/field-trials/results",lambda route:route.fulfill(status=503,body="Unavailable"))
    page.get_by_role("button",name="Refresh results").click()
    expect(page.get_by_role("status")).to_contain_text("could not be loaded")
    expect(page.locator(".trial")).to_have_count(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            for width,height in ((1280,800),(390,844)):
                context = browser.new_context(viewport={"width":width,"height":height},is_mobile=width==390)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                verify(page,args.url.rstrip("/"))
                assert not errors,errors
                context.close()
        finally:
            browser.close()
    print("Prospective results verified on desktop and mobile.")


if __name__ == "__main__":
    main()
