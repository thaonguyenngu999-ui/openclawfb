import requests, json
r = requests.post('http://127.0.0.1:8899/list_profiles', json={'folder_id': 'fb1', 'page_size': 10})
d = r.json()
print(json.dumps(d, indent=2, ensure_ascii=False)[:2000])
