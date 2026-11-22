"""Compare the published fleet report to its desktop and mobile field-check views."""
import argparse

from playwright.sync_api import expect, sync_playwright


def verify(page, base):
    endpoint = "/validation?day_type=fleet_boarding"
    with page.expect_response(lambda response: response.url.endswith(endpoint)) as received:
        page.goto(base.rstrip("/") + "/field-checks#fleet-check")
    assert received.value.ok
    report = received.value.json()
    box = page.locator("#fleet-report")
    expect(box).to_be_visible()
    select = box.get_by_label("Compare boarding rules for")
    for group in ["holdout", "holdout_overnight", "development", "overnight", "all"]:
        select.select_option(group)
        actual = report["groups"][group]
        plat = report["platform"]["groups"][group]
        rows = box.locator("#fleet-platform tbody tr")
        expect(rows).to_have_count(4)
        for row_id, name in enumerate(("before_exit", "after_exit", "revised_approach", "unconfirmed")):
            v = [plat[name][label] for label in ("approaching", "gone", "uncertain", "unverified")]
            values = rows.nth(row_id).locator("td").all_text_contents()
            assert [int(s.replace(",", "")) for s in values] == [sum(v), *v]
        for name, policies in (("fleet-comparison", ("baseline", "revised", "removed", "added")),
                               ("fleet-defaults", ("baseline", "revised_default", "revised_secondary"))):
            rows = box.locator("#" + name + " tbody tr")
            expect(rows).to_have_count(5)
            for i, field in enumerate(("offered", "approaching", "gone", "uncertain", "unverified")):
                values = rows.nth(i).locator("td").all_text_contents()
                assert [int(v.replace(",", "")) for v in values] == [actual[k][field] for k in policies]
    box.locator("summary", has_text="Results by hour").click()
    expect(box.locator("#fleet-hours tbody tr")).to_have_count(24)
    for hour in (0, 1, 5, 23):
        values = box.locator("#fleet-hours tbody tr").nth(hour).locator("td").all_text_contents()
        ref = report["by_hour"][hour]
        r = ref["revised"]
        assert [int(v.replace(",", "")) for v in values] == [ref["cases"], ref["replayed"], r["offered"],
                                                              r["approaching"], r["gone"], r["uncertain"] + r["unverified"]]
    box.locator("summary", has_text="fresher approach").click()
    for i, band in enumerate(("0_15", "16_30", "31_60")):
        v = report["platform"]["holdout_approach_by_age"][band]
        values = box.locator("#fleet-age tbody tr").nth(i).locator("td").all_text_contents()
        assert [int(s.replace(",", "")) for s in values[:-1]] == [sum(v.values()), v["approaching"], v["gone"], v["uncertain"] + v["unverified"]]
    box.locator("summary", has_text="Find a route").click()
    search = box.get_by_label("Route name or GTFS route ID")
    search.fill("1996")
    expect(box.locator("#fleet-route-list li").first).to_contain_text("502UP")
    search.fill("does-not-match-any-delhi-route")
    expect(box.locator("#fleet-route-message")).to_have_text("No matching route ID or name.")
    box.locator("summary", has_text="Remaining failures").click()
    expect(box.locator("#fleet-examples li")).to_have_count(len(report["examples"]))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), "Fleet report overflows viewport"
    page.route("**/validation?day_type=fleet_boarding", lambda route: route.fulfill(json={**report, "complete": False}))
    page.reload()
    expect(page.locator("#fleet-status")).to_have_text("The complete October fleet report is unavailable.")
    expect(box).to_be_hidden()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width, height in [(1300, 840), (390, 844)]:
            context = browser.new_context(viewport={"width": width, "height": height}, is_mobile=width == 390)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            verify(page, args.url)
            assert not errors, errors
            context.close()
        browser.close()
    print("Fleet report verified on desktop and mobile.")


if __name__ == "__main__":
    main()
