"""Exercise the From and To controls against a running Atlas server with Playwright."""
import argparse
import re
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


# Hold one suggestion response past dismissal. A network reply can finish even if
# the client tried to cancel it. All real stop and journey queries still use Atlas.
HOLD_SUGGESTION = """(() => {
  const realFetch = window.fetch;
  window.fetch = (url, ...args) => {
    const request = new URL(url, location.href);
    if (request.pathname === "/stops" && request.searchParams.get("q") === "DelayedStop") {
      return new Promise(resolve => { window.releaseStop = () =>
        resolve(new Response(JSON.stringify({stops:[{id:"x",name:"DelayedStop",lat:28.6,lon:77.2}]})));
      });
    }
    return realFetch(url, ...args);
  };
})()"""


def selection(field):
    return field.evaluate("el => [el.selectionStart, el.selectionEnd]")


def open_planner(browser, base, mobile=False):
    context = browser.new_context(
        viewport={"width": 360 if mobile else 1280, "height": 800},
        is_mobile=mobile, has_touch=mobile, reduced_motion="reduce",
    )
    page = context.new_page()
    page.add_init_script(HOLD_SUGGESTION)
    with page.expect_response(lambda r: urlsplit(r.url).path == "/journeys", timeout=90000):
        page.goto(base, wait_until="domcontentloaded", timeout=90000)
    expect(page.locator("#alternatives .journey-choice").first).to_be_visible(timeout=90000)
    return context, page


def check_desktop(browser, base):
    context, page = open_planner(browser, base)
    origin = page.locator("#origin")
    destination = page.locator("#destination")
    searches = []
    page.on("request", lambda r: searches.append(r.url) if urlsplit(r.url).path == "/journeys" else None)
    origin.click()
    assert selection(origin) == [0, len(origin.input_value())], "First click must select the whole From value"
    origin.click(position={"x":18, "y":12})
    start, end = selection(origin)
    assert start == end, "A second click must allow placing the caret"
    page.locator("#use-location").focus()
    page.keyboard.press("Tab")
    expect(origin).to_be_focused()
    assert selection(origin) == [0, len(origin.input_value())]
    page.keyboard.press("Backspace")
    expect(origin).to_have_value("")
    expect(destination).to_have_value("Nehru Place Terminal")

    page.locator("#origin-map").click()
    page.locator("#map").click(position={"x":120, "y":130})
    expect(origin).to_have_value(re.compile(r"^28\.\d+, 77\.\d+$"))
    page.evaluate("""() => {
      Object.defineProperty(navigator, "geolocation", {value: {
        getCurrentPosition: (success) => {window.completeLocation = () => success({
          coords:{latitude:28.71,longitude:77.07,accuracy:5},timestamp:Date.now()
        })}
      }});
    }""")
    page.locator("#use-location").click()
    page.locator("#origin-clear").click()
    page.evaluate("completeLocation()")
    expect(origin).to_have_value("")
    expect(origin).to_be_focused()

    origin.fill("DelayedStop")
    page.wait_for_function("typeof window.releaseStop === 'function'")
    origin.press("Escape")
    # Let the delayed response and its rendering microtasks finish.
    page.evaluate("async () => {releaseStop(); await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))}")
    expect(origin).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#origin-list [role=option]")).to_have_count(0)
    origin.fill("NoSuchDelhiBusStopXYZ")
    expect(page.locator("#origin-search-status")).to_contain_text("no", ignore_case=True)
    page.locator("#origin-clear").click()
    expect(origin).to_have_value("")
    expect(origin).to_be_focused()
    origin.press_sequentially("Kashmere Gate City")
    choices = page.locator("#origin-list [role=option]")
    expect(choices.first).to_contain_text("Kashmere Gate City Bus Terminal")
    origin.press("ArrowDown")
    expect(choices.first).to_have_attribute("aria-selected", "true")
    origin.press("Enter")
    expect(origin).to_have_value("Kashmere Gate City Bus Terminal")
    expect(origin).to_have_attribute("aria-expanded", "false")

    page.locator("#destination-clear").click()
    expect(destination).to_be_focused()
    expect(destination).to_have_value("")
    expect(origin).to_have_value("Kashmere Gate City Bus Terminal")
    destination.fill("Nehru")
    expect(page.locator("#destination-list [role=option]").first).to_be_visible()
    destination.press("Tab")
    expect(destination).not_to_be_focused()
    expect(destination).to_have_attribute("aria-expanded", "false")
    with page.expect_response(lambda r: urlsplit(r.url).path == "/stops" and
                              parse_qs(urlsplit(r.url).query).get("q") == ["Nehru Place Terminal"]) as received:
        destination.fill("Nehru Place Terminal")
    matching = received.value.json()["stops"]
    options = page.locator("#destination-list [role=option]")
    expect(options.nth(1)).to_be_visible()
    assert matching[0]["name"] == matching[1]["name"] and matching[0]["id"] != matching[1]["id"]
    assert options.first.inner_text() != options.nth(1).inner_text(), "Duplicate names need distinguishable labels"
    options.first.click()
    destination.press("Tab")
    expect(destination).not_to_be_focused()
    assert not searches, "Editing and choosing a suggestion must leave submission to the rider"
    with page.expect_response(lambda r: urlsplit(r.url).path == "/journeys", timeout=90000) as planned:
        page.get_by_role("button", name="Find journeys").click()
    query = parse_qs(urlsplit(planned.value.url).query)
    assert query["from"] == ["1025"] and query["to"] == [str(matching[0]["id"])], query
    assert planned.value.ok and planned.value.json()["journeys"]
    print("Desktop selection, clearing, suggestions, dismissal and a real platform-to-platform itinerary passed.")
    context.close()


def check_touch(browser, base):
    context, page = open_planner(browser, base, mobile=True)
    origin = page.locator("#origin")
    destination = page.locator("#destination")
    destination.tap()
    assert selection(destination) == [0, len(destination.input_value())], "First tap must select the whole To value"
    destination.press("Backspace")
    expect(destination).to_have_value("")
    with page.expect_response(lambda r: urlsplit(r.url).path == "/stops") as received:
        destination.fill("Nehru Place Terminal")
    second = received.value.json()["stops"][1]
    option = page.locator("#destination-list [role=option]").nth(1)
    expect(option).to_be_visible()
    assert option.evaluate("""el => {
      const r=el.getBoundingClientRect();
      return r.left >= 0 && r.right <= innerWidth+1 &&
        el.contains(document.elementFromPoint(r.left+r.width/2, r.top+r.height/2));
    }"""), "A mobile suggestion must be inside the viewport and tappable"
    option.tap()
    expect(destination).to_have_value(second["name"])
    with page.expect_response(lambda r: urlsplit(r.url).path == "/journeys", timeout=90000) as result:
        page.get_by_role("button", name="Find journeys").tap()
    assert parse_qs(urlsplit(result.value.url).query)["to"] == [str(second["id"])]
    origin.tap()
    assert selection(origin) == [0, len(origin.input_value())]
    page.locator("#origin-clear").tap()
    expect(origin).to_have_value("")
    expect(destination).to_have_value(second["name"])
    print("Mobile tap-to-replace, clearing and selection of the second same-name platform passed.")
    context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        check_desktop(browser, args.url)
        check_touch(browser, args.url)
        browser.close()
