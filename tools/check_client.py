"""Exercise the running local planner on desktop and mobile and optionally record a demo."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect


def check_month_evidence(page, base):
    url=base.rstrip("/")+"/field-checks#month-check"
    endpoint="/validation?day_type=month_boarding"
    if urlsplit(page.url).path=="/field-checks":
        page.goto("about:blank")
    with page.expect_response(lambda response:response.url.endswith(endpoint)) as received:
        page.goto(url)
    assert received.value.ok, "Completed October boarding report is unavailable"
    report=received.value.json()
    assert report["status"]=="complete"
    box=page.locator("#month-check")
    expect(box.get_by_role("heading",level=2)).to_be_visible()
    assert page.evaluate("location.hash==='#month-check'")
    table=box.get_by_role("table")
    expect(table.get_by_role("row")).to_have_count(6)
    fields=["offered","approaching","gone","uncertain","unverified"]
    for population,group in [("fresh",report["groups"]["fresh"]["policies"]),("all",report["policies"])]:
        box.get_by_label("Show results for").select_option(population)
        for index,field in enumerate(fields,start=1):
            row=table.get_by_role("row").nth(index)
            for column,policy in enumerate(["baseline","revised","suppressed"]):
                assert int(row.get_by_role("cell").nth(column).inner_text().replace(",",""))==group[policy][field]
    box.locator("summary").click()
    expect(box.locator("#month-limitations")).to_contain_text("DTC")
    census=report["census"]
    if "hour_bins_with_records" in census:
        expect(box.locator("#month-gaps")).to_contain_text(f"{census['hour_bins_with_records']} of {census['calendar_hour_bins']} calendar hours")
        expect(box.locator("#month-gaps")).to_contain_text(report["by_day"]["2026-10-07"]["last_receipt_clock"])
        expect(box.locator("#month-gaps")).to_contain_text(report["by_day"]["2026-10-08"]["first_receipt_clock"])
    if "502_diagnostic" in report.get("by_route_group",{}):
        route=report["by_route_group"]["502_diagnostic"]
        expect(box.locator("#month-502")).to_contain_text(f"{route['policies']['revised']['offered']} revised conditional offers")
    examples=[case for case in report.get("examples",[]) if case.get("route") and case.get("description")]
    expect(box.locator("#month-examples li")).to_have_count(len(examples))
    for example in examples:
        expect(box.locator("#month-examples")).to_contain_text(example["description"])
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+2"),"Month report overflow"
    # Even a populated response must stay hidden if its producer marks it incomplete.
    missing_day={day:item for day,item in report["by_day"].items() if day!="2026-10-07"}
    for invalid in [{"status":"partial"},{"calendar":{**report["calendar"],"processed_days":30}},{"by_day":missing_day}]:
        page.route("**"+endpoint,lambda route:route.fulfill(json={**report,**invalid}))
        try:
            with page.expect_response(lambda response:response.url.endswith(endpoint)) as broken:
                page.reload()
            assert all(broken.value.json()[key]==value for key,value in invalid.items())
            page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            expect(box).to_be_hidden()
            expect(page.locator("#month-jump")).to_be_hidden()
        finally:
            page.unroute("**"+endpoint)


def check_peak_evidence(page, base):
    with (
        page.expect_response(lambda response:response.url.endswith("/validation?day_type=near_miss")) as measured,
        page.expect_response(lambda response:response.url.endswith("/validation?day_type=near_miss_router")) as router_result,
        page.expect_response(lambda response:response.url.endswith("/validation?day_type=near_miss_confirmation")) as confirmed,
    ):
        page.goto(base.rstrip("/")+"/field-checks")
    assert measured.value.ok, "Peak-hour evidence is unavailable"
    peaks=measured.value.json()["test_by_peak"]
    panel=page.locator("#peak-check")
    expect(panel).to_be_visible()
    expect(panel.get_by_role("row")).to_have_count(7)
    for label,key in [("Cases meeting the timetable rule","offered"),("Still approaching","approaching"),("Already gone","gone"),
                      ("Timing uncertain","uncertain"),("Unverified","unverified"),("Approaching among classified buses","precision_among_definitive")]:
        row=panel.get_by_role("row").filter(has_text=label)
        for index,peak in enumerate(["AM","PM"]):
            value=peaks[peak]["thresholds"]["fallback_5min"]["30"][key]
            expected="—" if value is None else f"{100*value:.1f}%" if key=="precision_among_definitive" else f"{value:,}"
            expect(row.get_by_role("cell").nth(index)).to_have_text(expected)
    assert router_result.value.ok, "Journey-router peak replay is unavailable"
    stats=router_result.value.json()["overall"]
    router=panel.locator("#peak-router")
    expect(router).to_be_visible()
    offers=router.locator("#peak-router-offers")
    expect(offers).to_contain_text(f"Of {stats['attempted']:,} cases, the planner matched {stats['statically_matched_and_replayed']:,}")
    expect(offers).to_contain_text(f"offered a conditional journey in {stats['conditional_emitted']:,}")
    outcomes=stats["emitted_outcomes"]
    if stats["conditional_emitted"]:
        expect(router).to_contain_text(f"still approaching in {outcomes['approaching']:,} and already gone in {outcomes['gone']:,}")
        expect(router).to_contain_text(f"{outcomes['uncertain']+outcomes['unverified']:,} were unresolved ({outcomes['uncertain']:,} uncertain, {outcomes['unverified']:,} unverified)")
    else:
        expect(router).to_contain_text("no offered journeys to check")
    assert confirmed.value.ok, "October 29 confirmation is unavailable"
    confirmation=confirmed.value.json()
    assert confirmation["date"]=="2026-10-29"
    section=panel.locator("#peak-confirmation")
    expect(section).to_be_visible()
    groups=confirmation["router"]
    expect(section).to_contain_text(f"In {confirmation['positive_gap_1_30']} cases")
    expect(section).to_contain_text(f"The earlier rule offered {groups['baseline']['offered']} conditional")
    expect(section).to_contain_text(f"The revised rule offered {groups['revised']['offered']}.")
    for key,element in [("revised","#confirmation-offers"),("suppressed","#confirmation-suppressed")]:
        group=groups[key]
        if group["offered"]:
            expect(section.locator(element)).to_contain_text(
                f"{group['approaching']} approaching, {group['gone']} already gone, "
                f"{group['uncertain']} uncertain and {group['unverified']} unverified.")
        if key=="suppressed":
            label=f"withheld {group['offered']} option" if group["offered"] else "withheld no options"
            expect(section).to_contain_text(label)
    assert panel.locator("#peak-router, #peak-confirmation, #peak-timetable").evaluate_all(
        "nodes => nodes.map(n => n.id).join(',')")=="peak-router,peak-confirmation,peak-timetable"
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+2"),"Peak evidence overflow"


def check_field_evidence(page, base):
    with page.expect_response(lambda response:"/validation?day_type=matched" in response.url) as received:
        page.goto(base.rstrip("/")+"/field-checks")
    records=received.value.json()
    record=next(item for item in records if item["observed"]["route"]=="604UP")
    page.get_by_role("button",name="604UP",exact=True).click()
    expect(page.locator("#case-title")).to_have_text("604UP")
    table=page.locator("#case-times")
    expect(table.get_by_role("row").filter(has_text="Observed bus GPS")).to_contain_text("01:23:29")
    def ist_clock(stamp):
        return datetime.fromisoformat(stamp).astimezone(timezone(timedelta(hours=5,minutes=30))).strftime("%H:%M:%S")
    current=record["options"][0]["arrival"]
    expect(table.get_by_role("row").filter(has_text="Delhi Bus Atlas")).to_contain_text(ist_clock(current))
    previous=record.get("original_forecast")
    assert previous and previous["arrival"] != current, "604UP comparison is missing its earlier release"
    expect(page.locator("#previous")).to_contain_text(ist_clock(previous["arrival"]))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), "Field evidence overflow"


def check_walking_choices(page, base):
    page.goto(base)
    page.locator("#origin").fill("28.533043,77.152021")
    page.locator("#destination").fill("28.528843,77.157513")
    page.locator("#depart").fill("2026-11-21T01:08")
    page.get_by_role("button",name="Find journeys").click()
    walk=page.get_by_role("button",name="Show walking route",exact=False)
    expect(walk).to_be_visible(timeout=30000)
    first_bus=page.get_by_role("button",name="Journey 1,",exact=False)
    expect(first_bus).to_have_attribute("aria-pressed","true")
    bus_count=page.locator("#alternatives").get_by_role("button").count()
    assert bus_count>0
    walk.click()
    expect(page.locator("#details")).to_contain_text("Walking directions")
    expect(first_bus).to_have_attribute("aria-pressed","false")
    first_bus.click()
    expect(page.locator("#details")).to_contain_text("Journey details")
    expect(page.locator("#alternatives").get_by_role("button")).to_have_count(bus_count)
    # The timetable has expired on this date; the foot graph still connects these pins.
    page.locator("#depart").fill("2045-11-21T01:20")
    page.get_by_role("button",name="Find journeys").click()
    expect(walk).to_have_attribute("aria-pressed","true",timeout=30000)
    expect(page.locator("#walking-option")).to_contain_text("904 m")
    expect(page.locator("#details")).to_contain_text("1.25 m/s")
    expect(page.locator("#details")).to_contain_text("no mapped sidewalk")
    expect(page.locator("#alternatives").get_by_role("button")).to_have_count(0)


def check_conditional_boarding(page, base, screenshot=None):
    replay=base.rstrip("/")+"/?replay=502"
    with page.expect_response(lambda res:"/journeys?" in res.url and "replay=nov21" in res.url) as api:
        page.goto(replay)
    assert api.value.ok, api.value.text()
    captured=api.value.json()
    alert=next(item for item in captured["boarding_alerts"] if item.get("conditional_itinerary"))
    assert alert["seconds_before_ready"]==3 and not alert["catchable"]
    assert alert["vehicle_approaching"] is True and alert["gps_age_seconds"]==54
    possible=page.locator('[data-boarding="conditional_itinerary"]')
    later=page.locator('[data-boarding="fallback_itinerary"]')
    panel=page.locator("#boarding-options")
    expect(possible).to_have_attribute("aria-pressed","true")
    expect(page.locator("#result-context")).to_contain_text("Archived live feed")
    for label in ["502UP","01:17:57","01:18:00","3 seconds later","may already have left"]:
        expect(panel).to_contain_text(label)
    for stamp in ["01:17:57","01:18:00"]:
        expect(panel.locator(".boarding-context")).to_contain_text(stamp)
    expect(panel).not_to_contain_text("[object")
    expect(panel).to_contain_text("Board only if you see the bus taking passengers.")
    expect(panel).to_contain_text("54 seconds before this request")
    expect(possible).to_contain_text("01:19:42")
    expect(later).to_contain_text("02:46:01")
    expect(later).to_contain_text("02:49:44")
    expect(page.locator("#route-summary")).to_contain_text("01:19:42")
    expect(page.locator("#details")).to_contain_text("Downstream times move 3 s")
    expect(page.locator("#boarding-alerts")).to_be_empty()
    expect(page.locator("#map canvas").first).to_be_visible()
    expect(page.get_by_role("button",name="Compare with a 13:00 departure")).to_have_count(0)
    if screenshot:
        screenshot.parent.mkdir(exist_ok=True,parents=True)
        page.screenshot(path=screenshot,animations="disabled")
    later.click()
    expect(later).to_have_attribute("aria-pressed","true")
    expect(possible).to_have_attribute("aria-pressed","false")
    expect(page.locator("#details")).to_contain_text("Later bus details")
    expect(page.locator("#route-summary")).to_contain_text("02:49:44")
    possible.click()
    with page.expect_response(lambda res:"/arrivals?" in res.url and "replay=nov21" in res.url) as api:
        page.get_by_role("button",name="Check next bus at this stop").click()
    assert api.value.ok and api.value.json()["possible_boarding"]
    expect(page.locator("#platform-result")).to_contain_text("01:17:57")
    expect(page.locator("#platform-result")).to_contain_text("For a rider already at")
    expect(page.locator("#platform-result")).to_contain_text("54 seconds before this request")
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+2"),"Boarding options overflow"
    # Use exactly the recorded journey with its upstream evidence removed.
    # The rider can still open either itinerary, but uncertain GPS cannot select the possible bus.
    weakened = {**captured, "boarding_alerts":[dict(a,vehicle_approaching=False,gps_age_seconds=None)
                                               for a in captured["boarding_alerts"]]}
    replay_request = "**/journeys?**"
    page.route(replay_request,lambda route:route.fulfill(json=weakened))
    try:
        page.goto(replay)
        expect(possible).to_have_attribute("aria-pressed","false")
        expect(later).to_have_attribute("aria-pressed","true")
        expect(panel).to_contain_text("No recent GPS location")
        possible.click()
        expect(page.locator("#route-summary")).to_contain_text("01:19:42")
    finally:
        page.unroute(replay_request)
    # Hold strong upstream GPS fixed and exercise only the rider's selection boundary.
    for gap in (30, 31, 60):
        forecast = datetime.fromisoformat(alert['ready_at']) - timedelta(seconds=gap)
        option = {**alert['conditional_itinerary'], 'shift_seconds':gap}
        candidate = {**alert, 'seconds_before_ready':gap, 'predicted_departure':forecast.isoformat(),
                     'conditional_itinerary':option}
        page.route(replay_request,lambda route:route.fulfill(json={**captured,'boarding_alerts':[candidate]}))
        try:
            page.goto(replay)
            first_choice = possible if gap <= 30 else later
            expect(first_choice).to_have_attribute('aria-pressed','true')
            expect(panel.locator('button[data-boarding]').first).to_have_attribute('aria-pressed','true')
            possible.click()
            expect(page.locator('#details')).to_contain_text('If you board this bus')
        finally:
            page.unroute(replay_request)
    # A faster conditional route in the extended window stays selectable beside the original short-window offer.
    second = {**candidate, 'route':'Another bus', 'message':'The other bus may already have left.'}
    second['conditional_itinerary'] = {**second['conditional_itinerary'],
        'arrival':(datetime.fromisoformat(alert['conditional_itinerary']['arrival'])-timedelta(seconds=1)).isoformat()}
    page.route(replay_request,lambda route:route.fulfill(json={**captured,'boarding_alerts':[second,alert]}))
    try:
        page.goto(replay)
        expect(panel.get_by_role('heading')).to_contain_text('502UP')
        expect(possible).to_have_attribute('aria-pressed','true')
        page.get_by_role('button',name='Inspect Another bus and its later option').click()
        expect(panel.get_by_role('heading')).to_contain_text('Another bus')
        expect(possible).to_be_focused()
    finally:
        page.unroute(replay_request)
    page.locator('#origin').fill('10091')
    page.locator('#destination').fill('10092')
    with page.expect_response(lambda res:'/journeys?' in res.url and 'to=10092' in res.url) as incoming:
        page.get_by_role('button',name='Find journeys').click()
    assert incoming.value.ok
    wide = next(a for a in incoming.value.json()['boarding_alerts'] if a['route']=='764UP')
    assert (wide['seconds_before_ready'],wide['vehicle_approaching'],wide['catchable']) == (47,True,False)
    expect(panel).to_contain_text('47 seconds later')
    expect(possible).to_have_attribute('aria-pressed','false')
    expect(page.locator('#alternatives [aria-pressed="true"], [data-boarding="fallback_itinerary"][aria-pressed="true"]')).to_have_count(1)
    expect(later).to_contain_text('02:30:01')
    possible.click()
    expect(page.locator('#route-summary')).to_contain_text('01:18:25')
    with page.expect_response(lambda res:'/arrivals?' in res.url and 'route=2232' in res.url) as board:
        page.get_by_role('button',name='Check next bus at this stop').click()
    assert board.value.ok and board.value.json()['possible_boarding']['seconds_before_ready']==47
    page.locator("#depart").fill("2026-11-21T13:00")
    with page.expect_response(lambda res:"/journeys?" in res.url) as outside:
        page.get_by_role("button",name="Find journeys").click()
    assert "replay=" not in outside.value.url
    expect(panel).to_be_hidden()
    page.goto(base.rstrip("/")+"/field-checks")
    page.get_by_role("button",name="502UP",exact=True).click()
    expect(page.locator("#case-times")).to_contain_text("01:19:42")
    expect(page.locator("#case-times")).to_contain_text("02:49:44")
    expect(page.get_by_role("link",name="Compare the two 502UP journeys on the map")).to_have_attribute("href","/?replay=502")
    # Isolate the selection rule for the rider who must still walk to this platform.
    walk={"mode":"walk","seconds":20,"distance_m":25,"source":"osm","from":captured["origin"],
          "to":alert["platform"],"geometry":[],"departure":"2026-11-21T01:17:40+05:30","arrival":alert["ready_at"]}
    option={**alert["conditional_itinerary"],"legs":[walk,*alert["conditional_itinerary"]["legs"]]}
    access_alert={**alert,"access_walk_seconds":20,"conditional_itinerary":option}
    page.route("**/journeys?*",lambda route:route.fulfill(json={**captured,"boarding_alerts":[access_alert]}))
    try:
        page.goto(replay)
        expect(possible).to_have_attribute("aria-pressed","false")
        expect(panel).to_contain_text("25 m, 20 s walk")
        expect(page.locator('#alternatives [aria-pressed="true"], [data-boarding="fallback_itinerary"][aria-pressed="true"]')).to_have_count(1)
    finally:
        page.unroute("**/journeys?*")


def check(base, output, slow, screenshot=None, require_calls=False, require_arrivals=False,
          require_walking=False, require_conditional=False, conditional_screenshot=None, require_peak=False, require_month=False):
    errors = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        viewport={"width":1440,"height":900} if screenshot or conditional_screenshot else {"width":1366,"height":768}
        context = browser.new_context(viewport=viewport,
                                      **({"record_video_dir":temp,"record_video_size":viewport} if output else {}))
        page = context.new_page()
        page.on("pageerror", lambda e:errors.append(str(e)))
        page.goto(base)
        primary = page.get_by_role("button",name="Journey 1,",exact=False)
        expect(primary).to_be_visible(timeout=30000)
        expect(page.locator("#map canvas").first).to_be_visible(timeout=10000)
        expect(page.get_by_role("link",name="API guide")).to_have_attribute("href","/api-guide")
        expect(page.locator("#route-summary")).to_contain_text("Board")
        expect(primary).to_contain_text("total wait")
        expect(page.locator("#result-context")).to_contain_text("GPS replay")
        if screenshot:
            screenshot.parent.mkdir(exist_ok=True,parents=True)
            page.locator("body").focus()
            page.screenshot(path=screenshot,animations="disabled")
        if slow: page.wait_for_timeout(2600)
        page.get_by_role("button",name="Journey 3,",exact=False).click()
        expect(page.get_by_role("button",name="Journey 3,",exact=False)).to_have_attribute("aria-pressed","true")
        if slow: page.wait_for_timeout(2200)
        primary.click()
        page.get_by_role("button",name="View directions").click()
        expect(page.locator("#details")).to_be_focused()
        if require_arrivals:
            arrival_button=page.get_by_role("button",name="Check next bus at this stop")
            expect(arrival_button).to_have_count(1)
            with page.expect_response(lambda response:"/arrivals?" in response.url) as observed:
                arrival_button.click()
            assert observed.value.ok
            expect(page.locator("#platform-result")).to_contain_text("For a rider already at")
            if slow: page.wait_for_timeout(1600)
        stop_panel=page.locator(".bus-stops").first
        stop_panel.locator("summary").click()
        if require_calls:
            expect(page.locator(".walk-stages")).to_contain_text("To first stop")
            expect(page.locator(".walk-stages")).to_contain_text("From last stop")
            expect(stop_panel.get_by_role("table")).to_be_visible()
            expect(stop_panel.get_by_text("Prior segment",exact=True)).to_be_visible()
            assert stop_panel.locator(".stop-calls tbody tr").count()>1
            if slow: page.wait_for_timeout(1900)
        stop_panel.locator("summary").click()
        compare = page.get_by_role("button",name="Compare with a 13:00 departure")
        compare.scroll_into_view_if_needed()
        if slow: page.wait_for_timeout(2200)
        compare.click()
        expect(page.locator(".compare-result")).to_contain_text("arrives",timeout=25000)
        if slow: page.wait_for_timeout(2800)
        page.locator("#origin").fill("AIIMS")
        option = page.get_by_role("option").first
        expect(option).to_be_visible()
        page.locator("#origin").press("ArrowDown")
        page.locator("#origin").press("Enter")
        page.locator("#destination").fill("Lajpat Nagar")
        page.get_by_role("option").first.click()
        page.get_by_role("button",name="Find journeys").click()
        expect(primary).to_be_visible(timeout=30000)
        if slow: page.wait_for_timeout(3000)
        page.locator("#feed-toggle").click()
        expect(page.locator("#feed-panel")).to_be_visible()
        if slow: page.wait_for_timeout(2400)
        if require_calls:
            page.locator("#feed-toggle").click()
            page.get_by_role("link",name="API guide").click()
            page.get_by_role("button",name="Run request").click()
            expect(page.locator("#rows tr")).to_have_count(3,timeout=30000)
            if slow: page.wait_for_timeout(2300)
        if require_walking:
            check_field_evidence(page,base)
        if require_conditional:
            check_conditional_boarding(page,base,conditional_screenshot)
        if require_peak:
            check_peak_evidence(page,base)
        if require_month:
            check_month_evidence(page,base)
        video = page.video
        context.close()
        if output:
            ffmpeg = shutil.which("ffmpeg")
            if not ffmpeg:
                raise RuntimeError("FFmpeg is required for an MP4 demo.")
            output.parent.mkdir(exist_ok=True, parents=True)
            subprocess.run([ffmpeg,"-y","-loglevel","error","-i",str(video.path()),"-an","-c:v","libx264",
                            "-preset","veryfast","-pix_fmt","yuv420p","-movflags","+faststart",str(output)],check=True)
        mobile = browser.new_context(viewport={"width":390,"height":844},is_mobile=True,has_touch=True)
        mp = mobile.new_page()
        mp.on("pageerror",lambda e:errors.append(str(e)))
        with mp.expect_response(lambda response:"/journeys?" in response.url) as loaded:
            mp.goto(base)
        ordinary=loaded.value.json()
        expect(mp.get_by_role("button",name="Journey 1,",exact=False)).to_be_visible(timeout=30000)
        notices=mp.locator("#boarding-alerts")
        expect(notices).to_be_empty()
        assert mp.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), "Mobile horizontal overflow"
        mp.locator("#origin-map").click()
        expect(mp.locator("#map-prompt")).to_be_visible()
        mp.locator("#cancel-pick").click()
        expect(mp.locator("#map-prompt")).to_be_hidden()
        mp.locator("#origin-map").click()
        mp.locator("#map").focus()
        mp.keyboard.press("ArrowLeft")
        mp.keyboard.press("Enter")
        expect(mp.locator("#origin")).to_have_value(re.compile(r"^28\.\d+, 77\.\d+$"))
        mp.locator("#origin").fill("0,0")
        mp.get_by_role("button",name="Find journeys").click()
        expect(mp.locator("#message")).to_contain_text("No mapped walking connection",timeout=30000)
        expect(mp.locator("#alternatives").get_by_role("button")).to_have_count(0)
        message="The observed departure is 3 seconds before your estimated platform arrival."
        alert={"route":"502UP","catchable":False,"source":"live","message":message}
        mp.route("**/journeys?*",lambda route:route.fulfill(json={**ordinary,"boarding_alerts":[alert]}))
        mp.locator("#origin").fill("Kashmere Gate City Bus Terminal")
        mp.get_by_role("button",name="Find journeys").click()
        expect(notices).to_contain_text(message)
        expect(notices).to_have_attribute("role","status")
        expect(mp.locator("#boarding-options")).to_be_hidden()
        mp.unroute("**/journeys?*")
        mp.get_by_role("button",name="Find journeys").click()
        expect(mp.get_by_role("button",name="Journey 1,",exact=False)).to_be_visible(timeout=30000)
        expect(notices).to_be_empty()
        mp.set_viewport_size({"width":320,"height":740})
        assert mp.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), "320px planner overflow"
        if require_calls:
            mp.goto(base.rstrip("/")+"/api-guide")
            mp.get_by_role("button",name="Run request").click()
            expect(mp.locator("#rows tr")).to_have_count(3,timeout=30000)
            assert mp.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), "Mobile API guide overflow"
            if require_arrivals:
                swagger_requests=[]
                mp.on("request",lambda request:swagger_requests.append(request.url))
                mp.goto(base.rstrip("/")+"/docs")
                expect(mp.locator(".swagger-ui .opblock-summary-path").filter(has_text="/arrivals").first).to_be_visible(timeout=15000)
                assert all(urlsplit(url).netloc==urlsplit(base).netloc for url in swagger_requests
                           if url.startswith(("http://","https://"))), "Swagger requested an external asset"
        if require_walking:
            check_field_evidence(mp,base)
            check_walking_choices(mp,base)
        if require_conditional:
            mp.set_viewport_size({"width":390,"height":844})
            check_conditional_boarding(mp,base)
        if require_peak:
            check_peak_evidence(mp,base)
        if require_month:
            check_month_evidence(mp,base)
        mobile.close()
        browser.close()
    if errors:
        raise RuntimeError(errors)
    print("Passed desktop journey timing, directions, route selection, comparison, stop search, API navigation, and mobile map-pin and unreachable-trip tests.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url",default="http://127.0.0.1:8765/")
    p.add_argument("--demo",type=Path)
    p.add_argument("--screenshot",type=Path,help="Save the desktop rider view as a PNG.")
    p.add_argument("--require-calls",action="store_true",help="Require per-stop API timings and a working API guide.")
    p.add_argument("--require-arrivals",action="store_true",help="Verify the first boarding platform arrival lookup.")
    p.add_argument("--require-walking",action="store_true",help="Verify mapped walking and matched field evidence.")
    p.add_argument("--require-conditional",action="store_true",help="Exercise the archived 502UP boarding decision on desktop and mobile.")
    p.add_argument("--conditional-screenshot",type=Path,help="Save the archived 502UP boarding decision on desktop.")
    p.add_argument("--require-peak",action="store_true",help="Check measured rush-hour counts against the API on desktop and mobile.")
    p.add_argument("--require-month",action="store_true",help="Check held-out and whole-month counts and partial-report handling.")
    args = p.parse_args()
    check(args.url,args.demo,bool(args.demo),args.screenshot,args.require_calls,args.require_arrivals,args.require_walking,
          args.require_conditional or bool(args.conditional_screenshot),args.conditional_screenshot,args.require_peak,args.require_month)
