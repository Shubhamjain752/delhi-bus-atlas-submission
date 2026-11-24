"""Check how the planner handles slow or unavailable browser location without using a device's location."""
import argparse
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


# Stand in for the device provider. The test controls when a pending callback
# arrives and honours the caller's timeout and maximumAge, without real sleeps.
FAKE_LOCATION = """
window.geoCalls = [];
Object.defineProperty(navigator, "geolocation", {value: {
  getCurrentPosition(success, failure, options) {
    window.geoCalls.push({success, failure, options});
  }
}});
window.deliverLocation = ({elapsed=0, age=0, accuracy=8, error=0}) => {
  const {success, failure, options} = window.geoCalls.at(-1);
  if (error || elapsed > options.timeout || age > options.maximumAge) {
    failure({code: error || 3});
  } else {
    success({coords:{latitude:28.71815, longitude:77.0638, accuracy}, timestamp:Date.now()-age});
  }
};
"""


def check(base):
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page()
        page.add_init_script(FAKE_LOCATION)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        with page.expect_response(lambda response: urlsplit(response.url).path == "/journeys", timeout=90_000):
            page.goto(base, wait_until="domcontentloaded", timeout=90_000)
        button = page.get_by_role("button", name="Use my location")
        origin = page.locator("#origin")
        status = page.locator("#location-status")
        assert page.evaluate("geoCalls.length") == 0
        searches = []
        page.on("request", lambda request: searches.append(request.url) if urlsplit(request.url).path == "/journeys" else None)

        def start():
            origin.fill("Kashmere Gate City Bus Terminal")
            page.evaluate("geoCalls.length = 0")
            button.click()
            assert page.evaluate("geoCalls.length") == 1

        def deliver(**result):
            page.evaluate("result => deliverLocation(result)", result)

        start()
        deliver(elapsed=12_000)
        assert origin.input_value() == "Current location", f"12-second fix failed: {status.inner_text()}"
        assert page.evaluate("geoCalls.length") == 1
        assert not page.evaluate("geoCalls[0].options.enableHighAccuracy")
        print("A simulated 12-second location fix was accepted.")

        start()
        deliver(age=30_000, accuracy=200)
        expect(origin).to_have_value("Current location")
        expect(status).to_contain_text("200")
        expect(status).to_contain_text("30")
        expect(status).to_contain_text("map pin")
        print("A recent cached fix was accepted with its age and accuracy shown.")

        for error in (2, 3):  # Device temporarily unavailable or request timed out.
            start()
            deliver(error=error)
            assert page.evaluate("geoCalls.length") == 2
            assert page.evaluate("geoCalls[1].options.enableHighAccuracy && geoCalls[1].options.maximumAge === 0")
            expect(button).to_be_disabled()
            deliver(elapsed=18_000)
            expect(origin).to_have_value("Current location")
            expect(button).to_be_enabled()
        print("An unavailable first fix and a timed-out first fix each recovered on the second attempt.")

        for first, second, expected_calls in [(1, None, 1), (3, 3, 2), (3, 1, 2)]:
            start()
            deliver(error=first)
            if second:
                deliver(error=second)
            assert page.evaluate("geoCalls.length") == expected_calls
            expect(button).to_be_enabled()
            expect(origin).to_have_value("Kashmere Gate City Bus Terminal")
            expect(status).to_contain_text("map pin")
        print("Denied permission and exhausted attempts kept the original starting point.")

        start()
        origin.fill("AIIMS")
        deliver(error=3)
        assert page.evaluate("geoCalls.length") == 1
        expect(origin).to_have_value("AIIMS")

        start()
        deliver(error=3)
        origin.fill("AIIMS")
        deliver(elapsed=18_000)
        expect(origin).to_have_value("AIIMS")
        print("Editing From cancelled both a pending retry and a late location update.")
        assert not searches, f"Location triggered a journey search without submission: {searches}"
        assert not errors, errors
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/")
    check(parser.parse_args().url)
