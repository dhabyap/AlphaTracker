import requests, json

wallet = '0x47ac0fb4f2d84898e4d9e7b4dab3c24507a6d503'
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Referer': 'https://debank.com/',
    'Origin': 'https://debank.com',
    'Accept': 'application/json'
}

urls = [
    f'https://api.debank.com/user/addr?addr={wallet}',
    f'https://api.debank.com/token/cache_list?addr={wallet}&chain_id=bsc&is_all=true',
    f'https://api.debank.com/history/list?addr={wallet}&chain=bsc&page=1',
    f'https://api.debank.com/token/list?addr={wallet}&chain=bsc&has_balance=true',
    f'https://api.debank.com/portfolio/list?addr={wallet}',
]

for url in urls:
    try:
        r = requests.get(url, headers=headers, timeout=10)
        print(f'{r.status_code}: {url}')
        if r.status_code == 200 and r.text.strip():
            try:
                d = r.json()
            except:
                print(f'  non-JSON: {r.text[:100]}')
                continue
            if isinstance(d, dict) and 'error_code' in d:
                print(f'  error: {d}')
            elif isinstance(d, dict) and 'data' in d:
                data_val = d['data']
                print(f'  data type: {type(data_val).__name__}')
                if isinstance(data_val, list):
                    print(f'  items: {len(data_val)}')
                    if data_val:
                        print(f'  sample keys: {list(data_val[0].keys())[:8]}')
                elif isinstance(data_val, dict):
                    print(f'  keys: {list(data_val.keys())[:8]}')
            else:
                print(f'  response: {json.dumps(d, indent=2)[:200]}')
    except Exception as e:
        print(f'FAIL: {str(e)[:60]}')
