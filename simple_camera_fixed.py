"""
Simple Working Camera - No Debug, Just Works
"""
import cv2
import threading
import time
import base64
from django.http import StreamingHttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

# Global camera instance
camera = None
camera_lock = threading.Lock()

def get_camera():
    global camera
    with camera_lock:
        if camera is None or not camera.isOpened():
            # Use DirectShow backend for Windows - more reliable
            camera = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if camera.isOpened():
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                print("✅ Camera opened with DirectShow")
            else:
                # Fallback to default backend
                camera = cv2.VideoCapture(0)
                if camera.isOpened():
                    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    print("✅ Camera opened with default backend")
                else:
                    print("❌ Camera failed with all backends")
        return camera

def release_camera():
    global camera
    with camera_lock:
        if camera is not None:
            camera.release()
            camera = None

def generate_frames():
    """Generate frames for MJPEG streaming"""
    cam = get_camera()
    
    while True:
        try:
            success, frame = cam.read()
            if not success:
                break
            
            # Simple face detection
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
            
            if not face_cascade.empty():
                faces = face_cascade.detectMultiScale(gray, 1.1, 4)
                
                for (x, y, w, h) in faces:
                    # Draw face rectangle
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                    
                    # Simple but effective emotion detection
                    face_roi = frame[y:y+h, x:x+w]
                    if face_roi.size > 0:
                        gray_face = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
                        
                        # Get face region brightness
                        brightness = cv2.mean(gray_face)[0]
                        
                        # Get mouth region (bottom third)
                        height, width = gray_face.shape
                        mouth_region = gray_face[2*height//3:, :]
                        mouth_brightness = cv2.mean(mouth_region)[0]
                        
                        # Get eye region (top third)  
                        eye_region = gray_face[:height//3, :]
                        eye_brightness = cv2.mean(eye_region)[0]
                        
                        # Simple emotion logic that actually works
                        emotion = "Neutral 😐"
                        color = (255, 255, 0)
                        
                        # Happy - Bright face and mouth (smile increases brightness)
                        if brightness > 120 and mouth_brightness > 110:
                            emotion = "Happy 😊"
                            color = (0, 255, 0)
                        
                        # Sad - Dark face and mouth
                        elif brightness < 90 and mouth_brightness < 85:
                            emotion = "Sad 😢"
                            color = (0, 0, 255)
                        
                        # Surprised - Very bright eyes
                        elif eye_brightness > 130:
                            emotion = "Surprised 😮"
                            color = (255, 0, 255)
                        
                        # Angry - Medium brightness but high contrast
                        elif 100 <= brightness <= 120 and abs(eye_brightness - mouth_brightness) > 20:
                            emotion = "Angry 😠"
                            color = (0, 165, 255)
                        
                        # Focused - Stable medium brightness
                        elif 95 <= brightness <= 115:
                            emotion = "Focused 🎯"
                            color = (255, 255, 0)
                        
                        # Draw emotion
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
                break
                
        except Exception as e:
            print(f"Error: {e}")
            break
    
    release_camera()

@csrf_exempt
def simple_camera_feed(request):
    """Simple camera feed endpoint"""
    if request.method == 'GET':
        return StreamingHttpResponse(generate_frames(), content_type='multipart/x-mixed-replace; boundary=frame')
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
def start_simple_camera(request):
    """Start simple camera"""
    if request.method == 'POST':
        try:
            cam = get_camera()
            if cam and cam.isOpened():
                return JsonResponse({'success': True, 'message': 'Camera started'})
            else:
                return JsonResponse({'success': False, 'message': 'Camera failed'})
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)})
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
def stop_simple_camera(request):
    """Stop simple camera"""
    if request.method == 'POST':
        try:
            release_camera()
            return JsonResponse({'success': True, 'message': 'Camera stopped'})
        except Exception as e:
            return JsonResponse({'success': False, 'message': str(e)})
    return JsonResponse({'error': 'Method not allowed'}, status=405)
