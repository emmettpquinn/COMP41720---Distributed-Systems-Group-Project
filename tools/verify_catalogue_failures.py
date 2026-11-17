#!/usr/bin/env python3
"""Create a private synthetic stack, inject failures, save JSON, then remove it.

Requires Docker Compose with !override support and Python 3.9+. Never targets an
existing project. No other service's database or running stack is modified.
"""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]


def wait_for(probe, timeout=90):
    end = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < end:
        try:
            result = probe()
            if result:
                return result
        except (urllib.error.URLError, TimeoutError, ConnectionError, AssertionError, subprocess.CalledProcessError) as error:
            last_error = type(error).__name__
        time.sleep(0.3)
    raise AssertionError(f"Probe did not succeed within {timeout}s ({last_error})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='JSON result outside the repository')
    parser.add_argument('--outage-seconds', type=float, default=60)
    args = parser.parse_args()
    if not 60 <= args.outage_seconds <= 300:
        parser.error('Outage must be between 60 and 300 seconds')
    if args.output.resolve().is_relative_to(ROOT):
        parser.error('Keep generated results outside the repository')
    project = 'catalogue-fault-' + uuid.uuid4().hex[:10]
    result = {'project':project, 'started_at':datetime.now(timezone.utc).isoformat(), 'checks':{}, 'scope':'isolated Catalogue and synthetic queue probes; no real Booking producer'}
    with tempfile.TemporaryDirectory(prefix='catalogue-fault-') as folder:
        override=Path(folder)/'override.yml'
        override.write_text('''services:
  rabbitmq:
    ports: !override ["127.0.0.1::15672"]
  catalogue:
    ports: !override ["127.0.0.1::8000"]
''')
        command=['docker','compose','-p',project,'-f',str(ROOT/'docker-compose.yml'),'-f',str(override)]
        env=os.environ | {'CATALOGUE_MESSAGING_ENABLED':'true'}

        def compose(*arguments, capture=False):
            completed=subprocess.run(command+list(arguments),cwd=ROOT,env=env,check=True,
                                     stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                     stderr=subprocess.PIPE if capture else subprocess.DEVNULL,text=True,timeout=240)
            return completed.stdout.strip() if capture else None

        def sql(query):
            return compose('exec','-T','catalogue-db','psql','-U','catalogue','-d','catalogue','-v','ON_ERROR_STOP=1','-Atc',query,capture=True)

        def request(origin,path,body=None,method=None,management=False,expected=200):
            headers={}
            if management:
                headers['Authorization']='Basic '+base64.b64encode(b'guest:guest').decode()
            if body is not None:
                headers['Content-Type']='application/json'
            req=urllib.request.Request(origin+path,data=json.dumps(body).encode() if body is not None else None,headers=headers,method=method)
            try:
                response=urllib.request.urlopen(req,timeout=8)
            except urllib.error.HTTPError as error:
                response=error
            with response:
                raw=response.read().decode()
                assert response.code==expected,(path,response.code,raw)
                return json.loads(raw) if 'application/json' in response.headers.get('Content-Type','') else raw

        def emit(name,detail):
            result['checks'][name]=detail
            print(name+': '+json.dumps(detail),flush=True)

        try:
            print('Building fresh isolated project '+project,flush=True)
            compose('up','-d','--build','--wait','--wait-timeout','180','catalogue')
            api='http://'+compose('port','catalogue','8000',capture=True)
            broker='http://'+compose('port','rabbitmq','15672',capture=True)
            http=lambda path,**kwargs: request(api,path,**kwargs)
            mgmt=lambda path,**kwargs: request(broker,path,management=True,**kwargs)
            pending=lambda: int(sql('SELECT count(*) FROM outbox WHERE published_at IS NULL'))
            wait_for(lambda: pending()==0)
            queues=['booking.event-changes','discovery.event-changes']

            def take(queue):
                rows=mgmt('/api/queues/%2F/'+queue+'/get',body={'count':1000,'ackmode':'ack_requeue_false','encoding':'auto','truncate':1000000})
                return [json.loads(row['payload']) for row in rows]

            for queue in queues: take(queue)
            def create(label):
                event_id='fault-'+label+'-'+uuid.uuid4().hex[:10]
                http('/events',body={'event_id':event_id,'venue_id':'venue-demo-001','title':'Synthetic fault probe','starts_at':'2026-12-01T19:00:00Z','timezone':'Europe/Dublin','seats':[{'seat_id':'A1','section':'A','label':'A1'}]},expected=201)
                message_id=sql("SELECT message_id FROM outbox WHERE event_id = '"+event_id+"'")
                return event_id,message_id

            print('Testing process kill before publication',flush=True)
            # Kill the process with an acknowledged database write still unpublished.
            compose('stop','rabbitmq')
            before_id,before_message=create('before')
            assert pending()==1
            compose('kill','-s','SIGKILL','catalogue')
            compose('start','catalogue')
            api='http://'+compose('port','catalogue','8000',capture=True)
            wait_for(lambda: http('/ready')['status']=='ready')
            assert http('/events/'+before_id)['event_version']==1
            compose('start','rabbitmq')
            broker='http://'+compose('port','rabbitmq','15672',capture=True)
            wait_for(lambda: pending()==0)
            for queue in queues:
                assert [item['message_id'] for item in take(queue)]==[before_message]
            emit('process_kill_before_publish',{'durable_event':True,'stable_id_delivered_to_both_queues':True})

            print('Testing process kill after publisher confirmation',flush=True)
            # A database-only latch creates the real post-confirm/pre-commit window.
            sql("CREATE FUNCTION fault_marker_pause() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(60); RETURN NEW; END $$; CREATE TRIGGER fault_marker_pause BEFORE UPDATE OF published_at ON outbox FOR EACH ROW EXECUTE FUNCTION fault_marker_pause();")
            after_id,after_message=create('after')
            wait_for(lambda: sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event='PgSleep' AND query LIKE 'UPDATE outbox%'")=='1')
            first={queue:[item['message_id'] for item in take(queue)] for queue in queues}
            assert all(ids==[after_message] for ids in first.values()),first
            compose('kill','-s','SIGKILL','catalogue')
            sql('DROP TRIGGER fault_marker_pause ON outbox; DROP FUNCTION fault_marker_pause();')
            assert pending()==1
            compose('start','catalogue')
            api='http://'+compose('port','catalogue','8000',capture=True)
            wait_for(lambda: pending()==0)
            second={queue:[item['message_id'] for item in take(queue)] for queue in queues}
            assert first==second,(first,second)
            emit('process_kill_after_confirm',{'redelivered_same_id_to_both_queues':True,'sent_marker_committed_after_restart':True})

            print('Testing measured 60-second broker outage',flush=True)
            # No other project's broker is touched. Keep browsing throughout 60 seconds.
            compose('stop','rabbitmq')
            started=time.monotonic()
            outage_id,outage_message=create('outage')
            probes=0
            while time.monotonic()-started < args.outage_seconds:
                assert http('/events/'+outage_id)['event_version']==1
                assert pending()==1
                probes+=1
                time.sleep(max(0,min(2,args.outage_seconds-(time.monotonic()-started))))
            elapsed=time.monotonic()-started
            compose('start','rabbitmq')
            broker='http://'+compose('port','rabbitmq','15672',capture=True)
            wait_for(lambda: pending()==0)
            for queue in queues:
                assert [item['message_id'] for item in take(queue)]==[outage_message]
            emit('broker_outage',{'measured_seconds':round(elapsed,3),'successful_browse_probes':probes,'pending_write_recovered':True})

            # Replay full snapshots, including one duplicate and an older sequence.
            def snapshot(sequence,seats):
                return {'message_id':str(uuid.uuid4()),'type':'seat.availability-changed','occurred_at':datetime.now(timezone.utc).isoformat(),'version':1,'producer':'booking','payload':{'event_id':'event-demo-001','sequence':sequence,'seats_remaining':len(seats),'available_seat_ids':seats}}
            newest=snapshot(101,['A1','A2'])
            for body in [snapshot(100,[]),newest,newest,snapshot(99,['A1'])]:
                assert mgmt('/api/exchanges/%2F/events/publish',body={'properties':{'content_type':'application/json','delivery_mode':2},'routing_key':body['type'],'payload':json.dumps(body),'payload_encoding':'string'})['routed']
            wait_for(lambda: sql("SELECT sequence FROM availability WHERE event_id='event-demo-001'")=='101')
            wait_for(lambda: int(sql('SELECT count(*) FROM processed_messages'))==3)
            time.sleep(0.6)  # Cross the bounded browse cache TTL before observing via HTTP.
            assert http('/events/event-demo-001')['availability']['seats_remaining']==2
            emit('duplicate_reordering_recovery',{'highest_sequence':101,'processed_unique_ids':3,'available_after_release':2})

            compose('stop','catalogue-db')
            assert http('/health')['status']=='ok'
            http('/ready',expected=503)
            assert 'catalogue_database_up 0.0' in http('/metrics')
            compose('start','catalogue-db')
            wait_for(lambda: http('/ready')['status']=='ready')
            assert http('/events/'+outage_id)['event_id']==outage_id
            assert sql("SELECT sequence FROM availability WHERE event_id='event-demo-001'")=='101'
            emit('database_outage',{'liveness_preserved':True,'readiness_503':True,'metrics_preserved':True,'state_recovered':True})
            result['passed']=True
        finally:
            # This generated project and its volumes contain only this run's probes.
            compose('down','--volumes','--remove-orphans')
            result['finished_at']=datetime.now(timezone.utc).isoformat()
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(result,indent=2)+'\n')
            print('Result: '+str(args.output),flush=True)


if __name__=='__main__':
    main()
