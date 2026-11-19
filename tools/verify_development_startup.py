#!/usr/bin/env python3
"""Verify five-service startup, purchase and persistence in fresh private volumes."""

import argparse
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from urllib.parse import urlencode
import uuid

from _verification import Project, ROOT, external_output, http, source_info, wait_for


APPLICATIONS = ('catalogue', 'payment', 'booking', 'ticketing', 'discovery')
MONITORING_JOBS = set(APPLICATIONS) | {'rabbitmq', 'prometheus'}


def clean_archive(destination):
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    content = subprocess.check_output(['git', 'archive', '--format=tar', revision], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        for member in archive.getmembers():
            if not (destination / member.name).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Unsafe archive path')
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError('Unsupported special entry in source archive')
        archive.extractall(destination)
    return revision


def working_copy(destination):
    """Copy reviewed source without staging/committing, ignored files or Git metadata."""
    paths = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT).split(b'\0')
    for filename in set(paths) - {b''}:
        relative = Path(filename.decode())
        original = ROOT / relative
        target = destination / relative
        if not original.is_file():
            continue
        if original.is_symlink() or not original.resolve().is_relative_to(ROOT):
            raise ValueError('Working snapshot cannot contain external files or symlinks')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)


def verify_failure_cases(project, call, emit):
    """Only reconfigure this generated project; no existing stack is targeted."""
    def event(label):
        event_id = 'checkout-' + label + '-' + uuid.uuid4().hex[:12]
        call('catalogue', '/events', expected=201, body={
            'event_id': event_id, 'venue_id': 'venue-demo-001', 'title': 'Synthetic checkout ' + label,
            'starts_at': (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
            'timezone': 'Europe/Dublin', 'seats': [{'seat_id': 'A1', 'section': 'A', 'label': 'A1'}],
        })
        wait_for(lambda: call('catalogue', '/events/' + event_id)['availability'])
        return event_id

    def hold(event_id):
        customer = 'synthetic-' + uuid.uuid4().hex[:12]
        held = call('gateway', '/api/booking/holds', expected=201,
                    body={'event_id': event_id, 'seat_id': 'A1', 'customer_id': customer})
        return held['booking_id'], customer

    def status(identity):
        booking_id, customer = identity
        return call('gateway', '/api/booking/bookings/' + booking_id + '?' + urlencode({'customer_id': customer}))

    def checkout(identity):
        booking_id, customer = identity
        return call('gateway', '/api/booking/bookings/' + booking_id + '/checkout',
                    expected=202, body={'customer_id': customer})

    def no_valid_ticket(booking_id):
        ticket = call('ticketing', '/tickets/' + booking_id, expected=(200, 404))
        # Cancellation messages can create a void tombstone before any ticket
        # was issued. A stored invalid record is just as correct as no record.
        assert ticket.get('valid') is False or ticket.get('detail') == 'No ticket for this booking'

    def configure(mode='ok', delay='0', safety='60', hold_minutes='10'):
        project.env.update(PAYMENT_MODE=mode, PAYMENT_DELAY=delay,
                           BOOKING_SAFETY_NET_SECONDS=safety, HOLD_MINUTES=hold_minutes)
        project.cli('up', '-d', '--force-recreate', '--no-deps', '--wait', '--wait-timeout', '180', 'payment')
        project.cli('up', '-d', '--force-recreate', '--no-deps', '--wait', '--wait-timeout', '180', 'booking')

    configure(mode='decline')
    declined_event = event('decline')
    declined = hold(declined_event)
    checkout(declined)
    wait_for(lambda: status(declined)['booking_status'] == 'released')
    wait_for(lambda: call('catalogue', '/events/' + declined_event)['availability']['seats_remaining'] == 1)
    no_valid_ticket(declined[0])
    emit('declined_payment', {'booking_released': True, 'seat_available': True, 'no_ticket': True})

    configure()
    cancelled_event = event('cancel')
    cancelled = hold(cancelled_event)
    call('catalogue', '/events/' + cancelled_event, method='PATCH', body={'expected_version': 1, 'status': 'cancelled'})
    wait_for(lambda: status(cancelled)['booking_status'] == 'released')
    call('gateway', '/api/booking/bookings/' + cancelled[0] + '/checkout', expected=409, body={'customer_id': cancelled[1]})
    no_valid_ticket(cancelled[0])
    emit('event_cancelled_before_checkout', {'booking_released': True, 'checkout_409': True, 'no_ticket': True})

    configure(delay='10', safety='7')
    late_event = event('late')
    late = hold(late_event)
    checkout(late)
    wait_for(lambda: status(late)['booking_status'] == 'released', timeout=15)
    # A second customer can take the released seat. The late charge must not
    # touch it, even when the first booking moves through its refund states.
    next_customer = hold(late_event)
    wait_for(lambda: status(late)['booking_status'] == 'refunded', timeout=45)
    assert status(next_customer)['booking_status'] == 'held'
    no_valid_ticket(late[0])
    emit('late_success_refund', {'safety_net_seconds': 7, 'payment_delay_seconds': 10,
                               'original_refunded': True, 'replacement_hold_preserved': True, 'no_ticket': True})
    configure()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--working-tree', action='store_true', help='Verify a source snapshot including unstaged changes; creates no commit')
    parser.add_argument('--failure-cases', action='store_true', help='Also exercise declines, event cancellation and late-success refunds')
    args = parser.parse_args()
    output = external_output(args.output)
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'checks': {}, 'passed': False,
              'scope': 'five-service startup, synthetic purchase and persistence; HTTP model simulator, no real payments',
              'source': source_info(), 'clean_committed_archive': not args.working_tree}
    try:
        with tempfile.TemporaryDirectory(prefix='catalogue-startup-source-') as folder:
            source = Path(folder)
            if args.working_tree:
                working_copy(source)
                result['source_head'] = result['source']['head']
            else:
                result['source_head'] = clean_archive(source)
            with Project('startup', output, source=source, full=True, messaging=True) as project:
                result['project'] = project.name
                print('Building isolated source snapshot ' + project.name, flush=True)
                project.cli('up', '-d', '--build', '--wait', '--wait-timeout', '240', timeout=600)

                def call(service, path, **kwargs):
                    return http(project.origin(service), path, **kwargs)

                def emit(name, value):
                    result['checks'][name] = value
                    print(name + ': ' + json.dumps(value), flush=True)

                def assert_ready():
                    for service in APPLICATIONS:
                        assert call(service, '/ready')['status'] == 'ready'
                    assert call('gateway', '/health')['status'] == 'ok'
                    return True

                def assert_monitoring():
                    targets = http(project.origin('prometheus', 9090), '/api/v1/targets?state=active')['data']['activeTargets']
                    assert {item['labels']['job'] for item in targets} == MONITORING_JOBS
                    assert all(item['health'] == 'up' for item in targets)
                    items = http(project.origin('grafana', 3000), '/api/search', basic='admin:stagedoor-demo')
                    assert {'catalogue', 'stagedoor-payment', 'discovery', 'stagedoor-booking'} <= {item['uid'] for item in items}
                    return True

                wait_for(assert_ready)
                wait_for(assert_monitoring)
                jobs = [json.loads(line) for line in project.cli('ps', '-a', '--format', 'json').splitlines()]
                for job in ('catalogue-migrate', 'catalogue-seed', 'topology-init'):
                    match = next(item for item in jobs if item['Service'] == job)
                    assert match['State'] == 'exited' and match['ExitCode'] == 0, match
                emit('fresh_startup', {'applications_ready': 5, 'jobs_exit_zero': 3, 'scrape_targets_up': 7, 'dashboards_present': 4})
                assert call('gateway', '/api/capabilities') == {'holds': True, 'checkout': True, 'search': True}
                assert call('booking', '/ready')['capabilities'] == ['holds', 'checkout']
                events = call('gateway', '/api/catalogue/events')
                assert {event['event_id'] for event in events} == {'event-demo-001', 'event-demo-002'}
                detail_path = '/api/catalogue/events/event-demo-001'
                wait_for(lambda: call('gateway', detail_path)['availability'])
                assert call('gateway', detail_path)['availability']['seats_remaining'] == 2
                customer = 'synthetic-startup-buyer'
                hold_body = {'event_id': 'event-demo-001', 'seat_id': 'A1', 'customer_id': customer}
                held = call('gateway', '/api/booking/holds', body=hold_body, expected=201)
                repeat = call('gateway', '/api/booking/holds', body=hold_body)
                assert repeat['booking_ref'] == held['booking_id'] == held['booking_ref']
                refusal = call('gateway', '/api/booking/holds', body=hold_body | {'customer_id': 'synthetic-competitor'}, expected=409)
                assert refusal['error'] == 'seat_taken'
                wait_for(lambda: call('gateway', detail_path)['availability']['available_seat_ids'] == ['A2'])
                path = '/api/booking/bookings/' + held['booking_id']
                query = '?' + urlencode({'customer_id': customer})
                saved = call('gateway', path + query)
                assert saved['price_minor'] == 4500 and saved['currency'] == 'EUR'
                call('gateway', path + '?' + urlencode({'customer_id': 'wrong-customer'}), expected=404)
                call('gateway', path + '/checkout', body={'customer_id': 'wrong-customer'}, expected=404)
                accepted = call('gateway', path + '/checkout', body={'customer_id': customer}, expected=202)
                assert accepted['booking_status'] == 'payment_pending'
                wait_for(lambda: call('gateway', path + query)['booking_status'] == 'confirmed')
                wait_for(lambda: call('ticketing', '/tickets/' + held['booking_id'])['valid'])
                payment = call('payment', '/payments', body={'booking_id': held['booking_id'], 'amount': 4500, 'currency': 'EUR'})
                assert payment['payment_id'] == call('gateway', path + query)['payment_id']
                def email_arrived():
                    mail = http(project.origin('mailhog', 8025), '/api/v2/messages')
                    if isinstance(mail, str):
                        mail = json.loads(mail)
                    return any(held['booking_id'] in item['Content']['Body'] for item in mail['items'])
                wait_for(email_arrived)
                emit('browse_hold_checkout_ticket', {'same_id_hold_retry': True, 'competing_hold_409': True,
                     'availability_updates': True, 'owner_checks': True, 'saved_price': True,
                     'booking_confirmed': True, 'payment_idempotent': True, 'valid_ticket': True, 'email_captured': True})

                spare_body = hold_body | {'seat_id': 'A2'}
                spare = call('gateway', '/api/booking/holds', body=spare_body, expected=201)
                call('catalogue', '/events/event-demo-001', method='PATCH', body={'expected_version': 1, 'title': 'Synthetic restart verification'})
                before = project.sql('catalogue-db', 'SELECT count(*) FROM outbox')
                project.cli('run', '--rm', '--no-deps', '-T', 'catalogue-seed')
                assert project.sql('catalogue-db', 'SELECT count(*) FROM outbox') == before
                assert call('catalogue', '/events/event-demo-001')['title'] == 'Synthetic restart verification'
                assert project.cli('exec', '-T', 'booking', 'python', '-c', 'import config; print(config.BOOKING_DEMO_SEED)') == 'False'
                emit('repeat_safe_seeds', {'edited_metadata_preserved': True, 'no_extra_outbox_rows': True, 'booking_demo_seed_off': True})

                def assert_persisted():
                    assert call('catalogue', '/events/event-demo-001')['title'] == 'Synthetic restart verification'
                    assert call('gateway', path + query)['booking_status'] == 'confirmed'
                    assert call('gateway', '/api/booking/holds', body=spare_body)['booking_id'] == spare['booking_id']
                    assert call('payment', '/payments', body={'booking_id': held['booking_id'], 'amount': 4500, 'currency': 'EUR'})['payment_id'] == payment['payment_id']
                    assert 'catalogue_messaging_enabled 1.0' in call('catalogue', '/metrics')
                    assert call('ticketing', '/tickets/' + held['booking_id'])['valid']
                    return True

                project.cli('up', '-d', '--force-recreate', '--no-deps', '--wait', '--wait-timeout', '180', *APPLICATIONS, 'gateway')
                wait_for(assert_ready)
                wait_for(assert_persisted)
                wait_for(assert_monitoring)
                emit('application_recreation', {'confirmed_booking': True, 'held_booking': True, 'payment': True, 'ticket': True, 'catalogue_messaging_on': True})
                databases = tuple(service + '-db' for service in APPLICATIONS)
                project.cli('stop', *databases)
                for service in APPLICATIONS:
                    assert call(service, '/health')['status'] == 'ok'
                    call(service, '/ready', expected=503)
                project.cli('start', *databases)
                wait_for(assert_ready)
                wait_for(assert_persisted)
                wait_for(assert_monitoring)
                emit('database_restarts', {'liveness_preserved': 5, 'readiness_503': 5, 'owned_state_recovered': True})
                if args.failure_cases:
                    verify_failure_cases(project, call, emit)
                result['passed'] = True
    except Exception as error:
        result['failure'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        result['finished_at'] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(result, indent=2) + '\n')
        print('Result: ' + str(output), flush=True)


if __name__ == '__main__':
    main()
