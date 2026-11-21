#!/usr/bin/env python3
"""Generate a fictional row/column seat map; publish it only with --publish."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
from zoneinfo import ZoneInfo


class APIError(RuntimeError):
    def __init__(self, status, detail):
        self.status = status
        super().__init__(f'Catalogue returned {status}: {detail}')


def row_label(index):
    label = ''
    while index >= 0:
        index, remainder = divmod(index, 26)
        label = chr(65 + remainder) + label
        index -= 1
    return label


def identifier(value):
    value = value.strip()
    if not value or len(value) > 64:
        raise ValueError('IDs must contain 1 to 64 characters')
    return value


def create_payload(rows=5, columns=10, *, event_id=None, venue_id=None,
                   title='Demo Theatre Performance', venue_name='Demo Theatre',
                   city='Dublin', section='Stalls', starts_at=None,
                   timezone_name='Europe/Dublin'):
    if not 1 <= rows <= 52 or not 1 <= columns <= 40 or rows * columns > 500:
        raise ValueError('Use 1-52 rows, 1-40 columns and at most 500 seats for this demo tool')
    zone = ZoneInfo(timezone_name)
    if starts_at is None:
        start = (datetime.now(zone) + timedelta(days=7)).replace(hour=19, minute=30, second=0, microsecond=0)
    else:
        start = datetime.fromisoformat(starts_at.replace('Z', '+00:00'))
    if start.utcoffset() is None or start <= datetime.now(timezone.utc):
        raise ValueError('starts-at must be a future timestamp with an explicit UTC offset')
    title, venue_name, section = title.strip(), venue_name.strip(), section.strip()
    if not title or len(title) > 500 or not venue_name or not section:
        raise ValueError('Supply a title (at most 500 characters), venue name and section')
    suffix = uuid.uuid4().hex[:12]
    event_id = identifier(event_id or 'event-seating-' + suffix)
    venue_id = identifier(venue_id or 'venue-seating-' + suffix)
    seats = []
    for row in range(rows):
        for column in range(1, columns + 1):
            label = f'{row_label(row)}{column}'
            seats.append({'seat_id': label, 'section': section, 'label': label})
    return {
        'venue': {'venue_id': venue_id, 'name': venue_name, 'city': city},
        'event': {'event_id': event_id, 'venue_id': venue_id, 'title': title,
                  'description': 'A fictional performance with labelled rows for the local demo.',
                  'starts_at': start.isoformat(), 'timezone': timezone_name,
                  'tags': ['music', 'demo'], 'seats': seats},
    }


def request_json(origin, path, body=None):
    request = Request(origin + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'Accept': 'application/json', 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read(1000).decode(errors='replace')
        raise APIError(error.code, detail) from error


def publish_demo(origin, payload, request=request_json):
    parts = urlsplit(origin)
    if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.path not in {'', '/'} or parts.query or parts.fragment:
        raise ValueError('catalogue-url must be an HTTP origin without credentials, query or path')
    origin = origin.rstrip('/')
    event_id = payload['event']['event_id']
    try:
        request(origin, '/events/' + quote(event_id, safe=''))
    except APIError as error:
        if error.status != 404:
            raise
    else:
        raise ValueError('That event ID already exists. Choose a new --event-id; existing seats/bookings will not be changed')
    try:
        venue = request(origin, '/venues', payload['venue'])
    except APIError as error:
        if error.status != 409:
            raise
        # An explicitly reused venue is read, never modified.
        venue = request(origin, '/venues/' + quote(payload['venue']['venue_id'], safe=''))
    event = request(origin, '/events', payload['event'])
    return {'venue_id': venue['venue_id'], 'event_id': event['event_id'],
            'title': event['title'], 'seats': len(payload['event']['seats'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=5)
    parser.add_argument('--columns', type=int, default=10)
    parser.add_argument('--title', default='Demo Theatre Performance')
    parser.add_argument('--venue-name', default='Demo Theatre')
    parser.add_argument('--city', default='Dublin')
    parser.add_argument('--section', default='Stalls')
    parser.add_argument('--event-id')
    parser.add_argument('--venue-id')
    parser.add_argument('--starts-at', help='Future ISO timestamp with offset; default: a week from today at 19:30')
    parser.add_argument('--timezone', default='Europe/Dublin')
    parser.add_argument('--catalogue-url', default='http://localhost:8001')
    parser.add_argument('--publish', action='store_true', help='Create the venue/event through Catalogue; default only prints JSON')
    args = parser.parse_args()
    try:
        payload = create_payload(args.rows, args.columns, event_id=args.event_id,
                                 venue_id=args.venue_id, title=args.title, venue_name=args.venue_name,
                                 city=args.city, section=args.section, starts_at=args.starts_at,
                                 timezone_name=args.timezone)
        result = publish_demo(args.catalogue_url, payload) if args.publish else payload
        print(json.dumps(result, indent=2))
        if args.publish:
            print('Created. Open the website, choose Browse all events, then Refresh. Allow Booking and Discovery to catch up.')
    except (ValueError, OSError, RuntimeError, KeyError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
