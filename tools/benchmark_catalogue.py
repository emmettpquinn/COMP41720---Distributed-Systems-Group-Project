#!/usr/bin/env python3
"""Run the Catalogue-only benchmark in a fresh private Compose stack."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

from _verification import Project, ROOT, external_output, source_info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--clients',type=int,default=500)
    parser.add_argument('--seconds',type=float,default=30)
    args = parser.parse_args()
    output = external_output(args.output)
    source = source_info()
    info = json.loads(subprocess.check_output(['docker','info','--format','{{json .}}'],text=True))
    # No daemon paths, usernames or other host identity are copied into the result.
    environment = {key:info.get(key) for key in ('NCPU','MemTotal','Architecture','ServerVersion','OperatingSystem')}
    extra = f'''  load:
    build:
      context: {json.dumps(str(ROOT))}
      dockerfile: tools/load.Dockerfile
    environment:
      LOAD_DATABASE_URL: postgresql://catalogue:catalogue@catalogue-db:5432/catalogue
      LOAD_AMQP_URL: amqp://guest:guest@rabbitmq:5672/
'''
    result = {'source':source,'docker':environment,'passed':False}
    try:
        with Project('load', output, messaging=True, extra=extra) as project:
            print('Building isolated benchmark project '+project.name,flush=True)
            project.cli('up','-d','--build','--wait','--wait-timeout','180','catalogue')
            project.cli('build','load')
            print(f'Running {args.clients} clients for {args.seconds}s',flush=True)
            raw = project.cli('run','--rm','--no-deps','-T','load','--clients',str(args.clients),'--seconds',str(args.seconds),timeout=600)
            payload = next(line for line in reversed(raw.splitlines()) if line.startswith('{"scope"'))
            result.update(json.loads(payload))
            result['project']=project.name
            # Capture actual application dependency versions as well as generator versions.
            result['application_packages']=json.loads(project.cli('exec','-T','catalogue','python','-c',
                "import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ['fastapi','uvicorn','asyncpg','aio-pika']}))"))
    except Exception as error:
        result['passed']=False
        result['failure']=type(error).__name__+': '+str(error)
        raise
    finally:
        result['recorded_at']=datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(result,indent=2)+'\n')
        print('Result: '+str(output),flush=True)
    print(json.dumps(result['observed'] | {'checks':result['checks']}),flush=True)
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
