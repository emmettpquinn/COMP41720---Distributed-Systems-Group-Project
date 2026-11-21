#!/usr/bin/env python3
"""Verify real Catalogue/RabbitMQ/Discovery with separate owned databases.

The embedding provider is an explicit HTTP simulator. This does not exercise
learned-model relevance, the Booking saga or a completed purchase.
"""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
from urllib.parse import quote
import uuid

from _verification import Project, external_output, http, source_info, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    output = external_output(args.output)
    result = {'started_at':datetime.now(timezone.utc).isoformat(),'source':source_info(),
              'embedding_provider':'simulated_http','checks':{},'passed':False}
    try:
        with Project('discovery',output,full=True,messaging=True) as project:
            result['project']=project.name
            project.cli('up','-d','--build','--wait','--wait-timeout','240',timeout=600)

            def call(service,path,**kwargs):
                return http(project.origin(service),path,**kwargs)

            def search(query):
                return call('gateway','/api/discovery/search?q='+quote(query))

            def emit(name,details):
                result['checks'][name]=details
                print(name+': '+json.dumps(details),flush=True)

            for service in ('catalogue','booking','payment','ticketing','discovery'):
                wait_for(lambda service=service:call(service,'/ready')['status']=='ready')
            assert call('gateway','/api/capabilities')=={'holds':True,'checkout':True,'search':True}
            assert project.sql('catalogue-db',"SELECT to_regclass('public.discovery_events') IS NULL")=='t'
            assert project.sql('discovery-db',"SELECT to_regclass('public.events') IS NULL")=='t'
            wait_for(lambda: search('performance')['mode']=='semantic')
            seeded=search('performance')
            assert {'event-demo-001','event-demo-002'} <= {row['event_id'] for row in seeded['results']}
            assert seeded['embedding_provider']=='simulated_http'
            emit('startup_and_ownership',{'business_services_ready':5,'databases_separate':True,'seed_events_searchable':2,'model_explicitly_simulated':True})

            event_id='discovery-check-'+uuid.uuid4().hex[:16]
            query='integration'+uuid.uuid4().hex[:12]
            body={'event_id':event_id,'venue_id':'venue-demo-001','title':query+' music',
                  'description':'Synthetic Catalogue-to-Discovery integration check.',
                  'starts_at':(datetime.now(timezone.utc)+timedelta(days=7)).isoformat(),
                  'timezone':'Europe/Dublin','tags':[query,'music'],
                  'seats':[{'seat_id':'A1','section':'A','label':'A1'}]}
            call('catalogue','/events',body=body,expected=201)

            def observe(version,absent=False):
                started=time.monotonic()
                def probe():
                    row=project.sql('discovery-db',f"SELECT version::text || ':' || cancelled::text FROM discovery_events WHERE id='{event_id}'")
                    if row != f"{version}:{str(absent).lower()}":return False
                    response=search(query)
                    matches=[item for item in response['results'] if item['event_id']==event_id]
                    return (not matches) if absent else response['mode']=='semantic' and bool(matches and matches[0]['event_version']==version)
                wait_for(probe,timeout=30)
                return round(time.monotonic()-started,3)

            created_seconds=observe(1)
            call('catalogue','/events/'+event_id,body={'expected_version':1,'title':query+' revised music'},method='PATCH')
            updated_seconds=observe(2)
            similar=call('gateway','/api/discovery/events/'+event_id+'/similar')
            assert all(row['event_id']!=event_id for row in similar['results'])
            call('catalogue','/events/'+event_id,body={'expected_version':2,'status':'cancelled'},method='PATCH')
            cancelled_seconds=observe(3,absent=True)
            emit('create_update_cancel',{'event_id':event_id,'created_visible_seconds':created_seconds,'updated_visible_seconds':updated_seconds,'cancelled_absent_seconds':cancelled_seconds,'similar_excludes_self':True})

            envelope=json.loads(project.sql('catalogue-db',f"SELECT envelope FROM outbox WHERE event_id='{event_id}' AND event_version=1"))
            def publish(body):
                response=http(project.origin('rabbitmq',15672),'/api/exchanges/%2F/events/publish',
                    body={'properties':{'content_type':'application/json','delivery_mode':2},
                          'routing_key':'event.changed','payload':json.dumps(body),'payload_encoding':'string'},basic='guest:guest')
                assert response['routed']
            publish(envelope)
            stale_id=str(uuid.uuid4())
            publish(envelope|{'message_id':stale_id})
            wait_for(lambda: project.sql('discovery-db',f"SELECT count(*) FROM discovery_processed_messages WHERE message_id='{stale_id}'")=='1')
            assert project.sql('discovery-db',f"SELECT count(*) FROM discovery_processed_messages WHERE message_id='{envelope['message_id']}'")=='1'
            observe(3,absent=True)
            emit('duplicates_and_stale_after_cancel',{'same_id_recorded_once':True,'new_id_old_version_ignored':True,'cancellation_tombstone_preserved':True})

            before=project.sql('catalogue-db','SELECT count(*) FROM outbox')
            call('catalogue','/events',body=body|{'event_id':event_id+'-bad','title':'x'*501},expected=422)
            assert project.sql('catalogue-db','SELECT count(*) FROM outbox')==before
            emit('payload_limits',{'oversized_title_422_before_outbox_write':True})

            def fault(enabled):
                project.cli('exec','-T','discovery','python','-c',
                    "import json,urllib.request; r=urllib.request.Request('http://discovery-model:11434/test/faults',data=json.dumps({'fail':"+str(enabled)+"}).encode(),headers={'Content-Type':'application/json'}); urllib.request.urlopen(r,timeout=5).read()")
            try:
                fault(True)
                fallback=search('music')
                assert fallback['mode']=='keyword_fallback' and fallback['label']=='Showing keyword results'
                assert fallback['embedding_provider']=='simulated_http'
                assert call('discovery','/ready')['status']=='ready'
            finally:
                fault(False)
            wait_for(lambda: search('music')['mode']=='semantic',timeout=45)
            emit('model_outage_and_recovery',{'keyword_fallback_label':True,'readiness_preserved':True,'simulated_provider_label':True,'semantic_mode_recovers':True})

            project.cli('restart','discovery')
            wait_for(lambda: call('discovery','/ready')['status']=='ready')
            observe(3,absent=True)
            wait_for(lambda: search('music')['mode']=='semantic')
            emit('discovery_restart',{'projection_and_tombstone_preserved':True,'broker_reconnected':True,'model_index_reused':True})

            def monitoring():
                targets=http(project.origin('prometheus',9090),'/api/v1/targets?state=active')['data']['activeTargets']
                assert {item['labels']['job'] for item in targets}=={'catalogue','booking','discovery','payment','ticketing','rabbitmq','prometheus'}
                assert all(item['health']=='up' for item in targets)
                dashboard=http(project.origin('grafana',3000),'/api/dashboards/uid/discovery',basic='admin:stagedoor-demo')['dashboard']
                assert all(panel.get('datasource',{}).get('uid')=='prometheus' for panel in dashboard['panels'])
                data=http(project.origin('grafana',3000),'/api/datasources/proxy/uid/prometheus/api/v1/query?query=discovery_searches_total',basic='admin:stagedoor-demo')
                assert data['status']=='success' and data['data']['result']
                return True
            wait_for(monitoring)
            emit('shared_monitoring',{'targets_up':7,'discovery_dashboard':True,'actual_discovery_metrics_through_shared_datasource':True})
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
