"""Read-only connection check. Does not persist results or send mail."""
import json
from app.isir import query, get_hits

if __name__ == '__main__':
    payload = query({'ic':'00000000'})
    print(json.dumps({'status':payload.get('stav'), 'raw_hits':len(get_hits(payload))},default=str,ensure_ascii=False))
