import os
import time
import json

time.sleep(2)

os.environ['DJANGO_SETTINGS_MODULE'] = 'smartclass_backend.settings'
import django
django.setup()

from django.test import Client

client = Client(enforce_csrf_checks=False)
response = client.post('/api/sessions/start/', data=json.dumps({
    'class_name': 'CS101', 
    'subject': 'CS',
    'camera_source': '0'
}), content_type='application/json')

print(f'Endpoint Status: {response.status_code}')
if response.status_code == 200:
    result = json.loads(response.content)
    print(f'Session ID: {result["session"]["id"]}')
    print(f'Camera Started: {result["session"]["camera_started"]}')
else:
    print(f'Error: {response.content.decode()}')
