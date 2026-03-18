"""
Minimal Camera Test - Check Exactly What's Happening
"""
import cv2
import threading
import time
from django.http import StreamingHttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

camera = None
camera_lock = threading.Lock()

def get_camera():
    global camera
    with camera_lock:
        if camera is None or not camera.isOpened():
            print("🎥 Attempting to open camera...")
            camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            
            if camera.isOpened():
                print("✅ Camera opened successfully")
                # Test if we can read frames
                ret, test_frame = camera.read()
                if ret and test_frame is not None:
                    print(f"✅ Test frame: {test_frame.shape}")
                    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                else:
                    print("❌ Cannot read frames from camera")
                    camera.release()
                    camera = None
            else:
                print("❌ Failed to open camera")
                camera = None
        return camera

def generate_frames():
    cam = get_camera()
    if cam is None:
        print("❌ No camera available")
        return
    
    print("🎥 Starting frame generation...")
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    
    if face_cascade.empty():
        print("❌ Failed to load face cascade")
        return
    
    print("✅ Face cascade loaded")
    
    frame_count = 0
    while True:
        try:
            success, frame = cam.read()
            if not success:
                print("❌ Failed to read frame")
                break
            
            frame_count += 1
            
            # Add frame counter
            cv2.putText(frame, f"Frame: {frame_count}", (10, 60), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            # Face detection every 5 frames to reduce load
            if frame_count % 5 == 0:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(gray, 1.1, 4)
                
                print(f"🔍 Frame {frame_count}: Found {len(faces)} faces")
                
                for (x, y, w, h) in faces:
                    # Draw face rectangle
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                    
                    # Simple emotion detection
                    face_roi = frame[y:y+h, x:x+w]
                    if face_roi.size > 0:
                        gray_face = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
                        brightness = cv2.mean(gray_face)[0]
                        
                        print(f"   Face brightness: {brightness:.1f}")
                        
                        # Very simple emotion logic
                        if brightness > 120:
                            emotion = "Happy 😊"
                            color = (0, 255, 0)
                        elif brightness < 90:
                            emotion = "Sad 😢"
                            color = (0, 0, 255)
                        else:
                            emotion = "Neutral 😐"
                            color = (255, 255, 0)
                        
                        print(f"   Emotion: {emotion}")
                        cv2.putText(frame, emotion, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
            
            # Add timestamp
            cv2.putText(frame, f"Live Camera - {time.strftime('%H:%M:%S')}", (10, 30), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
            # Encode frame
            ret, buffer = cv2.imencode('.jpg', frame)
            if ret:
                frame_bytes = buffer.tobytes()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
            else:
                print("❌ Failed to encode frame")
                break
                
        except Exception as e:
            print(f"❌ Error in frame generation: {e}")
            break
    
    print("🎥 Frame generation stopped")

@csrf_exempt
def simple_camera_feed(request):
    if request.method == 'GET':
        print("🎥 Camera feed requested")
        return StreamingHttpResponse(generate_frames(), content_type='multipart/x-mixed-replace; boundary=frame')
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
def start_simple_camera(request):
    if request.method == 'POST':
        try:
            print("🎥 Start camera requested")
            cam = get_camera()
            if cam and cam.isOpened():
                print("✅ Camera start successful")
                return JsonResponse({'success': True, 'message': 'Camera started'})
            else:
                print("❌ Camera start failed")
                return JsonResponse({'success': False, 'message': 'Camera failed'})
        except Exception as e:
            print(f"❌ Camera start error: {e}")
            return JsonResponse({'success': False, 'message': str(e)})
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
def stop_simple_camera(request):
    if request.method == 'POST':
        try:
            print("🎥 Stop camera requested")
            global camera
            with camera_lock:
                if camera is not None:
                    camera.release()
                    camera = None
            print("✅ Camera stopped")
            return JsonResponse({'success': True, 'message': 'Camera stopped'})
        except Exception as e:
            print(f"❌ Camera stop error: {e}")
            return JsonResponse({'success': False, 'message': str(e)})
    return JsonResponse({'error': 'Method not allowed'}, status=405)
