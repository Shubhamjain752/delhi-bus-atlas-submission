"""Check the Rohini route choice and its departure evidence in a signed-out browser."""

import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


ORIGIN = "28.71815,77.06380"
DESTINATION = "28.70700,77.17890"
DEPARTURE = "2026-11-24T17:30"


def is_search(response, preference):
    parsed = urlsplit(response.url)
    query = parse_qs(parsed.query)
    return (
        parsed.path == "/journeys" and query.get("preference") == [preference]
        and query.get("from", [""])[0] in (ORIGIN, "28.71815,77.0638")
        and query.get("to", [""])[0] in (DESTINATION, "28.707,77.1789")
        and query.get("depart", [""])[0].startswith(DEPARTURE)
    )


def check(browser, base, width, output=None):
    context = browser.new_context(timezone_id="Asia/Kolkata", viewport={"width": width, "height": 900})
    try:
        assert not context.cookies()
        page = context.new_page()
        page_errors = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        if width < 500:
            with page.expect_response(lambda r: is_search(r, "balanced"), timeout=120000) as response:
                assert page.goto(base.rstrip("/") + "/?example=rohini-evening",
                                 wait_until="domcontentloaded", timeout=90000).ok
            fields = {"origin": "Rohini Sector 22", "destination": "Near Azadpur Terminal", "depart": DEPARTURE}
        else:
            assert page.goto(base, wait_until="domcontentloaded", timeout=90000).ok
            fields = {"origin": ORIGIN, "destination": DESTINATION, "depart": DEPARTURE}
            for field, value in fields.items():
                page.locator("#" + field).fill(value)
            with page.expect_response(lambda r: is_search(r, "balanced"), timeout=120000) as response:
                page.get_by_role("button", name="Find journeys").click()
        assert response.value.ok
        cards = page.locator("#alternatives .journey-choice")
        expect(cards.first).to_contain_text("102ADOWN")
        order = page.get_by_label("Show routes by")
        expect(order).to_have_value("balanced")
        with page.expect_response(lambda r: is_search(r, "earliest"), timeout=120000) as response:
            order.select_option("earliest")
        assert response.value.ok
        data = response.value.json()
        journey = data["journeys"][0]
        buses = [leg for leg in journey["legs"] if leg["mode"] == "bus"]
        assert [leg["route"] for leg in buses] == ["808STDOWN", "174DOWN"]
        assert buses[0]["departure"][11:19] == "17:44:46"
        evidence = buses[0]["origin_departure"]
        assert evidence["status"] == "unconfirmed"
        assert evidence["as_of"][11:16] == "17:30" and evidence["scheduled_at"][11:16] == "17:25"
        expect(cards.first).to_contain_text("808STDOWN")
        expect(cards.first).to_contain_text("Departure from first stop unconfirmed by GPS")
        expect(page.locator("#alternatives .journey-choice:visible")).to_have_count(min(3, len(data["journeys"])))
        expect(cards.first).to_have_attribute("aria-pressed", "true")
        leg_status = page.locator("#details details.departure-evidence")
        expect(leg_status).to_have_count(2)
        leg_status.first.locator("summary").click()
        expect(leg_status.first).to_contain_text("17:25")
        expect(leg_status.first).to_contain_text(evidence["origin"]["name"])
        expect(leg_status.first).to_contain_text("17:30 IST on 24 Nov")
        if output:
            output.mkdir(parents=True, exist_ok=True)
            leg_status.first.screenshot(path=output / f"departure-evidence-{width}.png")
        with page.expect_response(lambda r: is_search(r, "balanced"), timeout=120000):
            order.select_option("balanced")
        expect(cards.first).to_contain_text("102ADOWN")
        for field, value in fields.items():
            expect(page.locator("#" + field)).to_have_value(value)
        assert not page_errors, page_errors
        return {"width": width, "recommended": "102ADOWN", "earliest": [leg["route"] for leg in buses],
                "boarding": buses[0]["departure"], "arrival": journey["arrival"], "start_status": evidence["status"]}
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            results = [check(browser, args.url, width, args.output) for width in (1360, 390)]
        finally:
            browser.close()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
