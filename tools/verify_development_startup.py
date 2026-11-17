#!/usr/bin/env python3
"""Check committed development startup and persistence using fresh private volumes."""

import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import uuid
import urllib.request

from _verification import Project, ROOT, external_output, http, wait_for


def clean_archive(destination):
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    content = subprocess.check_output(['git','archive','--format=tar',revision],cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(content)) as archive:
        for member in archive.getmembers():
            if not (destination / member.name).resolve().is_relative_to(destination.resolve()):
                raise ValueError('Unsafe archive path')
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError('Unsupported special entry in source archive')
        archive.extractall(destination)
    return revision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    output = external_output(args.output)
    result = {'started_at':datetime.now(timezone.utc).isoformat(), 'checks':{}, 'passed':False,
              'scope':'committed development stack: four real business implementations plus Discovery shell; no full purchase'}
    try:
        with tempfile.TemporaryDirectory(prefix='catalogue-clean-source-') as folder:
            source = Path(folder)
            result['source_head']=clean_archive(source)
            result['clean_committed_archive']=True
            real_discovery=(source/'discovery/Dockerfile').exists()
            applications=('catalogue','payment','booking','ticketing') + (('discovery',) if real_discovery else ())
            target_count=6 if real_discovery else 5
            dashboard_count=3 if real_discovery else 2
            result['scope']='committed development stack: five business implementations and explicit model simulator; no full purchase' if real_discovery else result['scope']
            with Project('startup',output,source=source,full=True,messaging=False) as project:
                result['project']=project.name
                print('Building committed-source development stack '+project.name,flush=True)
                project.cli('up','-d','--build','--wait','--wait-timeout','180',timeout=600)

                def call(service,path,**kwargs):
                    return http(project.origin(service),path,**kwargs)

                def assert_ready():
                    for service in applications:
                        assert call(service,'/ready')['status']=='ready'
                    assert call('gateway','/health')['status']=='ok'
                    if not real_discovery:
                        assert call('discovery','/health')['mode']=='bootstrap'
                    return True

                def emit(name,value):
                    result['checks'][name]=value
                    print(name+': '+json.dumps(value),flush=True)

                def assert_monitoring():
                    data=http(project.origin('prometheus',9090),'/api/v1/targets?state=active')['data']['activeTargets']
                    expected={'catalogue','payment','ticketing','rabbitmq','prometheus'} | ({'discovery'} if real_discovery else set())
                    assert {target['labels']['job'] for target in data}==expected
                    assert all(target['health']=='up' for target in data)
                    items=http(project.origin('grafana',3000),'/api/search',basic='admin:stagedoor-demo')
                    expected_dashboards={'catalogue','stagedoor-payment'} | ({'discovery'} if real_discovery else set())
                    assert expected_dashboards <= {item['uid'] for item in items}
                    return True

                wait_for(assert_ready)
                wait_for(assert_monitoring)
                ps=project.cli('ps','-a','--format','json')
                jobs=[json.loads(line) for line in ps.splitlines()]
                for job in ('catalogue-migrate','catalogue-seed','topology-init'):
                    match=next(item for item in jobs if item['Service']==job)
                    assert match['State']=='exited' and match['ExitCode']==0,match
                emit('fresh_startup',{'applications_ready':len(applications),'discovery_mode':'real' if real_discovery else 'bootstrap','jobs_exit_zero':3,'scrape_targets_up':target_count,'dashboards_present':dashboard_count})

                events=call('gateway','/api/catalogue/events')
                assert {event['event_id'] for event in events}=={'event-demo-001','event-demo-002'}
                detail=call('gateway','/api/catalogue/events/event-demo-001')
                assert detail['availability'] is None
                capabilities=call('gateway','/api/capabilities')
                assert capabilities=={'holds':True,'checkout':False,'search':real_discovery}
                hold_body={'event_id':'event-demo-001','seat_id':'A1','customer_id':'synthetic-startup-buyer'}
                hold=call('gateway','/api/booking/holds',body=hold_body,expected=201)
                assert hold['status']=='held'
                repeat=call('gateway','/api/booking/holds',body=hold_body)
                assert repeat['booking_ref']==hold['booking_ref']
                refusal=call('gateway','/api/booking/holds',body=hold_body|{'customer_id':'synthetic-competing-buyer'},expected=409)
                assert refusal['error']=='seat_taken'
                emit('browse_hold_boundary',{'seeded_events':2,'unknown_availability_preserved':True,'stable_hold_retry':True,'competing_hold_409':True,'checkout_unavailable':True,'search_enabled':real_discovery})

                # Use Catalogue's public update API, then rerun owned seed code.
                origin=project.origin('catalogue')
                request=urllib.request.Request(origin+'/events/event-demo-001',method='PATCH',
                    data=json.dumps({'expected_version':1,'title':'Synthetic restart verification'}).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=10) as response:
                    assert response.status==200
                payment_body={'booking_id':'startup-'+uuid.uuid4().hex[:12],'amount':4500,'currency':'EUR'}
                payment=call('payment','/payments',body=payment_body)
                assert payment['status']=='paid'
                project.cli('run','--rm','--no-deps','-T','catalogue-seed')
                project.cli('exec','-T','booking','python','-c','from demo_seed import seed; seed()')
                assert call('catalogue','/events/event-demo-001')['title']=='Synthetic restart verification'
                assert call('booking','/holds',body=hold_body)['booking_ref']==hold['booking_ref']
                assert project.sql('catalogue-db','SELECT count(*) FROM outbox')=='3'
                assert project.sql('catalogue-db','SELECT count(*) FROM event_seats')=='4'
                emit('repeat_safe_seeds',{'edited_metadata_preserved':True,'held_inventory_preserved':True,'no_extra_outbox_rows':True})

                services=applications+('gateway',)
                project.cli('up','-d','--force-recreate','--no-deps','--wait','--wait-timeout','180',*services)
                wait_for(assert_ready)

                def assert_persisted():
                    assert call('catalogue','/events/event-demo-001')['title']=='Synthetic restart verification'
                    assert call('gateway','/api/booking/holds',body=hold_body)['booking_ref']==hold['booking_ref']
                    assert call('payment','/payments',body=payment_body)['payment_id']==payment['payment_id']
                    assert 'catalogue_messaging_enabled 0.0' in call('catalogue','/metrics')
                    return True

                assert_persisted()
                wait_for(assert_monitoring)
                emit('application_recreation',{'catalogue_metadata':True,'booking_reference':True,'payment_idempotency':True,'default_catalogue_messaging_off':True,'monitoring_recovered':True})

                # Restart owned database containers independently of the application processes.
                databases=tuple(service+'-db' for service in applications)
                project.cli('stop',*databases)
                for service in applications:
                    assert call(service,'/health')['status']=='ok'
                    call(service,'/ready',expected=503)
                project.cli('start',*databases)
                wait_for(assert_ready)
                assert_persisted()
                wait_for(assert_monitoring)
                emit('database_restarts',{'liveness_preserved':len(applications),'readiness_503':len(applications),'owned_state_recovered':True,'monitoring_recovered':True})
                result['passed']=True
    except Exception as error:
        result['passed']=False
        result['failure']=type(error).__name__+': '+str(error)
        raise
    finally:
        result['finished_at']=datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(result,indent=2)+'\n')
        print('Result: '+str(output),flush=True)


if __name__=='__main__':
    main()
