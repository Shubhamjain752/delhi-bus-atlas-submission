"""Check the Rohini journey, platform history and sparse-data states in a guest browser."""
import argparse
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def rohini_request(response):
    url = urlsplit(response.url)
    return url.path == "/journeys" and parse_qs(url.query).get("depart",[""])[0].startswith("2026-11-24T17:30")


def submit(page):
    with page.expect_response(rohini_request, timeout=120_000) as pending:
        page.get_by_role("button",name="Find journeys").click()
    response = pending.value
    assert response.ok, response.status
    return response.json()


def check(browser, base, width):
    context = browser.new_context(viewport={"width":width,"height":880}, timezone_id="Asia/Kolkata")
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.goto(base,wait_until="domcontentloaded",timeout=120_000)
    page.locator("#origin").fill("28.71815,77.0638")
    page.locator("#destination").fill("28.707,77.1789")
    page.locator("#depart").fill("2026-11-24T17:30")
    data = submit(page)
    choice = page.locator("#alternatives .journey-choice").first
    expect(choice).to_contain_text("808STDOWN")
    expect(choice).to_contain_text("Start unconfirmed")
    bus = next(l for l in data["journeys"][0]["legs"] if l["mode"]=="bus")
    assert bus["origin_departure"]["origin"]["name"] == "Rohini Sec 37 E Bus Depot"
    details = page.locator("#details .departure-evidence").first
    details.locator("summary").click()
    expect(details).to_contain_text("Rohini Sec 37 E Bus Depot at 17:25")
    expect(details).to_contain_text("no GPS confirmation")
    assert "service_history" in bus
    expect(details).to_contain_text("GPS recorded "+str(bus["service_history"]["observed"])+" movements")
    if "scheduled_start_history" in bus:
        hit = bus["scheduled_start_history"]
        expect(details).to_contain_text(f"GPS matched {hit['matched']} of {hit['scheduled']}")
    history = bus["platform_gap_history"]
    expect(details).to_contain_text(history["platform"]["name"]+" were typically")
    with page.expect_response(lambda r:urlsplit(r.url).path=="/arrivals") as at_stop:
        page.get_by_role("button",name="Check next bus at this stop").click()
    board = at_stop.value.json()
    same_stop = page.locator("#platform-result")
    expect(same_stop).to_contain_text("For a rider already at")
    assert board["platform_gap_history"]["platform"]["id"] == bus["from"]["id"]
    expect(same_stop).to_contain_text("gaps across")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # Exercise optional fields omitted by older and low-coverage responses.
    sparse = deepcopy(data)
    for journey in sparse["journeys"]:
        for leg in journey["legs"]:
            for key in ("origin_departure","service_history","scheduled_start_history","platform_gap_history"):
                leg.pop(key,None)
    page.route("**/journeys?*",lambda route:route.fulfill(json=sparse))
    submit(page)
    expect(choice).to_contain_text("808STDOWN")
    expect(choice).not_to_contain_text("Start unconfirmed")
    expect(details).to_have_count(0)
    # A display fixture also exercises a positively matched physical run, independently
    # of whether a tracked vehicle was available for the archived Rohini departure.
    page.unroute("**/journeys?*")
    supported = deepcopy(data)
    first = next(l for l in supported["journeys"][0]["legs"] if l["mode"]=="bus")
    first["origin_departure"].update(status="seen_in_service", observed_stop=first["from"],
                                    observed_at=data["issued_asof"], received_at=data["issued_asof"])
    page.route("**/journeys?*",lambda route:route.fulfill(json=supported))
    submit(page)
    expect(choice).to_contain_text("GPS saw this bus in service at 17:30")
    details.locator("summary").click()
    expect(details).to_contain_text("GPS recorded this bus farther along the route")
    assert not errors, errors
    print(f"{width}px: full form, origin, history, platform board and missing/confirmed display fixtures passed")
    context.close()


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args=parser.parse_args()
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        for width in (1360,390):
            check(browser,args.url,width)
        browser.close()
