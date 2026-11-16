"""Run an explicitly simulated learning demo without Docker or a model download."""
import os
from pathlib import Path
import sys

# Shared messaging belongs to Emmett; use it without copying his code.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'shared'))


if __name__ == '__main__':
    os.environ['EMBEDDING_PROVIDER'] = 'simulated'
    os.environ['AMQP_URL'] = ''
    os.environ['SEED_DEMO'] = '1'
    os.environ.setdefault('DISCOVERY_ADMIN_TOKEN', 'local-learning-only')
    import uvicorn
    print('SIMULATED learning demo. No real AI, RabbitMQ or PostgreSQL in this mode.')
    print('Open http://127.0.0.1:8003/docs — try GET /search with calm music on Friday night')
    uvicorn.run('discovery.app.main:app', host='127.0.0.1', port=8003)
