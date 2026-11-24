"""Check all five Vivek Vihar options through the public passenger form."""

import argparse
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


ORIGIN = "Girls School Vivek Vihar"
DESTINATION = "Rajiv Chowk Metro Station"
DEPARTURE = "2026-11-24T10:57"
SHORT_WALK = ("243DOWN", "943UP")
EARLIER = ("473UP", "317DOWN")


def is_test_search(response):
    parsed = urlsplit(response.url)
    query = parse_qs(parsed.query)
    return (
        parsed.path == "/journeys"
        and query.get("from") == [ORIGIN]
        and query.get("to") == [DESTINATION]
        and query.get("depart", [""])[0].startswith(DEPARTURE)
    )


def check(browser, base, width, output):
    context = browser.new_context(
        timezone_id="Asia/Kolkata", viewport={"width": width, "height": 900}
    )
    try:
        assert not context.cookies()
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        assert page.goto(base, wait_until="domcontentloaded", timeout=90000).ok
        for selector, value in (
            ("#origin", ORIGIN), ("#destination", DESTINATION), ("#depart", DEPARTURE)
        ):
            page.locator(selector).fill(value)
        with page.expect_response(is_test_search, timeout=120000) as pending:
            page.get_by_role("button", name="Find journeys").click()
        response = pending.value
        assert response.ok, (response.status, response.url)
        data = response.json()
        trips = data["journeys"]
        policy = data["balanced_choices"]
        assert policy["time_saved_seconds"] == 463 and policy["extra_walk_seconds"] == 690
        choices = []
        for trip in trips:
            routes = tuple(leg["route"] for leg in trip["legs"] if leg["mode"] == "bus")
            assert trip["duration_seconds"] == sum(
                trip[field] for field in ("walk_seconds", "wait_seconds", "ride_seconds")
            )
            choices.append((routes, trip))
        assert choices[0][0] == SHORT_WALK, [routes for routes, _ in choices]
        by_routes = dict(choices)
        assert EARLIER in by_routes, list(by_routes)
        earlier = by_routes[EARLIER]
        short = choices[0][1]
        assert short["arrival"][11:19] == "12:20:39" and short["walk_seconds"] == 635
        assert earlier["arrival"][11:19] == "12:12:56" and earlier["walk_seconds"] == 1325

        cards = page.locator("#alternatives .journey-choice")
        first = cards.filter(has_text=SHORT_WALK[0]).filter(has_text=SHORT_WALK[1]).first
        faster = cards.filter(has_text=EARLIER[0]).filter(has_text=EARLIER[1]).first
        expect(first).to_be_visible()
        expect(first).to_have_attribute("aria-pressed", "true")
        expect(faster).to_be_visible()
        expect(first).to_contain_text("Less walking")
        expect(faster).to_contain_text("8 min sooner")
        expect(faster).to_contain_text("12 min more walking")
        visible_cards = page.locator("#alternatives .journey-choice:visible").all_inner_texts()
        assert len(visible_cards) == policy["primary_count"] == 3, visible_cards
        assert len(by_routes) == len(choices) == 5
        assert choices[1][0] == EARLIER
        for index, (routes, _) in enumerate(choices[:3]):
            expect(cards.nth(index)).to_be_visible()
            for route in routes:
                expect(cards.nth(index)).to_contain_text(route)
        if output:
            output.mkdir(exist_ok=True, parents=True)
            page.locator("#alternatives").screenshot(path=output / f"journey-choices-{width}.png")
        disclosure = page.locator("#alternatives details")
        hidden_cards = disclosure.locator(".journey-choice")
        assert hidden_cards.count() == len(trips) - len(visible_cards) == 2
        expect(hidden_cards.first).to_be_hidden()
        disclosure.locator("summary").click()
        for index, (routes, _) in enumerate(choices[3:]):
            card = hidden_cards.nth(index)
            expect(card).to_be_visible()
            card.click()
            expect(card).to_have_attribute("aria-pressed", "true")
            for route in routes:
                expect(page.locator("#details")).to_contain_text(route)
        disclosure.locator("summary").click()
        expect(hidden_cards.first).to_be_hidden()
        cards.nth(2).click()
        expect(cards.nth(2)).to_have_attribute("aria-pressed", "true")
        for route in choices[2][0]:
            expect(page.locator("#details")).to_contain_text(route)
        faster.click()
        expect(faster).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#details")).to_contain_text(EARLIER[1])
        first.click()
        expect(first).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#details")).to_contain_text(SHORT_WALK[1])
        assert not errors, errors
        return {"width": width, "url": response.url, "visible_cards": visible_cards,
                "choices": [{"routes": routes, "arrival": trip["arrival"], "walk_seconds": trip["walk_seconds"],
                             "duration_seconds": trip["duration_seconds"]} for routes, trip in choices]}
    finally:
        context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, help="Save screenshots and the visible result")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            result = [check(browser, args.url, width, args.output) for width in (1360, 390)]
        finally:
            browser.close()
    report = json.dumps(result, ensure_ascii=False, indent=2)
    print(report)
    if args.output:
        (args.output / "checked-result.json").write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
