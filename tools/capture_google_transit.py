"""Capture public Google Maps transit choices for a predeclared prospective trip."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / "data" / "whole-journey"
IST = timezone(timedelta(hours=5, minutes=30))
CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)", re.I)


def stamp():
    return datetime.now(IST).isoformat(timespec="seconds")


def maps_url(case):
    start, end = (",".join(map(str, case[key])) for key in ("origin", "destination"))
    wall_clock = datetime.fromisoformat(case["departure"]).replace(tzinfo=timezone.utc)
    return (f"https://www.google.com/maps/dir/{start}/{end}/data="
            f"!4m6!4m5!2m3!6e0!7e2!8j{int(wall_clock.timestamp())}!3e3?hl=en")


async def capture(browser, key, case, output):
    if datetime.now(IST) >= datetime.fromisoformat(case["departure"]):
        raise ValueError(f"Departure already passed for {key}")
    folder = output / key / "capture"
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "google-timing.json").exists():
        raise FileExistsError(f"{key} already has a Google capture")
    ctx = await browser.new_context(locale="en-IN", timezone_id="Asia/Kolkata",
                                    viewport={"width":1480,"height":960})
    page = await ctx.new_page()
    page.set_default_timeout(55000)
    started = stamp()
    try:
        await page.goto(maps_url(case), wait_until="domcontentloaded")
        await page.get_by_role("button", name="Options", exact=True).click()
        bus = page.get_by_role("checkbox", name="Bus", exact=True)
        if not await bus.is_checked():
            await bus.click()
        if not await bus.is_checked():
            raise ValueError("Bus preference could not be set")
        await page.get_by_role("button", name="Close", exact=True).click()
        directions = page.get_by_role("main", name="Directions")
        await directions.get_by_role("button", name="Details",exact=True).wait_for()
        await page.wait_for_timeout(2500)
        date = datetime.fromisoformat(case["departure"])
        expected_display = f"{date.strftime('%a, %b')} {date.day}"
        await directions.get_by_role("button", name=expected_display, exact=True).wait_for()
        screen_clock = await page.locator("input").evaluate_all("(els)=>els.filter(x=>x.getClientRects().length).map(x=>x.value)")
        short_clock = date.strftime("%I:%M %p").lstrip("0")
        if not any(short_clock in re.sub(r"\s+", " ", s).upper() for s in screen_clock):
            raise ValueError(f"Displayed time {screen_clock!r} does not confirm {short_clock}")
        panels = directions.get_by_role("link", name=re.compile(r"^Transit ")).or_(
            directions.get_by_role("button", name=re.compile(r"^Transit ")))
        labels = []
        candidates = []
        for i in range(await panels.count()):
            node = panels.nth(i)
            label = await node.get_attribute("aria-label") or await node.aria_snapshot()
            labels.append(label)
            if not re.search(r"\bBus\b",label) or re.search(r"Metro rail|Subway|Commuter train|Ladies|Women.only",label,re.I):
                continue
            clocks = CLOCK.findall(label)
            if len(clocks)<2:
                continue
            h,m,period = clocks[1]
            arrive = (int(h)%12+(12 if period.upper()=="PM" else 0))*60+int(m)
            depart = date.hour*60+date.minute
            candidates.append(((arrive-depart)%1440,i))
        if not candidates:
            raise ValueError(f"No comparable public bus result: {labels!r}")
        _, index = min(candidates)
        list_text = await directions.inner_text()
        list_url = page.url
        await page.screenshot(path=str(folder/"google-options.png"))
        node = panels.nth(index)
        if await node.get_attribute("role") != "link" and await node.evaluate("e=>e.tagName") != "A":
            await node.click()
        await directions.get_by_role("button", name="Details",exact=True).click()
        await page.wait_for_timeout(2000)
        details = await page.locator("body").inner_text()
        await page.screenshot(path=str(folder/"google.png"))
        recorded = stamp()
        if datetime.fromisoformat(recorded) >= date:
            raise ValueError("Google details were read after departure")
        contents = f"BUS PREFERENCE · DATE {expected_display} · TIME {short_clock}\n\nDISPLAYED OPTIONS\n{list_text}\n\nSELECTED ITINERARY\n{details}"
        (folder/"google.txt").write_text(contents,encoding="utf-8")
        (folder/"google-timing.json").write_text(json.dumps({
            "requested_at":started,"captured_at":recorded,"url":list_url,"detail_url":page.url,
            "date_control":expected_display,"time_control_values":screen_clock,
            "bus_preferred":True,"displayed_options":labels,"chosen_option":labels[index],
            "origin":case["origin"],"destination":case["destination"],"departure":case["departure"]
        },indent=2,ensure_ascii=False),encoding="utf-8")
        print(key, recorded, labels[index], details[-2200:],flush=True)
    finally:
        await ctx.close()


async def run(keys, output, plan=None):
    cases = json.loads((plan or (STUDY/"plan.json")).read_text())["cases"]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            results=await asyncio.gather(*(capture(browser,k,cases[k],output) for k in keys),return_exceptions=True)
        finally:
            await browser.close()
        for k,err in zip(keys,results):
            if isinstance(err,BaseException):
                print(k,repr(err),flush=True)
        if any(isinstance(err,BaseException) for err in results):
            raise SystemExit(1)


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases",nargs="+")
    parser.add_argument("--output",type=Path,default=STUDY)
    parser.add_argument("--plan",type=Path,default=STUDY/"plan.json")
    args=parser.parse_args()
    asyncio.run(run(args.cases,args.output,args.plan))

