"""Verify the earlier-departure view with the AIIMS Sunday example on a running planner."""
import argparse
from datetime import datetime

from playwright.sync_api import expect, sync_playwright


def check(page, base):
    with page.expect_response(lambda response: "/journeys?" in response.url) as load:
        page.goto(base + "/?example=aiims")
    assert load.value.ok
    result = load.value.json()
    early = next(s for s in result["earlier_boarding_options"] if s["route"] == "433UP")
    assert result["departure"] <= early["departure_at"] < early["walk_ready_at"]
    expect(page.locator("#alternatives .journey-choice").first).to_contain_text("711UP")

    panel = page.locator("#earlier-departures")
    panel.locator("summary").click()
    stop = panel.locator(".nearby-stop").filter(
        has=page.get_by_text(early["boarding_stop"]["name"], exact=True)
    ).filter(has=page.get_by_text("433UP · due", exact=False)).first
    line = stop.get_by_role("listitem").filter(has_text="433UP · due").first
    expect(line).to_contain_text(datetime.fromisoformat(early["departure_at"]).strftime("%H:%M"))
    expect(line).to_contain_text(early["alighting_stop"]["name"])
    expect(stop).to_contain_text(datetime.fromisoformat(early["walk_ready_at"]).strftime("%H:%M"))
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 2")
    stop.get_by_role("button", name="Plan from this stop").click()
    expect(page.locator("#origin")).to_have_value(early["boarding_stop"]["name"])
    expect(page.locator("#depart")).to_be_focused()
    with page.expect_response(lambda response: "/journeys?" in response.url) as replanned:
        page.get_by_role("button", name="Find journeys").click()
    assert replanned.value.ok
    from_stop = replanned.value.json()
    assert from_stop["origin"]["id"] == early["boarding_stop"]["id"]
    assert all(option["boarding_stop"]["id"] != early["boarding_stop"]["id"]
               for option in from_stop["earlier_boarding_options"])
    assert not panel.evaluate("details => details.open")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    base = parser.parse_args().url.rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for mobile in (False, True):
            page = browser.new_page(viewport={"width": 390 if mobile else 1440, "height": 844},
                                    is_mobile=mobile, has_touch=mobile)
            page.set_default_timeout(30000)
            check(page, base)
            page.close()
        browser.close()
    print("Passed the AIIMS 433UP earlier-departure display and stop-based replanning on desktop and mobile.")
