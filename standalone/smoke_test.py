"""Run after build, init and web startup; NEVER point this at production."""
import os
import requests
base='http://127.0.0.1:8186/spidermap.php'
viewer=('observer',os.environ['SM_TEST_VIEWER_PASSWORD'])
admin=('nikos',os.environ['SM_TEST_ADMIN_PASSWORD'])
assert requests.get(base,timeout=10).status_code==401
assert requests.get(base,headers={'X-Auth-User':'nikos'},timeout=10).status_code==401
r=requests.get(base+'?data=me',auth=viewer,timeout=10)
assert r.status_code==200 and not any(r.json()['can'].values()),r.text
r=requests.get(base+'?data=me',auth=admin,timeout=10)
assert r.status_code==200 and all(r.json()['can'].values()),r.text
headers={'X-SpiderMap':'1','Origin':'http://localhost:8186'}
assert requests.post(base,auth=viewer,headers=headers,json={'action':'scan'},timeout=10).status_code==403
assert requests.post(base,auth=admin,json={'action':'scan'},timeout=10).status_code==403
assert requests.post(base,auth=admin,headers={**headers,'Origin':'https://evil.invalid'},json={'action':'scan'},timeout=10).status_code==403
assert requests.post(base,auth=admin,headers=headers,json={'action':'scan'},timeout=10).status_code==200
assert requests.get(base+'?data=../../etc/passwd',auth=admin,timeout=10).status_code==400
assert requests.get('http://127.0.0.1:8186/spidermap.json',auth=admin,timeout=10).status_code==403
print('Container HTTP authentication/roles/CSRF/path checks passed')
