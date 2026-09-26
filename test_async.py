import urllib.request
import json
import time

step_content = b"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('FreeCAD Model'),'2;1');
FILE_NAME('cube.step','2026-09-07T00:00:00',('Author'),(''),'Open CASCADE STEP processor 7.5','FreeCAD','');
FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));
ENDSEC;
DATA;
ENDSEC;
END-ISO-10303-21;
"""

boundary = '----WebKitFormBoundary7MA4YWxkTrZu0gW'
body = (
    f'--{boundary}\r\n'
    'Content-Disposition: form-data; name="file"; filename="test.step"\r\n'
    'Content-Type: application/step\r\n\r\n'
).encode() + step_content + f'\r\n--{boundary}--\r\n'.encode()

req = urllib.request.Request(
    'http://localhost:8080/convert',
    data=body,
    headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}
)

try:
    res = urllib.request.urlopen(req)
    data = json.loads(res.read().decode())
    print('POST /convert response code:', res.status, 'body:', data)
    job_id = data.get('jobId')
    if job_id:
        for i in range(10):
            time.sleep(1)
            status_res = urllib.request.urlopen(f'http://localhost:8080/status/{job_id}')
            status_data = json.loads(status_res.read().decode())
            print(f'Poll {i+1}: status={status_data.get("status")} error={status_data.get("error")}')
            if status_data.get('status') in ('done', 'failed'):
                print('Final Result:', status_data)
                break
except Exception as e:
    print('Error:', e)
