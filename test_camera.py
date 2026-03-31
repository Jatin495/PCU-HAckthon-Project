import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'smartclass_backend.settings'
import django
django.setup()
from engagement.camera import CameraProcessor

cp = CameraProcessor()
print('Testing camera initialization...')
print(f'✓ CameraProcessor initialized')

print('\nTesting camera start...')
try:
    result = cp.start(source=0)
    print(f'Camera start returned: {result}')
    print(f'Camera running: {cp.is_running}')
    if cp.is_running:
        print('✓ Camera started successfully')
        import time
        time.sleep(1)
        cp.stop()
        print('✓ Camera stopped')
    else:
        print('✗ Camera did not start (is_running = False)')
except Exception as e:
    print(f'✗ Camera start failed: {e}')
    import traceback
    traceback.print_exc()
