# FIXES FOR CAMERA NOT TURNING ON

## Critical Fixes Required:

### FIX #1: Demo Mode Activation Logic
**File**: `engagement/video_stream.py`
**Lines**: 49-100
**Problem**: Demo mode never activates when camera fails

```python
# CURRENT (BROKEN):
self.demo_mode = False
for backend in camera_backends:
    # ... tries backends
    if self.cap.isOpened():
        ret, test_frame = self.cap.read()
        if ret and test_frame is not None:
            self.demo_mode = False  # ← stays False
            break
        else:
            self.cap.release()  # ← but demo_mode STILL FALSE!

# AFTER FIX:
self.demo_mode = True  # ← Start as True (fallback)
for backend in camera_backends:
    # ... tries backends
    if self.cap.isOpened():
        ret, test_frame = self.cap.read()
        if ret and test_frame is not None:
            self.demo_mode = False  # ← Only set to False on SUCCESS
            break
        else:
            self.cap.release()
# If loop completes without success, self.demo_mode stays True
```

### FIX #2: Handle Both Detector Output Formats
**File**: `engagement/video_stream.py`
**Lines**: 195-199
**Problem**: Code looks for 'face_regions' but RealCameraDetector returns 'students'

```python
# CURRENT (only works with ClassroomDetector):
if result.get('face_regions'):
    result = self._add_face_recognition(result, frame.copy())

# AFTER FIX:
if result.get('face_regions') or result.get('students'):
    # Handle both detector output formats
    if result.get('face_regions') and not result.get('students'):
        # ClassroomDetector format - needs face recognition added
        result = self._add_face_recognition(result, frame.copy())
    # RealCameraDetector already has students, skip face recognition
```

### FIX #3: Initialize Current Frame Immediately
**File**: `engagement/video_stream.py`
**Lines**: 112-126
**Problem**: Frame stays None until first analysis (can take 5+ seconds)

```python
# Add after is_running = True:
self.current_frame = None
self.annotated_frame = None

# In __init__ or start():
# Ensure frames aren't None
self.frame_count = 0
self.fps = 0
self._fps_start = time.time()
```

### FIX #4: Always Update Frame Even Without Analysis
**File**: `engagement/video_stream.py`
**Lines**: 210-225
**Problem**: Frame doesn't update if no analysis is needed

```python
# CURRENT - may not update frame:
else:
    with self.lock:
        if self.annotated_frame is None:
            self.current_frame = frame
        else:
            self.current_frame = self.annotated_frame

# AFTER FIX:
# ALWAYS update the current frame to show live video
with self.lock:
    self.current_frame = frame  # ← Keep showing live frames
    if self.annotated_frame is not None:
        # If analysis available, prefer annotated version
        self.current_frame = self.annotated_frame
```

### FIX #5: MJPEG URL - Update if Needed for Docker/Production
**File**: `api.js`
**Line**: 8
**Current**: `const MJPEG_URL = 'http://127.0.0.1:8000/api/live/feed/';`
**For Docker/Production**: Change to `const MJPEG_URL = '/api/live/feed/';` (relative URL)

### FIX #6: Handle Stream Status Better
**File**: `engagement/views.py`
**Lines**: 605-620
**Problem**: Returns 503 error instead of fallback

```python
# CURRENT:
def video_feed(request):
    stream = get_video_stream()
    if not stream.is_running:
        return HttpResponse("Video stream not started.", status=503)

# AFTER FIX:
def video_feed(request):
    stream = get_video_stream()
    if not stream.is_running:
        # Don't give up - start stream in demo mode
        stream.start(source=0, session_id=None)
    
    if stream.is_running:
        return StreamingHttpResponse(
            generate_mjpeg_frames(),
            content_type='multipart/x-mixed-replace; boundary=frame'
        )
    else:
        return HttpResponse("Stream failed to start", status=503)
```

## Summary of Changes:
- ✅ Demo mode activates on camera failure
- ✅ Both detector formats supported
- ✅ Frames always display
- ✅ MJPEG stream always tries to start
- ✅ Better error handling

