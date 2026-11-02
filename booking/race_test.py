"""DEV ONLY: 50 customers try to hold the SAME seat at the same moment.

Expected: exactly one 201 and 49 409s. Booking must be running.
Run from the booking folder:  python seed_test_seats.py   then   python race_test.py
"""
import json
import threading
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import config

CUSTOMERS = 50
start_line = threading.Barrier(CUSTOMERS)   # everyone waits here, then all go at once


def hold(n):
    data = json.dumps({"event_id": "test-event", "seat_id": "A1", "customer_id": f"customer-{n}"}).encode()
    req = urllib.request.Request(
        config.BOOKING_URL + "/holds", data=data, headers={"Content-Type": "application/json"}
    )
    start_line.wait()
    try:
        return urllib.request.urlopen(req).status
    except urllib.error.HTTPError as error:
        return error.code


with ThreadPoolExecutor(CUSTOMERS) as pool:
    results = Counter(pool.map(hold, range(CUSTOMERS)))
print(dict(results))
print("PASS" if results == {201: 1, 409: CUSTOMERS - 1} else "FAIL")
