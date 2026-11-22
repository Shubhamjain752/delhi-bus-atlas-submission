"""Replay the archived 949DOWN /arrivals response in the real passenger interface."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def check(base):
    report = json.loads((ROOT/'data/fleet-arrivals-fallback.json').read_text(encoding='utf-8'))
    row, = (r for r in report['examples'] if r.get('route')=='949DOWN')
    actual = row['endpoint_with_causal_history']
    observation = actual['route_observation']
    assert actual['source']=='timetable' and actual['wait_seconds']==79
    assert observation['trip_assignment']=='unmatched_start_clock' and observation['direction']=='toward_platform'
    assert not {'arrival','wait_seconds','predicted_departure','eta','catchable'} & observation.keys()
    issue = datetime.fromisoformat(actual['at'])
    old_forecast = issue + timedelta(seconds=row['previous']['wait_seconds'])
    wanted = {'stop':[actual['platform']['id']],'route':[actual['route_id']],'at':[actual['at']]}
    requests = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={'width':1280,'height':800},
                                    timezone_id='America/Los_Angeles',reduced_motion='reduce')
            page.set_default_timeout(60000)

            def historical_platform(route):
                requests.append(parse_qs(urlsplit(route.request.url).query))
                route.fulfill(json=actual)

            page.route('**/arrivals?*',historical_platform)
            page.goto(base.rstrip('/')+'/',wait_until='networkidle',timeout=120000)
            # The archive retains seconds. Populate the application's regular search
            # state with that exact instant and invoke its ordinary submit handler.
            # The journey request, routing response and selection controls are live.
            with page.expect_response(lambda r:'/journeys?' in r.url and 'from=7918' in r.url) as network:
                page.evaluate("""async ({origin,at}) => {
                    setPoint('origin',origin);
                    setPoint('destination',{id:'826',name:'Delhi Secretariat'});
                    document.querySelector('#depart').value=at;
                    await searchJourney(true);
                }""", {'origin':actual['platform'],'at':issue.replace(tzinfo=None).isoformat()})
            assert network.value.ok
            journey = network.value.json()
            assert journey['issued_asof']==actual['at']
            direct, = (i for i,j in enumerate(journey['journeys']) if any(
                leg['mode']=='bus' and leg['route_id']==actual['route_id'] and leg['from']['id']==row['target']
                and leg['to']['id']=='826' for leg in j['legs']))
            page.locator(f'#alternatives .journey-choice[data-index="{direct}"]').click()

            for width,height in ((1280,800),(390,844)):
                page.set_viewport_size({'width':width,'height':height})
                button = page.get_by_role('button',name='Check next bus at this stop')
                expect(button).to_be_visible()
                with page.expect_response(lambda r:'/arrivals?' in r.url) as response:
                    button.click()
                assert requests[-1]==wanted and response.value.json()==actual
                panel = page.locator('#platform-result')
                expect(panel).to_have_attribute('role','status')
                expect(panel.get_by_text('Bus seen upstream',exact=True)).to_be_visible()
                headings = panel.locator('strong').all_inner_texts()
                assert headings==['Next 949DOWN at 18:30 · 1 min wait','Bus seen upstream'],headings
                children = panel.locator(':scope > *').all_inner_texts()
                assert any('timetable' in s.lower() for s in children[2:children.index('Bus seen upstream')])
                assert children[-1]==('949DOWN was seen at Anand Parbat Industrial Area at 18:28:59, '
                    'travelling toward this stop. Its timetable trip and arrival time are unconfirmed.')
                text = panel.inner_text()
                assert actual['platform']['name'] in text and '18:29 IST on 22 Oct' in text
                # Also inspect non-visible or assistive text in the rider panel.
                accessible = panel.evaluate("""node => [node.textContent,...Array.from(
                    node.querySelectorAll('[aria-label],[title]'), e=>[e.ariaLabel,e.title].join(' '))].join(' ')""")
                for wrong in (old_forecast.strftime('%H:%M'),'130 seconds','130 s','2 min wait','Possible boarding'):
                    assert wrong.casefold() not in accessible.casefold(),(wrong,accessible)
                assert not re.search(r'\b(?:wait|ETA|in \d+ min)\b',children[-1],re.I)
                assert panel.evaluate('el => el.scrollWidth<=el.clientWidth+1')
                print(f'{width}px: '+ ' / '.join(children),flush=True)
            assert len(requests)==2
        finally:
            browser.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8765')
    check(parser.parse_args().url)
