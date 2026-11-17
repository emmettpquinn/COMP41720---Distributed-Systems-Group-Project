from prometheus_client import Counter, Gauge, Histogram

SEARCHES = Counter('discovery_searches', 'Searches by mode', ['mode'])
HTTP = Counter('discovery_http_requests', 'HTTP requests', ['route', 'status'])
LATENCY = Histogram('discovery_http_seconds', 'HTTP latency', ['route'])
TOKENS = Counter('discovery_embedding_tokens', 'Ollama reported input tokens', ['purpose'])
COST = Counter('discovery_notional_cost', 'Configured notional cost; not a real charge', ['purpose'])
BREAKER = Gauge('discovery_breaker_open', '1 when Ollama breaker is open')
INFLIGHT = Gauge('discovery_ollama_inflight', 'Concurrent Ollama calls')
LAG = Histogram('discovery_indexing_lag_seconds', 'Event timestamp to embedding stored', buckets=(1,2,5,10,30,60,300))
PENDING = Gauge('discovery_pending_embeddings', 'Entries still waiting for an embedding')
MESSAGES = Counter('discovery_messages', 'Consumer outcomes', ['outcome'])
ACTIVE = Gauge('discovery_active_index', 'Active index version', ['version'])
