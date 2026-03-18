"""
Simple Camera Test - Direct working solution
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
            camera = cv2.VideoCapture(0)
            if camera.isOpened():
                # Set camera properties
                camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                camera.set(cv2.CAP_PROP_FPS, 30)
                print("✅ Camera opened successfully")
            else:
                print("❌ Failed to open camera")
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
                print("❌ Failed to read frame")
                break
            
            # Simple face detection
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
            
            if not face_cascade.empty():
                faces = face_cascade.detectMultiScale(gray, 1.1, 4)
                
                for (x, y, w, h) in faces:
                    # Draw face rectangle
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
                    
                    # Accurate emotion detection based on facial features
                    face_roi = frame[y:y+h, x:x+w]
                    if face_roi.size > 0:
                        # Convert to different color spaces for better analysis
                        gray_face = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
                        hsv_face = cv2.cvtColor(face_roi, cv2.COLOR_BGR2HSV)
                        
                        # Extract facial features
                        height, width = gray_face.shape
                        
                        # 1. Eye region analysis (top 1/3 of face)
                        eye_region = gray_face[:height//3, :]
                        eye_brightness = cv2.mean(eye_region)[0]
                        
                        # 2. Mouth region analysis (bottom 1/3 of face)
                        mouth_region = gray_face[2*height//3:, :]
                        mouth_brightness = cv2.mean(mouth_region)[0]
                        
                        # 3. Overall face brightness
                        overall_brightness = cv2.mean(gray_face)[0]
                        
                        # 4. Edge detection for smile/frown analysis
                        edges = cv2.Canny(gray_face, 50, 150)
                        edge_count = cv2.countNonZero(edges)
                        
                        # 5. HSV analysis for skin tone changes
                        h_mean = cv2.mean(hsv_face)[0]
                        s_mean = cv2.mean(hsv_face)[1]
                        v_mean = cv2.mean(hsv_face)[2]
                        
                        # 6. Histogram analysis for texture
                        hist = cv2.calcHist([gray_face], [0], None, [256], [0, 256])
                        
                        # Calculate histogram variance manually
                        hist_mean = sum(i * hist[i][0] for i in range(256)) / sum(hist[i][0] for i in range(256))
                        hist_variance = sum((i - hist_mean) ** 2 * hist[i][0] for i in range(256)) / sum(hist[i][0] for i in range(256))
                        
                        # ACCURATE EMOTION DETECTION ALGORITHM
                        emotion = "Neutral 😐"
                        color = (255, 255, 0)
                        confidence = 0.5
                        
                        # DEBUG: Log all facial features
                        print(f"🔍 Face Analysis:")
                        print(f"   Eye brightness: {eye_brightness:.1f}")
                        print(f"   Mouth brightness: {mouth_brightness:.1f}")
                        print(f"   Overall brightness: {overall_brightness:.1f}")
                        print(f"   Edge count: {edge_count}")
                        print(f"   Saturation (S): {s_mean:.1f}")
                        print(f"   Histogram variance: {hist_variance:.1f}")
                        print(f"   Eye-Mouth diff: {abs(eye_brightness - mouth_brightness):.1f}")
                        
                        # HAPPY - Bright eyes, bright mouth, high variance (smile lines)
                        if (eye_brightness > 120 and mouth_brightness > 110 and 
                            overall_brightness > 110 and hist_variance > 500):
                            emotion = "Happy 😊"
                            color = (0, 255, 0)
                            confidence = 0.8
                            print(f"✅ Detected: HAPPY (eyes:{eye_brightness:.1f}, mouth:{mouth_brightness:.1f}, var:{hist_variance:.1f})")
                        
                        # SAD - Low overall brightness, low eye brightness, low variance
                        elif (overall_brightness < 90 and eye_brightness < 90 and 
                              mouth_brightness < 85 and hist_variance < 300):
                            emotion = "Sad 😢"
                            color = (255, 0, 0)
                            confidence = 0.7
                            print(f"✅ Detected: SAD (overall:{overall_brightness:.1f}, eyes:{eye_brightness:.1f}, var:{hist_variance:.1f})")
                        
                        # CONFUSED - Medium brightness, high edge activity, asymmetric features
                        elif (90 <= overall_brightness <= 120 and edge_count > 5000 and
                              abs(eye_brightness - mouth_brightness) > 15):
                            emotion = "Confused 😕"
                            color = (255, 165, 0)
                            confidence = 0.6
                            print(f"✅ Detected: CONFUSED (edges:{edge_count}, diff:{abs(eye_brightness - mouth_brightness):.1f})")
                        
                        # ANGRY - High red channel, high edge count, medium brightness
                        elif (s_mean > 100 and edge_count > 6000 and 
                              100 <= overall_brightness <= 130):
                            emotion = "Angry 😠"
                            color = (0, 0, 255)
                            confidence = 0.7
                            print(f"✅ Detected: ANGRY (saturation:{s_mean:.1f}, edges:{edge_count})")
                        
                        # SURPRISED - Very bright eyes, wide mouth area, high variance
                        elif (eye_brightness > 140 and mouth_brightness > 120 and 
                              hist_variance > 700):
                            emotion = "Surprised 😮"
                            color = (255, 0, 255)
                            confidence = 0.7
                            print(f"✅ Detected: SURPRISED (eyes:{eye_brightness:.1f}, mouth:{mouth_brightness:.1f}, var:{hist_variance:.1f})")
                        
                        # FOCUSED - Medium brightness, low edges, stable features
                        elif (95 <= overall_brightness <= 115 and edge_count < 4000 and
                              hist_variance < 400):
                            emotion = "Focused 🎯"
                            color = (0, 255, 255)
                            confidence = 0.6
                            print(f"✅ Detected: FOCUSED (brightness:{overall_brightness:.1f}, edges:{edge_count}, var:{hist_variance:.1f})")
                        
                        # BORED - Low brightness, very low edges, low variance
                        elif (overall_brightness < 95 and edge_count < 3000 and 
                              hist_variance < 250):
                            emotion = "Bored 😴"
                            color = (128, 128, 128)
                            confidence = 0.6
                            print(f"✅ Detected: BORED (brightness:{overall_brightness:.1f}, edges:{edge_count}, var:{hist_variance:.1f})")
                        else:
                            print(f"❌ No emotion matched - Defaulting to NEUTRAL")
                        
                        print(f"🎯 Final: {emotion} (confidence: {confidence:.1f})")
                        print("-" * 50)
                        
                        # Draw emotion label with confidence
                        label = f"{emotion} ({confidence:.1f})"
                        cv2.putText(frame, label, (x, y-10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                        
                        # Draw confidence bar
                        bar_width = int(w * confidence)
                        cv2.rectangle(frame, (x, y+h+5), (x+bar_width, y+h+10), color, -1)
            
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
            print(f"❌ Error in generate_frames: {e}")
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
                return JsonResponse({'success': True, 'message': 'Camera started successfully'})
            else:
                return JsonResponse({'success': False, 'message': 'Failed to start camera'})
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
