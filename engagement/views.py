"""
SmartClass Monitor - REST API Views
All endpoints that the frontend HTML/JS communicates with.
"""

import json
import hashlib
import logging
import threading
import base64
import time
from datetime import datetime, timedelta

from django.http import StreamingHttpResponse, JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.db.models import Avg, Count, Max, Min
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status
import pandas as pd

from .models import (
    Teacher, Student, ClassSession, Attendance,
    EngagementRecord, ClassEngagementSnapshot, Alert
)

logger = logging.getLogger(__name__)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def json_response(data, status_code=200):
    return JsonResponse(data, status=status_code, safe=False)


# ─── Auth Endpoints ───────────────────────────────────────────────────────────

@csrf_exempt
@api_view(['POST'])
def login(request):
    """Teacher login endpoint"""
    try:
        data = request.data
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')

        if not email or not password:
            return Response({'error': 'Email and password required'}, status=400)

        teacher = Teacher.objects.filter(email=email, is_active=True).first()

        if not teacher:
            teacher = Teacher.objects.create(
                name='Demo Teacher',
                email=email,
                password_hash=hash_password(password),
                subject='Computer Science'
            )
            logger.info(f"Auto-created teacher: {email}")

        if teacher.password_hash != hash_password(password):
            return Response({'error': 'Invalid credentials'}, status=401)

        request.session['teacher_id'] = teacher.id
        request.session['teacher_name'] = teacher.name
        request.session.modified = True

        return Response({
            'success': True,
            'teacher': {
                'id': teacher.id,
                'name': teacher.name,
                'email': teacher.email,
                'subject': teacher.subject,
            }
        })
    except Exception as e:
        logger.error(f"Login error: {e}")
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
def logout_view(request):
    request.session.flush()
    return Response({'success': True})


# ─── Dashboard Endpoints ──────────────────────────────────────────────────────

@api_view(['GET'])
def dashboard_stats(request):
    try:
        today = timezone.now().date()
        total_students = Student.objects.filter(is_active=True).count()
        present_today = Attendance.objects.filter(date=today, is_present=True).count()
        active_session = ClassSession.objects.filter(status='active').first()
        today_records = EngagementRecord.objects.filter(timestamp__date=today)
        avg_engagement = today_records.aggregate(avg=Avg('engagement_score'))['avg'] or 0
        active_alerts = Alert.objects.filter(is_resolved=False).count()

        thirty_min_ago = timezone.now() - timedelta(minutes=30)
        recent_records = EngagementRecord.objects.filter(timestamp__gte=thirty_min_ago)
        emotion_dist = {}
        for record in recent_records:
            e = record.emotion
            emotion_dist[e] = emotion_dist.get(e, 0) + 1

        return Response({
            'total_students': total_students,
            'present_today': present_today,
            'avg_engagement': round(avg_engagement, 1),
            'active_alerts': active_alerts,
            'active_session': None if not active_session else {
                'id': active_session.id,
                'name': active_session.class_name,
                'duration': active_session.duration_minutes
            },
            'emotion_distribution': emotion_dist,
            'date': today.isoformat(),
        })
    except Exception as e:
        logger.error(f"Dashboard stats error: {e}")
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def engagement_timeline(request):
    try:
        session_id = request.query_params.get('session_id')
        one_hour_ago = timezone.now() - timedelta(hours=1)
        snapshots = ClassEngagementSnapshot.objects.filter(timestamp__gte=one_hour_ago)
        if session_id:
            snapshots = snapshots.filter(session_id=session_id)
        snapshots = snapshots.order_by('timestamp')
        data = [{
            'time': s.timestamp.strftime('%H:%M:%S'),
            'engagement': round(s.avg_engagement, 1),
            'attention': round(s.avg_attention, 1),
            'present': s.present_count,
        } for s in snapshots]
        return Response({'timeline': data})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Students Endpoints ───────────────────────────────────────────────────────

@api_view(['GET'])
def list_students(request):
    try:
        students = Student.objects.filter(is_active=True)
        today = timezone.now().date()
        result = []
        for student in students:
            latest = EngagementRecord.objects.filter(student=student).order_by('-timestamp').first()
            attendance = Attendance.objects.filter(student=student, date=today).first()
            result.append({
                'id': student.id,
                'student_id': student.student_id,
                'name': student.name,
                'email': student.email,
                'seat_row': student.seat_row,
                'seat_col': student.seat_col,
                'face_registered': bool(student.face_encoding),
                'present_today': attendance.is_present if attendance else False,
                'current_emotion': latest.emotion if latest else 'unknown',
                'current_engagement': round(latest.engagement_score, 1) if latest else 0,
                'avg_engagement': round(latest.engagement_score, 1) if latest else 0,
                'last_updated': latest.timestamp.isoformat() if latest else None,
            })
        return Response({'students': result, 'total': len(result)})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def students_overview(request):
    """
    Aggregated student page metrics for Chart.js.
    Returns attendance overview (last 4 weeks), engagement distribution (latest per student),
    and class performance trend (last 7 days).
    """
    try:
        now = timezone.now()
        today = now.date()

        # ── Attendance overview: last 28 days grouped into 4 weeks ────────────
        start_date = today - timedelta(days=27)
        attendance_qs = Attendance.objects.filter(date__gte=start_date, date__lte=today).only('date', 'is_present', 'student_id')
        # Aggregate by day using unique students (sessions can create duplicates).
        per_day = {}
        for a in attendance_qs:
            d = a.date
            if d not in per_day:
                per_day[d] = {'present': set(), 'absent': set()}
            if a.is_present:
                per_day[d]['present'].add(a.student_id)
            else:
                per_day[d]['absent'].add(a.student_id)

        # Then group days into 4 week buckets and average per day.
        present_sum = [0, 0, 0, 0]
        absent_sum = [0, 0, 0, 0]
        days_count = [0, 0, 0, 0]
        for d, sets in per_day.items():
            days_ago = (today - d).days
            bucket = 3 - min(3, max(0, days_ago // 7))
            present_sum[bucket] += len(sets['present'])
            absent_sum[bucket] += len(sets['absent'])
            days_count[bucket] += 1

        present_by_week = [
            round(present_sum[i] / days_count[i], 1) if days_count[i] else 0 for i in range(4)
        ]
        absent_by_week = [
            round(absent_sum[i] / days_count[i], 1) if days_count[i] else 0 for i in range(4)
        ]

        attendance_overview = {
            'labels': ['Week 1', 'Week 2', 'Week 3', 'Week 4'],
            'present': present_by_week,
            'absent': absent_by_week,
        }

        # ── Engagement distribution: latest record per student (fallback 0) ──
        students = Student.objects.filter(is_active=True).only('id')
        bins_labels = ['90-100%', '80-89%', '70-79%', '60-69%', 'Below 60%']
        bins_counts = [0, 0, 0, 0, 0]

        for s in students:
            latest = EngagementRecord.objects.filter(student=s).order_by('-timestamp').only('engagement_score').first()
            score = float(latest.engagement_score) if latest and latest.engagement_score is not None else 0.0
            if score >= 90:
                bins_counts[0] += 1
            elif score >= 80:
                bins_counts[1] += 1
            elif score >= 70:
                bins_counts[2] += 1
            elif score >= 60:
                bins_counts[3] += 1
            else:
                bins_counts[4] += 1

        engagement_distribution = {
            'labels': bins_labels,
            'counts': bins_counts,
        }

        # ── Class performance: last 7 days averages by weekday ───────────────
        since = now - timedelta(days=7)
        recs = EngagementRecord.objects.filter(timestamp__gte=since).values(
            'timestamp', 'engagement_score', 'attention_score'
        )

        weekday_labels = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        eng_sum = [0.0] * 7
        att_sum = [0.0] * 7
        eng_cnt = [0] * 7
        att_cnt = [0] * 7

        for r in recs:
            ts = r.get('timestamp')
            if not ts:
                continue
            idx = ts.weekday()  # Mon=0..Sun=6
            e = r.get('engagement_score')
            a = r.get('attention_score')
            if e is not None:
                eng_sum[idx] += float(e)
                eng_cnt[idx] += 1
            if a is not None:
                att_sum[idx] += float(a)
                att_cnt[idx] += 1

        engagement_avg = [round((eng_sum[i] / eng_cnt[i]), 1) if eng_cnt[i] else 0 for i in range(7)]
        attention_avg = [round((att_sum[i] / att_cnt[i]), 1) if att_cnt[i] else 0 for i in range(7)]

        class_performance = {
            'labels': weekday_labels,
            'avg_engagement': engagement_avg,
            'avg_attention': attention_avg,
        }

        return Response({
            'attendance_overview': attendance_overview,
            'engagement_distribution': engagement_distribution,
            'class_performance': class_performance,
            'generated_at': now.isoformat(),
        })
    except Exception as e:
        logger.error(f"Students overview error: {e}")
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def add_student(request):
    try:
        data = request.data
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        seat_row = data.get('seat_row', 1)
        seat_col = data.get('seat_col', 1)
        face_image = request.FILES.get('face_image')  # Face image for registration
        
        if not name:
            return Response({'error': 'Name is required'}, status=400)
        
        count = Student.objects.count() + 1
        student_id = f"STU{count:03d}"
        
        # Process face encoding if image provided
        face_encoding = None
        if face_image:
            try:
                import cv2
                import numpy as np
                from engagement.simple_detector import SimpleFaceDetector
                
                # Read and process face image
                image_bytes = face_image.read()
                nparr = np.frombuffer(image_bytes, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                if img is not None:
                    detector = SimpleFaceDetector()
                    face_regions = detector.detect_faces(img, lenient=True)  # Use lenient mode for registration
                    
                    if face_regions:
                        # Use the first detected face for encoding
                        face_roi = face_regions[0]['face_roi']
                        # Generate a simple encoding (face ROI dimensions + color histogram)
                        face_encoding = generate_face_encoding(face_roi)
                        logger.info(f"✅ Face registered for student {name}")
                    else:
                        logger.warning(f"No face detected in image for {name} - using center region")
                        # Fallback: use center region of image (this will work for registration)
                        h, w = img.shape[:2]
                        # Use a larger center region for better results
                        center_size = min(h, w) // 2
                        center_x, center_y = w // 2, h // 2
                        face_roi = img[max(0, center_y-center_size//2):min(h, center_y+center_size//2), 
                                       max(0, center_x-center_size//2):min(w, center_x+center_size//2)]
                        
                        # Ensure we have a valid region
                        if face_roi.size > 0:
                            face_encoding = generate_face_encoding(face_roi)
                            if face_encoding:
                                logger.info(f"✅ Face registered using center region for {name}")
                            else:
                                logger.error(f"Failed to generate encoding for {name}")
                        else:
                            logger.error(f"Invalid image region for {name}")
                else:
                    logger.error(f"Could not decode face image for {name}")
                    
            except Exception as e:
                logger.error(f"Face processing error for {name}: {e}")
        
        student = Student.objects.create(
            student_id=student_id, name=name, email=email,
            seat_row=seat_row, seat_col=seat_col,
            face_encoding=json.dumps(face_encoding) if face_encoding is not None else None
        )

        # Keep the live recognition cache in sync with newly registered students.
        if face_encoding is not None:
            try:
                from .face_recognition import get_face_recognition_system
                get_face_recognition_system().refresh_encodings()
            except Exception as refresh_error:
                logger.warning(f"Face cache refresh failed for {name}: {refresh_error}")
        
        return Response({'success': True, 'student': {
            'id': student.id, 'student_id': student.student_id, 'name': student.name,
            'face_registered': face_encoding is not None
        }})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


def generate_face_encoding(face_roi):
    """Generate a simple face encoding from face ROI"""
    try:
        import cv2
        import numpy as np
        
        # Resize face to standard size
        face_resized = cv2.resize(face_roi, (64, 64))
        
        # Convert to different color spaces
        face_gray = cv2.cvtColor(face_resized, cv2.COLOR_BGR2GRAY)
        face_hsv = cv2.cvtColor(face_resized, cv2.COLOR_BGR2HSV)
        
        # Generate features: dimensions + color histograms
        encoding = []
        
        # Add dimensions
        encoding.extend([face_roi.shape[0], face_roi.shape[1]])
        
        # Add color histograms (simplified)
        for channel in range(3):
            hist = cv2.calcHist([face_resized], [channel], None, [16], [0, 256])
            encoding.extend(hist.flatten())
        
        # Add gray histogram
        gray_hist = cv2.calcHist([face_gray], [0], None, [8], [0, 256])
        encoding.extend(gray_hist.flatten())
        
        # Convert to numpy array and normalize
        encoding = np.array(encoding, dtype=np.float32)
        encoding = encoding / (np.linalg.norm(encoding) + 1e-5)
        
        return encoding.tolist()
        
    except Exception as e:
        logger.error(f"Face encoding error: {e}")
        return None


@api_view(['GET'])
def student_detail(request, student_id):
    try:
        student = Student.objects.get(student_id=student_id, is_active=True)
        seven_days = timezone.now() - timedelta(days=7)
        records = EngagementRecord.objects.filter(student=student, timestamp__gte=seven_days).order_by('timestamp')

        if records.exists():
            df = pd.DataFrame(list(records.values('timestamp', 'engagement_score', 'attention_score', 'emotion')))
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df['date'] = df['timestamp'].dt.date
            daily_avg = df.groupby('date').agg({'engagement_score': 'mean', 'attention_score': 'mean'}).reset_index()
            engagement_history = [{
                'date': str(row['date']),
                'avg_engagement': round(row['engagement_score'], 1),
                'avg_attention': round(row['attention_score'], 1),
            } for _, row in daily_avg.iterrows()]
            emotion_freq = df['emotion'].value_counts().to_dict()
        else:
            engagement_history = []
            emotion_freq = {}

        thirty_days = timezone.now().date() - timedelta(days=30)
        attendance = Attendance.objects.filter(student=student, date__gte=thirty_days)
        attend_rate = attendance.filter(is_present=True).count() / max(attendance.count(), 1) * 100

        return Response({
            'student': {
                'id': student.id, 'student_id': student.student_id,
                'name': student.name, 'email': student.email,
                'seat_row': student.seat_row, 'seat_col': student.seat_col,
            },
            'engagement_history': engagement_history,
            'emotion_frequency': emotion_freq,
            'attendance_rate': round(attend_rate, 1),
        })
    except Student.DoesNotExist:
        return Response({'error': 'Student not found'}, status=404)
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Session Endpoints ────────────────────────────────────────────────────────

@api_view(['GET'])
def list_sessions(request):
    try:
        sessions = ClassSession.objects.all()[:20]
        return Response({'sessions': [{
            'id': s.id, 'class_name': s.class_name, 'subject': s.subject,
            'start_time': s.start_time.isoformat(),
            'end_time': s.end_time.isoformat() if s.end_time else None,
            'status': s.status, 'duration_minutes': s.duration_minutes,
            'total_students': s.total_students,
        } for s in sessions]})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
def start_session(request):
    try:
        data = request.data
        class_name = data.get('class_name', 'CS101')
        subject = data.get('subject', 'Computer Science')
        camera_source = data.get('camera_source', '0')
        teacher_id_raw = data.get('teacher_id', 1)
        teacher_id = 1
        try:
            teacher_id = int(str(teacher_id_raw).replace('teacher', ''))
        except (ValueError, TypeError):
            teacher_id = 1

        teacher = Teacher.objects.filter(id=teacher_id).first()
        if not teacher:
            teacher, _ = Teacher.objects.get_or_create(
                email='demo@smartclass.com',
                defaults={'name': 'Demo Teacher', 'password_hash': hash_password('demo123')}
            )

        ClassSession.objects.filter(status='active').update(status='ended', end_time=timezone.now())

        session = ClassSession.objects.create(
            teacher=teacher, class_name=class_name, subject=subject,
            camera_source=camera_source,
            total_students=Student.objects.filter(is_active=True).count(),
        )

        from .video_stream import start_stream, stop_stream

        # Reset any stale stream instance before starting a new session.
        try:
            stop_stream()
        except Exception:
            pass

        cam_source = int(str(camera_source)) if str(camera_source).isdigit() else 0
        camera_candidates = [cam_source, 0, 1, 2]
        # Keep unique order while preserving preferred source first.
        camera_candidates = list(dict.fromkeys(camera_candidates))

        started = False
        selected_source = cam_source
        for source_idx in camera_candidates:
            started = start_stream(source=source_idx, session_id=session.id)
            if started:
                selected_source = source_idx
                break
            # Ensure failed attempt is fully released before next source.
            try:
                stop_stream()
            except Exception:
                pass

        from .video_stream import get_video_stream
        stream = get_video_stream()
        camera_started = bool(started or stream.is_running)
        session.camera_source = str(selected_source)
        session.save(update_fields=['camera_source'])

        today = timezone.now().date()
        for student in Student.objects.filter(is_active=True):
            Attendance.objects.get_or_create(
                student=student, session=session, date=today,
                defaults={'is_present': True, 'arrival_time': timezone.now()}
            )

        return Response({'success': True, 'session': {
            'id': session.id, 'class_name': session.class_name,
            'start_time': session.start_time.isoformat(),
            'camera_started': camera_started,
            'camera_source': str(selected_source),
        }})
    except Exception as e:
        logger.error(f"Start session error: {e}")
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
def end_session(request, session_id):
    try:
        logger.info(f"🛑 Ending session {session_id}")
        
        session = ClassSession.objects.get(id=session_id)
        session.status = 'ended'
        session.end_time = timezone.now()
        session.save()
        
        # Stop the video stream immediately
        from .video_stream import stop_stream
        stream_stopped = stop_stream()
        
        if stream_stopped:
            logger.info(f"✅ Session {session_id} ended successfully - Stream stopped")
        else:
            logger.warning(f"⚠️ Session {session_id} ended but stream stop failed")
        
        return Response({
            'success': True, 
            'duration': session.duration_minutes,
            'stream_stopped': stream_stopped
        })
        
        
    except ClassSession.DoesNotExist:
        logger.error(f"❌ Session {session_id} not found")
        return Response({'error': 'Session not found'}, status=404)
    except Exception as e:
        logger.error(f"❌ Error ending session {session_id}: {e}")
        return Response({'error': str(e)}, status=500)


# ─── Live Monitoring Endpoints ────────────────────────────────────────────────

@api_view(['GET'])
def video_feed(request):
    try:
        from .video_stream import get_video_stream, generate_mjpeg_frames, start_stream
        stream = get_video_stream()
        
        # If stream isn't running, try to start it (will use demo mode if camera unavailable)
        if not stream.is_running:
            logger.info("📹 Stream not running, attempting to start...")
            start_stream(source=0, session_id=None)
        
        if stream.is_running:
            response = StreamingHttpResponse(
                generate_mjpeg_frames(),
                content_type='multipart/x-mixed-replace; boundary=frame'
            )
            response['Cache-Control'] = 'no-cache'
            return response
        else:
            logger.error("❌ Stream failed to start")
            return HttpResponse("Failed to start video stream", status=503)
    except Exception as e:
        logger.error(f"Stream error: {e}")
        return HttpResponse(f"Stream error: {e}", status=500)

@csrf_exempt
@api_view(['POST'])
def stop_stream_force(request):
    try:
        from .video_stream import stop_stream
        stopped = stop_stream()
        return Response({'success': True, 'stream_stopped': stopped})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def live_data(request):
    try:
        from .video_stream import get_video_stream
        stream = get_video_stream()
        stream_status = stream.get_status()
        analysis = stream.get_latest_analysis()
        active_session = ClassSession.objects.filter(status='active').first()

        recent_snapshots = ClassEngagementSnapshot.objects.filter(
            timestamp__gte=timezone.now() - timedelta(minutes=5)
        ).order_by('-timestamp')[:6]
        timeline = [{
            'time': s.timestamp.strftime('%H:%M:%S'),
            'engagement': round(s.avg_engagement, 1),
            'present': s.present_count,
        } for s in reversed(list(recent_snapshots))]

        # Generate real-time alerts ONLY for students actually detected in camera
        alerts_data = []
        if analysis and analysis.get('recognized_students'):
            detected_students = analysis['recognized_students']
            
            # Show alerts for ALL detected students (both recognized and placeholder)
            for student_data in detected_students:
                student_name = student_data.get('name', 'Unknown Person')
                student_id = student_data.get('student_id')
                emotion = student_data.get('emotion', 'neutral')
                engagement = student_data.get('engagement', 0)
                confidence = student_data.get('confidence', 0)
                student_alert_created = False
                
                # Skip if no student name
                if not student_name or student_name == 'Unknown Person':
                    continue
                
                # Low engagement alert
                if engagement < 60:
                    alerts_data.append({
                        'id': f"engagement_{student_id}_{int(time.time())}",
                        'type': 'low_engagement',
                        'severity': 'medium',
                        'message': f"{student_name} shows low engagement ({engagement}%)",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': timezone.now().strftime('%H:%M:%S'),
                    })
                    student_alert_created = True
                
                # Emotion-based alerts (only 4 emotions: happy, bored, confused, neutral)
                if emotion == 'confused':
                    alerts_data.append({
                        'id': f"confused_{student_id}_{int(time.time())}",
                        'type': 'confused',
                        'severity': 'high',
                        'message': f"{student_name} appears confused",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': timezone.now().strftime('%H:%M:%S'),
                    })
                    student_alert_created = True
                elif emotion == 'bored':
                    alerts_data.append({
                        'id': f"bored_{student_id}_{int(time.time())}",
                        'type': 'bored',
                        'severity': 'medium',
                        'message': f"{student_name} appears bored",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': timezone.now().strftime('%H:%M:%S'),
                    })
                    student_alert_created = True
                elif emotion == 'happy':
                    alerts_data.append({
                        'id': f"happy_{student_id}_{int(time.time())}",
                        'type': 'happy',
                        'severity': 'info',
                        'message': f"{student_name} appears happy and engaged",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': timezone.now().strftime('%H:%M:%S'),
                    })
                    student_alert_created = True
                
                # Always show at least one info alert per detected student.
                if not student_alert_created:
                    alerts_data.append({
                        'id': f"detection_{student_id}_{int(time.time())}",
                        'type': 'detection',
                        'severity': 'info',
                        'message': f"{student_name} is present in class",
                        'student_name': student_name,
                        'student_id': student_id,
                        'time': timezone.now().strftime('%H:%M:%S'),
                    })
            
            # Sort alerts by time (most recent first) and limit to 10
            alerts_data.sort(key=lambda x: x['time'], reverse=True)
            alerts_data = alerts_data[:10]

        if analysis:
            # Prepare students data - SHOW ALL DETECTED STUDENTS
            students_data = []
            if analysis.get('recognized_students'):
                for student_data in analysis['recognized_students']:
                    student_name = student_data.get('name', 'Unknown Person')
                    student_id = student_data.get('student_id')
                    
                    # Skip if no student name
                    if not student_name or student_name == 'Unknown Person':
                        continue
                    
                    students_data.append({
                        'student_id': student_id,
                        'name': student_name,
                        'engagement_score': student_data.get('engagement', 0),
                        'emotion': student_data.get('emotion', 'neutral'),
                        'confidence': student_data.get('confidence', 0),
                        'present_today': True,  # These are detected students
                        'face_registered': student_data['student_id'] is not None
                    })
            elif analysis.get('students'):
                # Fallback for RealCameraDetector: it returns analysis['students'] directly
                for i, student_data in enumerate(analysis['students']):
                    students_data.append({
                        'student_id': student_data.get('student_id') or student_data.get('face_index') or f"FACE_{i+1}",
                        'name': student_data.get('student_name') or student_data.get('name') or f"Detected Face {i+1}",
                        'engagement_score': student_data.get('engagement_score', 0),
                        'emotion': student_data.get('emotion', 'neutral'),
                        'confidence': student_data.get('emotion_confidence', 0),
                        'present_today': True,
                        'face_registered': bool(student_data.get('student_id')),
                    })

            return Response({
                'stream_active': stream_status['is_running'],
                'fps': stream_status['fps'],
                'session_id': active_session.id if active_session else None,
                'present_count': analysis.get('present_count', len([s for s in students_data if s.get('present_today', True)])),
                'avg_engagement': analysis.get('class_avg_engagement', 0),
                'emotion_distribution': analysis.get('emotion_distribution', {}),
                'students': students_data[:12],
                'recognized_students': analysis.get('recognized_students', []),
                'timeline': timeline,
                'alerts': alerts_data,
                'timestamp': analysis.get('timestamp'),
            })
        else:
            return Response(_generate_demo_live_data(active_session, timeline, alerts_data))
    except Exception as e:
        logger.error(f"Live data error: {e}")
        return Response({'error': str(e), 'stream_active': False}, status=500)


def _generate_demo_live_data(active_session, timeline, alerts_data):
    import random
    emotions = ['happy', 'neutral', 'confused', 'bored', 'focused']
    students_demo = [{
        'face_index': i,
        'emotion': random.choice(emotions),
        'emotion_confidence': round(random.uniform(0.6, 0.95), 2),
        'engagement_score': random.randint(50, 95),
        'attention_score': random.randint(60, 95),
        'posture_score': random.randint(70, 95),
        'is_looking_forward': random.random() > 0.3,
        'face_detected': True,
    } for i in range(8)]
    avg_eng = sum(s['engagement_score'] for s in students_demo) / len(students_demo)
    return {
        'stream_active': False, 'fps': 0,
        'session_id': active_session.id if active_session else None,
        'present_count': 8, 'avg_engagement': round(avg_eng, 1),
        'emotion_distribution': {'happy': 3, 'neutral': 2, 'confused': 2, 'bored': 1},
        'students': students_demo, 'timeline': timeline, 'alerts': alerts_data,
        'timestamp': timezone.now().isoformat(), 'demo_mode': True,
    }


@api_view(['GET'])
def stream_frame(request):
    try:
        from .video_stream import get_video_stream
        stream = get_video_stream()
        if not stream.is_running:
            return Response({'error': 'Stream not active', 'frame': None})
        frame_b64 = stream.get_frame_base64()
        return Response({'frame': frame_b64, 'timestamp': time.time()})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Alerts ───────────────────────────────────────────────────────────────────



# ─── Alert System ───────────────────────────────────────────────────────────

@api_view(['GET'])
def check_engagement_alert(request):
    """Check engagement data and trigger alerts if confusion > 30%"""
    try:
        from django.utils import timezone
        from datetime import timedelta
        
        # Get latest engagement data from last 5 minutes
        five_minutes_ago = timezone.now() - timedelta(minutes=5)
        
        # Get latest engagement records per student (SQLite compatible)
        latest_records = []
        
        # Get all students with recent engagement data
        student_ids = EngagementRecord.objects.filter(
            timestamp__gte=five_minutes_ago,
            face_detected=True
        ).values_list('student_id', flat=True).distinct()
        
        # For each student, get their latest record
        for student_id in student_ids:
            latest_record = EngagementRecord.objects.filter(
                student_id=student_id,
                timestamp__gte=five_minutes_ago,
                face_detected=True
            ).order_by('-timestamp').first()
            
            if latest_record:
                latest_records.append(latest_record)
        
        if not latest_records:
            return Response({
                'alert': False,
                'percentage': 0,
                'message': 'No engagement data available',
                'total_students': 0,
                'confused_students': 0
            })
        
        total_students = len(latest_records)
        confused_students = 0
        
        for record in latest_records:
            # Student is confused if emotion = confused OR engagement_score < 0.4
            if record.emotion == 'confused' or record.engagement_score < 40.0:
                confused_students += 1
        
        percentage_confused = (confused_students / total_students) * 100 if total_students > 0 else 0
        
        # Check if alert should be triggered
        alert_triggered = percentage_confused > 30.0
        
        if alert_triggered:
            # Create alert in database
            active_session = ClassSession.objects.filter(status='active').first()
            
            if active_session:
                Alert.objects.create(
                    session=active_session,
                    alert_type='class_confusion',
                    severity='high' if percentage_confused > 50 else 'medium',
                    message=f'⚠ ALERT: More than 30% students appear confused or not engaged ({percentage_confused:.1f}%).',
                    timestamp=timezone.now()
                )
        
        return Response({
            'alert': alert_triggered,
            'percentage': round(percentage_confused, 1),
            'message': f'⚠ ALERT: More than 30% students appear confused ({percentage_confused:.1f}%).' if alert_triggered else 'Class engagement is normal',
            'total_students': total_students,
            'confused_students': confused_students,
            'timestamp': timezone.now().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error checking engagement alert: {e}")
        return Response({
            'error': str(e),
            'alert': False,
            'percentage': 0,
            'message': 'Error checking engagement data'
        }, status=500)


@api_view(['GET'])
def list_alerts(request):
    """List all unresolved alerts"""
    try:
        alerts = Alert.objects.filter(is_resolved=False).order_by('-timestamp')[:20]
        return Response({'alerts': [{
            'id': a.id, 'type': a.alert_type, 'severity': a.severity,
            'message': a.message,
            'student_name': a.student.name if a.student else 'Class',
            'student_id': a.student.student_id if a.student else None,
            'time': a.timestamp.isoformat(),
        } for a in alerts], 'count': alerts.count()})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
def resolve_alert(request, alert_id):
    try:
        alert = Alert.objects.get(id=alert_id)
        alert.is_resolved = True
        alert.resolved_at = timezone.now()
        alert.save()
        return Response({'success': True})
    except Alert.DoesNotExist:
        return Response({'error': 'Alert not found'}, status=404)
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Attendance ───────────────────────────────────────────────────────────────

@api_view(['GET'])
def attendance_report(request):
    try:
        date_str = request.query_params.get('date')
        from datetime import date
        target_date = date.fromisoformat(date_str) if date_str else timezone.now().date()
        attendances = Attendance.objects.filter(date=target_date).select_related('student')
        return Response({
            'date': target_date.isoformat(),
            'attendance': [{
                'student_id': a.student.student_id, 'student_name': a.student.name,
                'is_present': a.is_present,
                'arrival_time': a.arrival_time.isoformat() if a.arrival_time else None,
                'seat_row': a.student.seat_row, 'seat_col': a.student.seat_col,
            } for a in attendances],
            'present_count': attendances.filter(is_present=True).count(),
            'absent_count': attendances.filter(is_present=False).count(),
            'total': attendances.count(),
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Analytics ────────────────────────────────────────────────────────────────

@api_view(['GET'])
def analytics_summary(request):
    try:
        days = int(request.query_params.get('days', 7))
        since = timezone.now() - timedelta(days=days)
        records = EngagementRecord.objects.filter(timestamp__gte=since)

        if not records.exists():
            return Response({
                'message': 'No data available yet. Start a monitoring session.',
                'days': days, 'daily_engagement': [], 'hourly_pattern': [],
                'emotion_trend': {}, 'top_students': [], 'needs_attention': [],
                'overall_avg_engagement': 0,
            })

        df = pd.DataFrame(list(records.values(
            'timestamp', 'engagement_score', 'attention_score',
            'emotion', 'posture_score', 'student__name', 'student__student_id'
        )))
        df.columns = ['timestamp', 'engagement_score', 'attention_score',
                      'emotion', 'posture_score', 'student_name', 'student_id']
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour

        daily_avg = df.groupby('date').agg({'engagement_score': 'mean', 'attention_score': 'mean'}).reset_index()
        daily_engagement = [{'date': str(row['date']), 'engagement': round(row['engagement_score'], 1),
                              'attention': round(row['attention_score'], 1)} for _, row in daily_avg.iterrows()]

        hourly_avg = df.groupby('hour')['engagement_score'].mean().reset_index()
        hourly_pattern = [{'hour': f"{int(row['hour']):02d}:00", 'engagement': round(row['engagement_score'], 1)}
                          for _, row in hourly_avg.iterrows()]

        emotion_counts = df['emotion'].value_counts().to_dict()

        student_avg = df.groupby(['student_id', 'student_name'])['engagement_score'].mean().reset_index()
        student_avg_sorted = student_avg.sort_values('engagement_score', ascending=False)

        top_students = [{'student_id': row['student_id'], 'name': row['student_name'],
                         'avg_engagement': round(row['engagement_score'], 1)}
                        for _, row in student_avg_sorted.head(5).iterrows()]
        needs_attention = [{'student_id': row['student_id'], 'name': row['student_name'],
                            'avg_engagement': round(row['engagement_score'], 1)}
                           for _, row in student_avg_sorted.tail(5).iterrows()]

        return Response({
            'days': days, 'total_records': len(df),
            'daily_engagement': daily_engagement, 'hourly_pattern': hourly_pattern,
            'emotion_trend': emotion_counts, 'top_students': top_students,
            'needs_attention': needs_attention,
            'overall_avg_engagement': round(df['engagement_score'].mean(), 1),
        })
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        return Response({'error': str(e)}, status=500)


# ─── Heatmap ──────────────────────────────────────────────────────────────────

@api_view(['GET'])
def classroom_heatmap(request):
    try:
        today = timezone.now().date()
        students = Student.objects.filter(is_active=True)
        heatmap_data = []
        for student in students:
            latest = EngagementRecord.objects.filter(student=student, timestamp__date=today).order_by('-timestamp').first()
            attend = Attendance.objects.filter(student=student, date=today).first()
            engagement = latest.engagement_score if latest else 0
            emotion = latest.emotion if latest else 'unknown'
            present = attend.is_present if attend else False

            if not present:
                level, color = 'absent', '#374151'
            elif engagement >= 80:
                level, color = 'high', '#22c55e'
            elif engagement >= 60:
                level, color = 'medium', '#3b82f6'
            elif engagement >= 40:
                level, color = 'low', '#f59e0b'
            else:
                level, color = 'very_low', '#ef4444'

            heatmap_data.append({
                'student_id': student.student_id, 'name': student.name,
                'seat_row': student.seat_row, 'seat_col': student.seat_col,
                'engagement': round(engagement, 1), 'emotion': emotion,
                'present': present, 'level': level, 'color': color,
            })
        return Response({'heatmap': heatmap_data})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Session Report ───────────────────────────────────────────────────────────

@api_view(['GET'])
def session_report(request, session_id):
    try:
        session = ClassSession.objects.get(id=session_id)
        records = EngagementRecord.objects.filter(session=session)
        snapshots = ClassEngagementSnapshot.objects.filter(session=session).order_by('timestamp')
        attendance = Attendance.objects.filter(session=session)
        alerts_list = Alert.objects.filter(session=session)

        if records.exists():
            df = pd.DataFrame(list(records.values('timestamp', 'engagement_score', 'emotion', 'student__name')))
            df.columns = ['timestamp', 'engagement_score', 'emotion', 'student_name']
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            overall_avg = df['engagement_score'].mean()
            emotion_dist = df['emotion'].value_counts().to_dict()
            timeline = [{'time': s.timestamp.strftime('%H:%M:%S'), 'engagement': round(s.avg_engagement, 1),
                         'present': s.present_count} for s in snapshots]
        else:
            overall_avg = 0
            emotion_dist = {}
            timeline = []

        return Response({
            'session': {
                'id': session.id, 'class_name': session.class_name, 'subject': session.subject,
                'start_time': session.start_time.isoformat(),
                'end_time': session.end_time.isoformat() if session.end_time else None,
                'duration_minutes': session.duration_minutes, 'status': session.status,
            },
            'summary': {
                'total_students': attendance.count(), 'present': attendance.filter(is_present=True).count(),
                'avg_engagement': round(overall_avg, 1), 'total_alerts': alerts_list.count(),
            },
            'emotion_distribution': emotion_dist, 'engagement_timeline': timeline,
        })
    except ClassSession.DoesNotExist:
        return Response({'error': 'Session not found'}, status=404)
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Seed Data ────────────────────────────────────────────────────────────────

@api_view(['POST'])
def seed_demo_data(request):
    try:
        import random
        student_names = [
            "Emma Johnson", "Liam Smith", "Olivia Davis", "Noah Wilson",
            "Ava Martinez", "Ethan Brown", "Sophia Garcia", "Mason Taylor",
            "Isabella Anderson", "William Thomas", "Mia Jackson", "James White",
            "Charlotte Harris", "Benjamin Lewis", "Amelia Walker", "Lucas Hall",
            "Harper Young", "Henry King", "Evelyn Wright", "Alexander Scott",
            "Abigail Green", "Michael Adams", "Emily Baker", "Daniel Carter"
        ]
        created = 0
        for i, name in enumerate(student_names):
            sid = f"STU{i+1:03d}"
            if not Student.objects.filter(student_id=sid).exists():
                Student.objects.create(
                    student_id=sid, name=name,
                    email=f"{name.lower().replace(' ', '.')}@student.edu",
                    seat_row=(i // 8) + 1, seat_col=(i % 8) + 1,
                )
                created += 1

        # Ensure we have a teacher + an ended "seed" session to attach records to
        teacher, _ = Teacher.objects.get_or_create(
            email='demo@smartclass.com',
            defaults={'name': 'Demo Teacher', 'password_hash': hash_password('demo123'), 'subject': 'Computer Science'}
        )
        seed_session, _ = ClassSession.objects.get_or_create(
            teacher=teacher,
            class_name='CS101',
            subject='Computer Science',
            status='ended',
            defaults={
                'start_time': timezone.now() - timedelta(days=30),
                'end_time': timezone.now() - timedelta(days=30) + timedelta(hours=1),
                'camera_source': '0',
                'total_students': Student.objects.filter(is_active=True).count(),
            }
        )

        students = list(Student.objects.filter(is_active=True))
        if students:
            # ── Attendance: last 28 days (unique_together prevents duplicates) ──
            attendance_created = 0
            for days_ago in range(27, -1, -1):
                d = timezone.now().date() - timedelta(days=days_ago)
                # weekdays more present than weekends
                weekday = d.weekday()  # Mon=0..Sun=6
                base_present_rate = 0.92 if weekday < 5 else 0.75

                for s in students:
                    is_present = random.random() < base_present_rate
                    arrival_time = None
                    if is_present:
                        # Arrive between 08:55 and 09:10
                        minutes = random.randint(0, 15)
                        arrival_time = timezone.make_aware(datetime.combine(d, datetime.min.time())) + timedelta(
                            hours=9, minutes=minutes
                        )
                    obj, was_created = Attendance.objects.get_or_create(
                        student=s,
                        session=seed_session,
                        date=d,
                        defaults={
                            'is_present': is_present,
                            'arrival_time': arrival_time,
                            'detection_confidence': round(random.uniform(0.75, 0.98), 2) if is_present else 0.0,
                        }
                    )
                    if was_created:
                        attendance_created += 1

            # ── Engagement records: last 7 days, a few samples per day/student ──
            emotions = ['happy', 'neutral', 'confused', 'bored', 'focused']
            engagement_created = 0
            snapshot_created = 0

            now = timezone.now()
            for days_ago in range(6, -1, -1):
                d = now.date() - timedelta(days=days_ago)
                # 4 samples across the class time window
                sample_times = [9, 10, 11, 12]  # hours

                # generate student-level records
                per_sample_avgs = []
                for hour in sample_times:
                    ts = timezone.make_aware(datetime.combine(d, datetime.min.time())) + timedelta(
                        hours=hour, minutes=random.randint(0, 45)
                    )
                    # keep timestamps not in the future
                    if ts > now:
                        continue

                    present_ids = set(
                        Attendance.objects.filter(session=seed_session, date=d, is_present=True)
                        .values_list('student_id', flat=True)
                    )

                    eng_vals = []
                    att_vals = []
                    emo_dist = {e: 0 for e in emotions}

                    for s in students:
                        # If absent that day, still create a low record sometimes (rare) to keep DB realistic
                        if s.id not in present_ids and random.random() > 0.08:
                            continue

                        # Create a "student baseline" so top performers / at-risk exist
                        sid_num = int(s.student_id.replace('STU', '') or 0)
                        baseline = 72 + (sid_num % 7) * 2  # mild spread
                        noise = random.uniform(-18, 18)
                        engagement = max(5, min(98, baseline + noise))
                        attention = max(5, min(98, engagement + random.uniform(-10, 10)))

                        # Map low engagement to bored/confused more often
                        if engagement < 55:
                            emo = random.choices(['bored', 'confused', 'neutral'], weights=[0.45, 0.35, 0.20])[0]
                        elif engagement > 85:
                            emo = random.choices(['happy', 'focused', 'neutral'], weights=[0.35, 0.45, 0.20])[0]
                        else:
                            emo = random.choices(['neutral', 'focused', 'happy', 'confused'], weights=[0.45, 0.25, 0.15, 0.15])[0]

                        emo_dist[emo] = emo_dist.get(emo, 0) + 1
                        eng_vals.append(engagement)
                        att_vals.append(attention)

                        _, was_created = EngagementRecord.objects.get_or_create(
                            student=s,
                            session=seed_session,
                            timestamp=ts,
                            defaults={
                                'engagement_score': round(engagement, 1),
                                'attention_score': round(attention, 1),
                                'emotion': emo,
                                'emotion_confidence': round(random.uniform(0.6, 0.95), 2),
                                'posture_score': round(max(10, min(95, attention + random.uniform(-15, 15))), 1),
                                'eye_contact': attention > 70,
                                'face_detected': True,
                                'face_confidence': round(random.uniform(0.7, 0.98), 2),
                            }
                        )
                        if was_created:
                            engagement_created += 1

                    # class snapshot for that sample time
                    if eng_vals:
                        avg_eng = sum(eng_vals) / len(eng_vals)
                        avg_att = sum(att_vals) / len(att_vals)
                        present_count = len(eng_vals)
                        confusion_alert = (emo_dist.get('confused', 0) / max(present_count, 1)) > 0.30
                        low_eng_alert = avg_eng < 60
                        _, was_created = ClassEngagementSnapshot.objects.get_or_create(
                            session=seed_session,
                            timestamp=ts,
                            defaults={
                                'avg_engagement': round(avg_eng, 1),
                                'avg_attention': round(avg_att, 1),
                                'present_count': present_count,
                                'emotion_distribution': json.dumps(emo_dist),
                                'confusion_alert': confusion_alert,
                                'low_engagement_alert': low_eng_alert,
                            }
                        )
                        if was_created:
                            snapshot_created += 1

            return Response({
                'success': True,
                'students_created': created,
                'total_students': Student.objects.count(),
                'attendance_created': attendance_created,
                'engagement_created': engagement_created,
                'snapshot_created': snapshot_created
            })
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['POST'])
def create_test_engagement_data(request):
    """Create test engagement data to trigger alerts"""
    try:
        from django.utils import timezone
        from datetime import timedelta
        import random
        
        # Get or create active session
        active_session = ClassSession.objects.filter(status='active').first()
        if not active_session:
            # Create a test session
            teacher = Teacher.objects.first()
            if not teacher:
                return Response({'error': 'No teacher found'}, status=400)
            
            active_session = ClassSession.objects.create(
                teacher=teacher,
                class_name='CS101',
                subject='Computer Science',
                status='active'
            )
        
        # Get students
        students = Student.objects.all()[:5]  # Test with 5 students
        
        # Create engagement records with high confusion
        for student in students:
            # Create 3 records per student with confused emotion
            for i in range(3):
                EngagementRecord.objects.create(
                    student=student,
                    session=active_session,
                    timestamp=timezone.now() - timedelta(minutes=i),
                    engagement_score=random.uniform(20, 35),  # Low engagement
                    attention_score=random.uniform(15, 30),
                    emotion='confused',  # Confused emotion
                    emotion_confidence=random.uniform(0.7, 0.9),
                    face_detected=True,
                    face_confidence=random.uniform(0.8, 0.95)
                )
        
        return Response({
            'success': True,
            'message': f'Created test engagement data for {students.count()} students'
        })
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@api_view(['GET'])
def api_health(request):
    from .video_stream import get_video_stream
    stream = get_video_stream()
    return Response({
        'status': 'ok', 'timestamp': timezone.now().isoformat(),
        'database': 'connected', 'stream_running': stream.is_running,
        'students': Student.objects.count(),
        'sessions': ClassSession.objects.filter(status='active').count(),
    })


# ─── Reports Endpoints ───────────────────────────────────────────────────────

@api_view(['GET'])
def list_reports(request):
    """List all generated reports"""
    try:
        # Mock reports data - in production this would come from database
        reports = [
            {
                'id': 'rpt_001',
                'name': 'Daily Summary - CS101',
                'type': 'Daily',
                'date': '2024-03-15',
                'format': 'PDF',
                'size': '2.4 MB',
                'status': 'completed'
            },
            {
                'id': 'rpt_002',
                'name': 'Weekly Analysis - All Classes',
                'type': 'Weekly',
                'date': '2024-03-14',
                'format': 'PDF',
                'size': '5.1 MB',
                'status': 'completed'
            },
            {
                'id': 'rpt_003',
                'name': 'Monthly Performance - CS101',
                'type': 'Monthly',
                'date': '2024-03-10',
                'format': 'Excel',
                'size': '1.8 MB',
                'status': 'completed'
            },
            {
                'id': 'rpt_004',
                'name': 'Student Individual - Emma Wilson',
                'type': 'Individual',
                'date': '2024-03-12',
                'format': 'PDF',
                'size': '856 KB',
                'status': 'completed'
            },
            {
                'id': 'rpt_005',
                'name': 'Class Comparison - March',
                'type': 'Comparison',
                'date': '2024-03-08',
                'format': 'PowerPoint',
                'size': '3.2 MB',
                'status': 'completed'
            }
        ]
        
        return Response({
            'success': True,
            'reports': reports
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['POST'])
def generate_report(request):
    """Generate a new report"""
    try:
        data = request.data
        report_type = data.get('type', 'Daily')
        date_range = data.get('date_range', 'Today')
        class_name = data.get('class', 'CS101')
        format_type = data.get('format', 'PDF')
        
        # Generate report based on type
        report_data = {
            'id': f'rpt_{int(time.time())}',
            'name': f'{report_type} Summary - {class_name}',
            'type': report_type,
            'date': timezone.now().strftime('%Y-%m-%d'),
            'format': format_type,
            'size': f'{random.uniform(0.5, 5.0):.1f} MB',
            'status': 'generating'
        }
        
        # In production, this would generate actual report data
        # For now, return success
        return Response({
            'success': True,
            'message': f'Report generation started for {class_name}',
            'report': report_data
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['GET'])
def download_report(request, report_id):
    """Download a specific report"""
    try:
        # Mock report data - in production this would fetch from database
        reports = {
            'rpt_001': {'name': 'Daily Summary - CS101', 'format': 'PDF'},
            'rpt_002': {'name': 'Weekly Analysis - All Classes', 'format': 'PDF'},
            'rpt_003': {'name': 'Monthly Performance - CS101', 'format': 'Excel'},
        }
        
        if report_id in reports:
            report = reports[report_id]
            return Response({
                'success': True,
                'message': f'Downloading {report["format"]} report: {report["name"]}',
                'download_url': f'/api/reports/download/{report_id}/file/'
            })
        else:
            return Response({'error': 'Report not found'}, status=404)
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['DELETE'])
def delete_report(request, report_id):
    """Delete a specific report"""
    try:
        # Mock deletion - in production this would delete from database
        return Response({
            'success': True,
            'message': f'Report {report_id} deleted successfully'
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['GET'])
def report_templates(request):
    """Get available report templates"""
    try:
        templates = [
            {
                'id': 'tpl_001',
                'name': 'Daily Performance Template',
                'description': 'Comprehensive daily classroom performance analysis',
                'sections': ['attendance', 'engagement', 'emotions', 'participation']
            },
            {
                'id': 'tpl_002',
                'name': 'Weekly Summary Template',
                'description': 'Weekly overview of classroom metrics and trends',
                'sections': ['summary', 'trends', 'top_performers', 'alerts']
            },
            {
                'id': 'tpl_003',
                'name': 'Individual Student Report',
                'description': 'Detailed analysis for individual student performance',
                'sections': ['engagement', 'attendance', 'emotions', 'recommendations']
            }
        ]
        
        return Response({
            'success': True,
            'templates': templates
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['POST'])
def schedule_report(request):
    """Schedule a report to be generated automatically"""
    try:
        data = request.data
        schedule_time = data.get('schedule_time', '')
        report_type = data.get('type', 'Daily')
        
        # Mock scheduling - in production this would create a scheduled task
        return Response({
            'success': True,
            'message': f'Report scheduled for {schedule_time}',
            'scheduled_time': schedule_time
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)
