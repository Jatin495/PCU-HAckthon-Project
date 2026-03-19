"""
SmartClass Monitor - REST API Views
All endpoints that the frontend HTML/JS communicates with.
"""

import json
import logging
import threading
import base64
import time
from datetime import datetime, timedelta

from django.http import StreamingHttpResponse, JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.db.models import Avg, Count, Max, Min, Q
from django.contrib.auth.hashers import make_password, check_password
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework import status
import pandas as pd

from .models import (
    Teacher, Student, ClassSession, Attendance,
    EngagementRecord, ClassEngagementSnapshot, Alert, Report,
    SyllabusTopic, DailyLectureTopic, StudentTopicProgress,
    ExtraLecturePlan, LectureFeedback,
)
from .camera import generate_face_encoding

logger = logging.getLogger(__name__)

_ENGAGEMENT_WRITE_INTERVAL_SECONDS = 5
_last_engagement_write_by_student = {}
_last_snapshot_write_by_session = {}
_engagement_write_lock = threading.Lock()
_active_session_topic_map = {}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def hash_password(password):
    return make_password(password)


def verify_and_upgrade_password(teacher, raw_password):
    """
    Verify password using Django hashers and transparently upgrade legacy
    unsalted SHA-256 hashes to Django's default hasher on successful login.
    """
    stored = str(teacher.password_hash or '')
    if not raw_password:
        return False

    # First try Django-standard password hash verification.
    if check_password(raw_password, stored):
        return True

    # Legacy compatibility path: previous versions stored SHA-256 hex digests.
    # Keep this strictly as a migration bridge and upgrade on success.
    import hashlib
    legacy_sha256 = hashlib.sha256(raw_password.encode()).hexdigest()
    if stored == legacy_sha256:
        teacher.password_hash = make_password(raw_password)
        teacher.save(update_fields=['password_hash'])
        return True

    return False

def json_response(data, status_code=200):
    return JsonResponse(data, status=status_code, safe=False)


def _mark_attendance_from_face_detections(active_session, detected_students):
    """Mark attendance as present for recognized students seen in the live feed."""
    if not active_session or not detected_students:
        return 0

    today = timezone.now().date()
    now = timezone.now()

    # Keep strongest confidence per recognized student_id in this batch.
    confidence_by_student_id = {}
    for item in detected_students:
        sid = item.get('student_id')
        if not sid:
            continue
        confidence = float(item.get('confidence') or 0)
        previous = confidence_by_student_id.get(sid, 0)
        if confidence > previous:
            confidence_by_student_id[sid] = confidence

    if not confidence_by_student_id:
        return 0

    students = Student.objects.filter(
        is_active=True,
        student_id__in=list(confidence_by_student_id.keys())
    )

    updated_count = 0
    for student in students:
        conf = confidence_by_student_id.get(student.student_id, 0.0)
        attendance, created = Attendance.objects.get_or_create(
            student=student,
            session=active_session,
            date=today,
            defaults={
                'is_present': True,
                'arrival_time': now,
                'detection_confidence': conf,
            }
        )

        if created:
            updated_count += 1
            continue

        changed = False
        if not attendance.is_present:
            attendance.is_present = True
            changed = True
        if attendance.arrival_time is None:
            attendance.arrival_time = now
            changed = True
        if conf > float(attendance.detection_confidence or 0):
            attendance.detection_confidence = conf
            changed = True

        if changed:
            attendance.save(update_fields=['is_present', 'arrival_time', 'detection_confidence'])
            updated_count += 1

    return updated_count


def _normalize_emotion(emotion):
    """Normalize detector emotion labels to EngagementRecord choices."""
    normalized = str(emotion or 'unknown').strip().lower()
    allowed = {
        'happy', 'neutral', 'sad', 'angry', 'surprise',
        'fear', 'disgust', 'confused', 'bored', 'focused', 'unknown'
    }
    return normalized if normalized in allowed else 'unknown'


def _first_value(*values):
    """Return first meaningful value, handling numpy-like arrays safely."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and value == '':
            continue

        # Handle numpy arrays and array-like objects without importing numpy.
        if hasattr(value, 'shape') and hasattr(value, 'flatten'):
            try:
                size = getattr(value, 'size', None)
                if size == 0:
                    continue
                flat = value.flatten()
                if len(flat) == 0:
                    continue
                return flat[0]
            except Exception:
                continue

        return value

    return None


def _safe_float(*values, default=0.0):
    value = _first_value(*values)
    if value is None:
        return float(default)
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_bool(*values, default=False):
    value = _first_value(*values)
    if value is None:
        return bool(default)
    try:
        return bool(value)
    except Exception:
        return bool(default)


def _persist_live_engagement_records(active_session, analysis, now):
    """
    Persist per-student engagement records and periodic class snapshots from live analysis.
    Throttles writes to reduce DB pressure while keeping timeline and reports current.
    """
    if not active_session or not analysis:
        return {'records_written': 0, 'snapshot_written': False}

    students_payload = analysis.get('students') or analysis.get('recognized_students') or []
    if not students_payload:
        return {'records_written': 0, 'snapshot_written': False}

    now_ts = now.timestamp()
    valid_student_ids = [s.get('student_id') for s in students_payload if s.get('student_id')]
    students_by_sid = {
        s.student_id: s for s in Student.objects.filter(is_active=True, student_id__in=valid_student_ids)
    }

    records_written = 0
    with _engagement_write_lock:
        for item in students_payload:
            sid = item.get('student_id')
            if not sid:
                continue

            student = students_by_sid.get(sid)
            if not student:
                continue

            student_key = f"{active_session.id}:{sid}"
            last_student_write = _last_engagement_write_by_student.get(student_key, 0)
            if (now_ts - last_student_write) < _ENGAGEMENT_WRITE_INTERVAL_SECONDS:
                continue

            emotion = _normalize_emotion(item.get('emotion'))
            emotion_conf = _safe_float(item.get('emotion_confidence'), item.get('confidence'), default=0.0)
            engagement_score = _safe_float(item.get('engagement_score'), item.get('engagement'), default=0.0)
            attention_score = _safe_float(item.get('attention_score'), default=(engagement_score * 0.9))
            posture_score = _safe_float(item.get('posture_score'), default=0.0)
            eye_contact = _safe_bool(item.get('is_looking_forward'), default=False)

            face_bbox_val = item.get('face_bbox')
            if hasattr(face_bbox_val, 'tolist'):
                try:
                    face_bbox_val = face_bbox_val.tolist()
                except Exception:
                    pass

            if isinstance(face_bbox_val, (list, tuple)):
                face_bbox = ','.join(str(int(float(v))) for v in face_bbox_val[:4])
            else:
                face_bbox = str(face_bbox_val) if face_bbox_val is not None else ''

            emotion_scores = item.get('emotion_scores')
            if not isinstance(emotion_scores, dict):
                emotion_scores = {emotion: round(emotion_conf, 3)}

            EngagementRecord.objects.create(
                student=student,
                session=active_session,
                timestamp=now,
                engagement_score=round(engagement_score, 1),
                attention_score=round(attention_score, 1),
                emotion=emotion,
                emotion_confidence=round(emotion_conf, 3),
                emotion_scores=json.dumps(emotion_scores),
                head_angle=float(item.get('head_angle') or 0.0),
                eye_contact=eye_contact,
                posture_score=round(posture_score, 1),
                is_slouching=_safe_bool(item.get('is_slouching'), default=(posture_score > 0 and posture_score < 50)),
                face_detected=True,
                face_confidence=round(_safe_float(item.get('confidence'), emotion_conf, default=0.0), 3),
                face_bbox=face_bbox,
                frame_path=str(item.get('frame_path') or ''),
            )

            _last_engagement_write_by_student[student_key] = now_ts
            records_written += 1

        snapshot_written = False
        snapshot_key = str(active_session.id)
        last_snapshot_write = _last_snapshot_write_by_session.get(snapshot_key, 0)
        if (now_ts - last_snapshot_write) >= _ENGAGEMENT_WRITE_INTERVAL_SECONDS:
            avg_eng = float(analysis.get('avg_engagement') or 0.0)
            avg_att = float(analysis.get('avg_attention') or (avg_eng * 0.9))
            present_count = int(analysis.get('present_count') or 0)
            emotion_distribution = analysis.get('emotion_distribution') or analysis.get('emotions') or {}
            if not isinstance(emotion_distribution, dict):
                emotion_distribution = {}

            confusion_ratio = 0.0
            if present_count > 0:
                confusion_count = float(emotion_distribution.get('confused', 0) or 0)
                confusion_ratio = confusion_count / present_count

            ClassEngagementSnapshot.objects.create(
                session=active_session,
                timestamp=now,
                avg_engagement=round(avg_eng, 1),
                avg_attention=round(avg_att, 1),
                present_count=present_count,
                emotion_distribution=json.dumps(emotion_distribution),
                confusion_alert=confusion_ratio > 0.30,
                low_engagement_alert=avg_eng < 60,
            )
            _last_snapshot_write_by_session[snapshot_key] = now_ts
            snapshot_written = True

    return {'records_written': records_written, 'snapshot_written': snapshot_written}


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

        if not verify_and_upgrade_password(teacher, password):
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
        present_today = Attendance.objects.filter(date=today, is_present=True).values('student').distinct().count()
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
        from django.db.models import OuterRef, Subquery
        
        # FIXED: Use Subquery to avoid N+1 queries
        today = timezone.now().date()
        
        # Subquery for latest engagement record per student
        latest_engagement = EngagementRecord.objects.filter(
            student=OuterRef('pk')
        ).order_by('-timestamp')
        
        # Prefetch today's attendance in one query
        attendance_map = {
            a.student_id: a 
            for a in Attendance.objects.filter(date=today)
        }
        
        # Single query with annotations
        students = Student.objects.filter(is_active=True).annotate(
            latest_emotion=Subquery(latest_engagement.values('emotion')[:1]),
            latest_engagement_score=Subquery(latest_engagement.values('engagement_score')[:1]),
            latest_timestamp=Subquery(latest_engagement.values('timestamp')[:1])
        )
        
        result = []
        for student in students:
            attendance = attendance_map.get(student.id)
            result.append({
                'id': student.id,
                'student_id': student.student_id,
                'name': student.name,
                'email': student.email,
                'seat_row': student.seat_row,
                'seat_col': student.seat_col,
                'face_registered': bool(student.face_encoding),
                'present_today': attendance.is_present if attendance else False,
                'current_emotion': student.latest_emotion or 'unknown',
                'current_engagement': round(student.latest_engagement_score, 1) if student.latest_engagement_score else 0,
                'avg_engagement': round(student.latest_engagement_score, 1) if student.latest_engagement_score else 0,
                'last_updated': student.latest_timestamp.isoformat() if student.latest_timestamp else None,
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

        if not face_image:
            return Response({'error': 'Face image is required for student registration'}, status=400)
        
        count = Student.objects.count() + 1
        student_id = f"STU{count:03d}"
        
        # Process face encoding if image provided
        face_encoding = None
        if face_image:
            try:
                import cv2
                import numpy as np
                
                # Read and process face image
                image_bytes = face_image.read()
                nparr = np.frombuffer(image_bytes, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                if img is not None:
                    face_roi = None

                    # Try OpenCV Haar detector first.
                    try:
                        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
                        faces = cascade.detectMultiScale(
                            gray,
                            scaleFactor=1.1,
                            minNeighbors=4,
                            minSize=(60, 60),
                        )

                        if len(faces) > 0:
                            # Select the largest detected face.
                            x, y, w, h = max(faces, key=lambda f: int(f[2]) * int(f[3]))
                            face_roi = img[y:y+h, x:x+w]
                    except Exception as detect_error:
                        logger.warning(f"OpenCV face detection failed for {name}: {detect_error}")

                    # Fallback to center crop if detector found nothing.
                    if face_roi is None or getattr(face_roi, 'size', 0) == 0:
                        logger.warning(f"No face box detected for {name}; using center crop fallback")
                        h, w = img.shape[:2]
                        center_size = max(80, min(h, w) // 2)
                        center_x, center_y = w // 2, h // 2
                        face_roi = img[
                            max(0, center_y - center_size // 2):min(h, center_y + center_size // 2),
                            max(0, center_x - center_size // 2):min(w, center_x + center_size // 2)
                        ]

                    if face_roi is not None and getattr(face_roi, 'size', 0) > 0:
                        face_encoding = generate_face_encoding(face_roi)

                    # Final fallback: encode using whole image.
                    if face_encoding is None:
                        face_encoding = generate_face_encoding(img)

                    if face_encoding:
                        logger.info(f"✅ Face registered for student {name}")
                    else:
                        logger.error(f"Failed to generate encoding for {name}")
                else:
                    logger.error(f"Could not decode face image for {name}")
                    
            except Exception as e:
                logger.error(f"Face processing error for {name}: {e}")
        
        if face_encoding is None:
            return Response({'error': 'Face registration failed. Please upload a clear student face image.'}, status=400)

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
        unit = str(data.get('unit') or '').strip()
        topic_name = str(data.get('topic_name') or data.get('topic') or '').strip()
        daily_plan_id_raw = data.get('daily_plan_id')
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

        daily_plan = None
        if daily_plan_id_raw not in (None, '', 'null'):
            try:
                daily_plan = (
                    DailyLectureTopic.objects
                    .select_related('topic')
                    .get(id=int(daily_plan_id_raw), topic__teacher=teacher)
                )
                subject = daily_plan.topic.subject
                unit = daily_plan.topic.unit
                topic_name = daily_plan.topic.topic
            except Exception as plan_error:
                logger.warning(f"Invalid daily_plan_id '{daily_plan_id_raw}' at session start: {plan_error}")
                daily_plan = None

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
                defaults={'is_present': False}
            )

        _active_session_topic_map[session.id] = {
            'daily_plan_id': daily_plan.id if daily_plan else (int(daily_plan_id_raw) if str(daily_plan_id_raw).isdigit() else None),
            'subject': subject,
            'unit': unit,
            'topic_name': topic_name,
        }

        return Response({'success': True, 'session': {
            'id': session.id, 'class_name': session.class_name,
            'subject': subject,
            'unit': unit,
            'topic_name': topic_name,
            'daily_plan_id': _active_session_topic_map[session.id].get('daily_plan_id'),
            'start_time': session.start_time.isoformat(),
            'camera_started': camera_started,
            'camera_source': str(selected_source),
        }})
    except Exception as e:
        logger.error(f"Start session error: {e}")
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def end_session(request, session_id):
    try:
        logger.info(f"🛑 Ending session {session_id}")
        
        session = ClassSession.objects.get(id=session_id)
        session.status = 'ended'
        session.end_time = timezone.now()
        session.save()

        topic_ctx = _active_session_topic_map.get(session.id, {})
        daily_plan_id = topic_ctx.get('daily_plan_id')
        if daily_plan_id:
            try:
                plan = DailyLectureTopic.objects.select_related('topic').get(id=daily_plan_id)
                if not plan.is_completed:
                    plan.is_completed = True
                    plan.completed_at = timezone.now()
                    plan.save(update_fields=['is_completed', 'completed_at'])

                topic = plan.topic
                if topic.status != 'completed':
                    topic.status = 'completed'
                    topic.checkpoint_assigned = True
                    topic.checkpoint_completion_rate = max(float(topic.checkpoint_completion_rate or 0.0), 75.0)
                    topic.save(update_fields=['status', 'checkpoint_assigned', 'checkpoint_completion_rate'])
            except Exception as topic_close_error:
                logger.warning(f"Failed to update selected daily topic on session end: {topic_close_error}")
        _active_session_topic_map.pop(session.id, None)
        
        # Stop the video stream immediately
        from .video_stream import stop_stream
        stream_stopped = stop_stream()
        
        if stream_stopped:
            logger.info(f"✅ Session {session_id} ended successfully - Stream stopped")
        else:
            logger.warning(f"⚠️ Session {session_id} ended but stream stop failed")

        auto_report = _generate_session_auto_report(session)
        
        return Response({
            'success': True, 
            'duration': session.duration_minutes,
            'stream_stopped': stream_stopped,
            'topic_updated': bool(daily_plan_id),
            'auto_report_generated': bool(auto_report and auto_report.status == 'completed'),
            'auto_report_id': auto_report.id if auto_report else None,
        })
        
        
    except ClassSession.DoesNotExist:
        logger.error(f"❌ Session {session_id} not found")
        return Response({'error': 'Session not found'}, status=404)
    except Exception as e:
        logger.error(f"❌ Error ending session {session_id}: {e}")
        return Response({'error': str(e)}, status=500)


def _generate_session_auto_report(session):
    """Create a CSV report automatically whenever a session is ended."""
    report = None
    try:
        import os
        import pandas as pd
        from django.conf import settings

        session_end = session.end_time or timezone.now()
        report = Report.objects.create(
            name=f"Session Report - {session.class_name} - {session.start_time.strftime('%Y-%m-%d %H-%M')}",
            report_type='summary',
            format='csv',
            status='generating',
            date_from=session.start_time.date(),
            date_to=session_end.date(),
        )

        engagement_qs = EngagementRecord.objects.filter(session=session).select_related('student').order_by('student__student_id', 'timestamp')
        attendance_qs = Attendance.objects.filter(session=session).select_related('student')

        attendance_by_sid = {}
        for a in attendance_qs:
            attendance_by_sid[a.student.student_id] = a

        student_rows = []
        if engagement_qs.exists():
            from collections import defaultdict

            by_sid = defaultdict(list)
            for rec in engagement_qs:
                by_sid[rec.student.student_id].append(rec)

            for sid, rows in by_sid.items():
                first = rows[0]
                student_name = first.student.name
                avg_eng = sum(float(r.engagement_score or 0.0) for r in rows) / max(len(rows), 1)
                avg_att = sum(float(r.attention_score or 0.0) for r in rows) / max(len(rows), 1)

                emotion_counts = {}
                for r in rows:
                    emotion = str(r.emotion or 'unknown')
                    emotion_counts[emotion] = emotion_counts.get(emotion, 0) + 1
                dominant_emotion = max(emotion_counts, key=emotion_counts.get) if emotion_counts else 'unknown'

                attendance = attendance_by_sid.get(sid)
                student_rows.append({
                    'Session ID': session.id,
                    'Class Name': session.class_name,
                    'Subject': session.subject,
                    'Student ID': sid,
                    'Student Name': student_name,
                    'Attendance': 'Present' if (attendance and attendance.is_present) else 'Absent',
                    'Avg Engagement Score': round(avg_eng, 1),
                    'Avg Attention Score': round(avg_att, 1),
                    'Dominant Emotion': dominant_emotion,
                    'Samples Captured': len(rows),
                    'Session Start': session.start_time.isoformat(),
                    'Session End': session_end.isoformat(),
                    'Duration (min)': session.duration_minutes,
                })
        else:
            # Still produce a session-level report row even if no detections were captured.
            student_rows.append({
                'Session ID': session.id,
                'Class Name': session.class_name,
                'Subject': session.subject,
                'Student ID': '',
                'Student Name': 'No engagement records captured',
                'Attendance': '',
                'Avg Engagement Score': 0.0,
                'Avg Attention Score': 0.0,
                'Dominant Emotion': 'unknown',
                'Samples Captured': 0,
                'Session Start': session.start_time.isoformat(),
                'Session End': session_end.isoformat(),
                'Duration (min)': session.duration_minutes,
            })

        df = pd.DataFrame(student_rows)

        reports_dir = os.path.join(settings.MEDIA_ROOT, 'reports')
        os.makedirs(reports_dir, exist_ok=True)

        filename = f"session_report_{session.id}_{report.id}.csv"
        file_path = os.path.join(reports_dir, filename)
        df.to_csv(file_path, index=False)

        report.file_path = f"reports/{filename}"
        report.status = 'completed'
        report.generated_at = timezone.now()
        report.file_size = os.path.getsize(file_path)
        report.save(update_fields=['file_path', 'status', 'generated_at', 'file_size'])
        return report
    except Exception as e:
        logger.warning(f"Auto report generation failed for session {session.id}: {e}")
        if report is not None:
            report.status = 'failed'
            report.save(update_fields=['status'])
        return None


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
@authentication_classes([])
@permission_classes([])
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
        now = timezone.now()

        # If live monitoring is running without an explicit session start,
        # create a lightweight active session so attendance can be recorded.
        if active_session is None and stream_status.get('is_running'):
            teacher = Teacher.objects.filter(is_active=True).first()
            if teacher is None:
                teacher = Teacher.objects.create(
                    name='Demo Teacher',
                    email='demo@smartclass.com',
                    password_hash=hash_password('demo123'),
                    subject='Computer Science',
                )

            active_session = ClassSession.objects.create(
                teacher=teacher,
                class_name='Live Class',
                subject='Computer Science',
                camera_source='0',
                total_students=Student.objects.filter(is_active=True).count(),
                status='active',
            )

            auto_plan = (
                DailyLectureTopic.objects
                .select_related('topic')
                .filter(topic__teacher=teacher, lecture_date=timezone.now().date())
                .order_by('id')
                .first()
            )
            _active_session_topic_map[active_session.id] = {
                'daily_plan_id': auto_plan.id if auto_plan else None,
                'subject': auto_plan.topic.subject if auto_plan else active_session.subject,
                'unit': auto_plan.topic.unit if auto_plan else '',
                'topic_name': auto_plan.topic.topic if auto_plan else '',
            }

            today = timezone.now().date()
            for student in Student.objects.filter(is_active=True):
                Attendance.objects.get_or_create(
                    student=student,
                    session=active_session,
                    date=today,
                    defaults={'is_present': False},
                )

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
        if analysis and active_session and analysis.get('recognized_students'):
            try:
                _mark_attendance_from_face_detections(active_session, analysis.get('recognized_students', []))
            except Exception as attendance_error:
                logger.warning(f"Attendance update from face detection failed: {attendance_error}")

        if analysis and active_session:
            try:
                _persist_live_engagement_records(active_session, analysis, now)
            except Exception as persist_error:
                logger.warning(f"Live engagement persistence failed: {persist_error}")

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

        # Class-level low engagement alert (persisted to DB).
        class_avg_engagement = 0
        if analysis:
            class_avg_engagement = analysis.get('class_avg_engagement', analysis.get('avg_engagement', 0)) or 0

        if class_avg_engagement < 30:
            class_alert_msg = f"Class average engagement is low ({round(class_avg_engagement, 1)}%). Immediate intervention recommended."

            # Always surface on live panel.
            alerts_data.append({
                'id': f"class_low_{int(time.time())}",
                'type': 'low_engagement',
                'severity': 'high',
                'message': class_alert_msg,
                'student_name': 'Classroom',
                'student_id': None,
                'time': now.strftime('%H:%M:%S'),
            })

            # Persist to DB if an active session exists, with short cooldown to avoid spam.
            if active_session:
                recent_duplicate = Alert.objects.filter(
                    session=active_session,
                    student__isnull=True,
                    alert_type='low_engagement',
                    is_resolved=False,
                    timestamp__gte=now - timedelta(minutes=2),
                ).exists()

                if not recent_duplicate:
                    Alert.objects.create(
                        session=active_session,
                        student=None,
                        alert_type='low_engagement',
                        severity='high',
                        message=class_alert_msg,
                    )

        # Include unresolved DB alerts so Live Alerts box always reflects persisted records.
        if active_session:
            db_alerts = Alert.objects.filter(
                session=active_session,
                is_resolved=False,
            ).order_by('-timestamp')[:10]

            for db_alert in db_alerts:
                alerts_data.append({
                    'id': f"db_{db_alert.id}",
                    'type': db_alert.alert_type,
                    'severity': db_alert.severity,
                    'message': db_alert.message,
                    'student_name': db_alert.student.name if db_alert.student else 'Classroom',
                    'student_id': db_alert.student.student_id if db_alert.student else None,
                    'time': db_alert.timestamp.strftime('%H:%M:%S'),
                })

        alerts_data.sort(key=lambda x: x.get('time', ''), reverse=True)
        alerts_data = alerts_data[:10]

        if analysis:
            # Prepare students data - SHOW ALL DETECTED STUDENTS
            students_data = []
            if analysis.get('recognized_students'):
                for i, student_data in enumerate(analysis['recognized_students']):
                    student_name = student_data.get('name') or f"Detected Face {i + 1}"
                    student_id = student_data.get('student_id')

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
                'session': {
                    'class_name': active_session.class_name if active_session else 'Live Class',
                    'subject': (_active_session_topic_map.get(active_session.id, {}) if active_session else {}).get('subject') or (active_session.subject if active_session else 'Computer Science'),
                    'unit': (_active_session_topic_map.get(active_session.id, {}) if active_session else {}).get('unit') or '',
                    'topic_name': (_active_session_topic_map.get(active_session.id, {}) if active_session else {}).get('topic_name') or '',
                    'daily_plan_id': (_active_session_topic_map.get(active_session.id, {}) if active_session else {}).get('daily_plan_id'),
                    'teacher_id': active_session.teacher.id if active_session and active_session.teacher else None,
                    'teacher_name': active_session.teacher.name if active_session and active_session.teacher else 'Teacher',
                    'start_time': active_session.start_time.isoformat() if active_session else None,
                } if active_session else None,
                'present_count': analysis.get('present_count', len([s for s in students_data if s.get('present_today', True)])),
                'avg_engagement': class_avg_engagement,
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
        total_students = Student.objects.filter(is_active=True).count()

        attendance_qs = Attendance.objects.filter(date__gte=since.date())
        attendance_total = attendance_qs.count()
        attendance_present = attendance_qs.filter(is_present=True).count()
        attendance_rate = (attendance_present / attendance_total * 100) if attendance_total > 0 else 0

        if not records.exists():
            return Response({
                'message': 'No data available yet. Start a monitoring session.',
                'days': days, 'daily_engagement': [], 'hourly_pattern': [],
                'emotion_trend': {}, 'top_students': [], 'needs_attention': [],
                'overall_avg_engagement': 0,
                'total_students': total_students,
                'attendance_rate': round(attendance_rate, 1),
                'at_risk_count': 0,
                'performance_distribution': {
                    'excellent': 0,
                    'good': 0,
                    'average': 0,
                    'below_average': 0,
                },
            })

        df = pd.DataFrame(list(records.values(
            'timestamp', 'engagement_score', 'attention_score',
            'emotion', 'posture_score', 'eye_contact',
            'student__id', 'student__name', 'student__student_id'
        )))
        df.columns = ['timestamp', 'engagement_score', 'attention_score',
                      'emotion', 'posture_score', 'eye_contact',
                      'student_pk', 'student_name', 'student_id']
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour

        # Trend based on first vs latest record in selected period (not daily buckets)
        # so short sessions within one day still produce a meaningful trend.
        df_sorted = df.sort_values('timestamp')
        first_point = float(df_sorted['engagement_score'].iloc[0]) if len(df_sorted) > 0 else 0.0
        latest_point = float(df_sorted['engagement_score'].iloc[-1]) if len(df_sorted) > 0 else 0.0
        engagement_trend = latest_point - first_point

        daily_avg = df.groupby('date').agg({'engagement_score': 'mean', 'attention_score': 'mean'}).reset_index()
        daily_engagement = [{'date': str(row['date']), 'engagement': round(row['engagement_score'], 1),
                              'attention': round(row['attention_score'], 1)} for _, row in daily_avg.iterrows()]

        hourly_avg = df.groupby('hour')['engagement_score'].mean().reset_index()
        hourly_pattern = [{'hour': f"{int(row['hour']):02d}:00", 'engagement': round(row['engagement_score'], 1)}
                          for _, row in hourly_avg.iterrows()]

        emotion_counts = df['emotion'].value_counts().to_dict()

        student_agg = (
            df.groupby(['student_pk', 'student_id', 'student_name'])
            .agg(
                engagement_score=('engagement_score', 'mean'),
                focus_score=('attention_score', 'mean'),
                participation_score=('eye_contact', 'mean'),
            )
            .reset_index()
        )

        # Attendance rate by student in selected period.
        attendance_rates = {
            row['student_id']: round(
                (float(row['present_days']) / float(row['total_days']) * 100.0) if row['total_days'] else 0.0,
                1,
            )
            for row in (
                Attendance.objects
                .filter(date__gte=since.date(), student__is_active=True)
                .values('student__student_id')
                .annotate(
                    student_id=Max('student__student_id'),
                    total_days=Count('id'),
                    present_days=Count('id', filter=Q(is_present=True)),
                )
            )
        }

        student_agg['attendance_score'] = student_agg['student_id'].map(lambda sid: attendance_rates.get(sid, 0.0))
        student_agg['participation_score'] = student_agg['participation_score'].fillna(0.0) * 100.0

        # Weighted score for ranking.
        student_agg['ranking_score'] = (
            (student_agg['engagement_score'] * 0.50)
            + (student_agg['attendance_score'] * 0.20)
            + (student_agg['focus_score'] * 0.20)
            + (student_agg['participation_score'] * 0.10)
        )
        student_avg_sorted = student_agg.sort_values('ranking_score', ascending=False)

        top_students = [{
            'student_id': row['student_id'],
            'name': row['student_name'],
            'avg_engagement': round(float(row['engagement_score']), 1),
            'attendance': round(float(row['attendance_score']), 1),
            'focus': round(float(row['focus_score']), 1),
            'participation': round(float(row['participation_score']), 1),
            'ranking_score': round(float(row['ranking_score']), 1),
        } for _, row in student_avg_sorted.head(5).iterrows()]
        at_risk_df = student_avg_sorted[student_avg_sorted['engagement_score'] < 60]
        needs_attention = [{'student_id': row['student_id'], 'name': row['student_name'],
                            'avg_engagement': round(row['engagement_score'], 1)}
                           for _, row in at_risk_df.head(5).iterrows()]

        excellent_count = int((student_agg['engagement_score'] >= 90).sum())
        good_count = int(((student_agg['engagement_score'] >= 80) & (student_agg['engagement_score'] < 90)).sum())
        average_count = int(((student_agg['engagement_score'] >= 70) & (student_agg['engagement_score'] < 80)).sum())
        below_average_count = int((student_agg['engagement_score'] < 70).sum())

        return Response({
            'days': days, 'total_records': len(df),
            'daily_engagement': daily_engagement, 'hourly_pattern': hourly_pattern,
            'emotion_trend': emotion_counts, 'top_students': top_students,
            'student_rankings': top_students,
            'needs_attention': needs_attention,
            'overall_avg_engagement': round(df['engagement_score'].mean(), 1),
            'engagement_trend': round(float(engagement_trend), 1),
            'trend_has_data': bool(len(df_sorted) > 1),
            'total_students': total_students,
            'attendance_rate': round(attendance_rate, 1),
            'at_risk_count': int(len(at_risk_df)),
            'performance_distribution': {
                'excellent': excellent_count,
                'good': good_count,
                'average': average_count,
                'below_average': below_average_count,
            },
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
        # FIXED: Query actual Report objects from DB
        reports = Report.objects.all().order_by('-created_at')
        
        report_data = []
        for report in reports:
            report_data.append({
                'id': report.id,
                'name': report.name,
                'type': report.get_report_type_display(),
                'report_type': report.report_type,
                'date': report.created_at.strftime('%Y-%m-%d'),
                'format': report.format.upper(),
                'size': f"{report.file_size / (1024*1024):.1f} MB" if report.file_size else "N/A",
                'status': report.status,
                'created_at': report.created_at.isoformat(),
                'generated_at': report.generated_at.isoformat() if report.generated_at else None
            })
        
        return Response({
            'success': True,
            'reports': report_data
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['POST'])
def generate_report(request):
    """Generate a new report"""
    try:
        import pandas as pd
        import os
        from django.conf import settings
        
        data = request.data
        report_type = data.get('type', 'engagement')
        date_range = data.get('date_range', '7')
        class_name = data.get('class', 'CS101')
        format_type = data.get('format', 'csv')
        
        # FIXED: Actually generate a CSV report using pandas from real data
        # Create report record
        report = Report.objects.create(
            name=f'{report_type.title()} Report - {class_name}',
            report_type=report_type,
            format=format_type,
            status='generating'
        )
        
        # Get date range
        days = int(date_range) if date_range.isdigit() else 7
        date_from = timezone.now().date() - timedelta(days=days)
        date_to = timezone.now().date()
        
        report.date_from = date_from
        report.date_to = date_to
        report.save()
        
        # Generate report data based on type
        if report_type == 'engagement':
            # Get engagement records
            records = EngagementRecord.objects.filter(
                timestamp__date__gte=date_from,
                timestamp__date__lte=date_to
            ).select_related('student')
            
            # Create DataFrame
            data = []
            for record in records:
                data.append({
                    'Student Name': record.student.name,
                    'Student ID': record.student.student_id,
                    'Date': record.timestamp.date(),
                    'Time': record.timestamp.time(),
                    'Engagement Score': record.engagement_score,
                    'Attention Score': record.attention_score,
                    'Emotion': record.emotion,
                    'Posture Score': record.posture_score
                })
            
            df = pd.DataFrame(data)
            
        elif report_type == 'attendance':
            # Get attendance records
            records = Attendance.objects.filter(
                date__gte=date_from,
                date__lte=date_to
            ).select_related('student', 'session')
            
            data = []
            for record in records:
                data.append({
                    'Student Name': record.student.name,
                    'Student ID': record.student.student_id,
                    'Date': record.date,
                    'Status': 'Present' if record.is_present else 'Absent',
                    'Arrival Time': record.arrival_time,
                    'Session': record.session.class_name if record.session else 'N/A',
                    'Detection Confidence': record.detection_confidence
                })
            
            df = pd.DataFrame(data)
            
        else:  # summary report
            # Combine engagement and attendance
            engagement_records = EngagementRecord.objects.filter(
                timestamp__date__gte=date_from,
                timestamp__date__lte=date_to
            ).select_related('student')
            
            data = []
            for record in engagement_records:
                data.append({
                    'Student Name': record.student.name,
                    'Student ID': record.student.student_id,
                    'Date': record.timestamp.date(),
                    'Engagement Score': record.engagement_score,
                    'Attention Score': record.attention_score,
                    'Emotion': record.emotion
                })
            
            df = pd.DataFrame(data)
        
        # Create reports directory if it doesn't exist
        reports_dir = os.path.join(settings.MEDIA_ROOT, 'reports')
        os.makedirs(reports_dir, exist_ok=True)
        
        # Generate filename
        filename = f"{report.name.replace(' ', '_').lower()}_{report.id}.{format_type}"
        file_path = os.path.join(reports_dir, filename)
        
        # Save file
        if format_type == 'csv':
            df.to_csv(file_path, index=False)
        elif format_type == 'xlsx':
            df.to_excel(file_path, index=False)
        
        # Update report record
        report.file_path = f"reports/{filename}"
        report.status = 'completed'
        report.generated_at = timezone.now()
        report.file_size = os.path.getsize(file_path)
        report.save()
        
        return Response({
            'success': True,
            'message': f'Report generated successfully: {report.name}',
            'report': {
                'id': report.id,
                'name': report.name,
                'type': report.get_report_type_display(),
                'date': report.created_at.strftime('%Y-%m-%d'),
                'format': report.format.upper(),
                'size': f"{report.file_size / (1024*1024):.1f} MB",
                'status': report.status
            }
        })
        
    except Exception as e:
        # Update report status to failed
        if 'report' in locals():
            report.status = 'failed'
            report.save()
        return Response({'error': str(e)}, status=500)

@api_view(['GET'])
def download_report(request, report_id):
    """Download a specific report"""
    try:
        from django.http import FileResponse
        from django.conf import settings
        import os
        
        # FIXED: Serve the actual saved file using Django FileResponse
        try:
            report = Report.objects.get(id=report_id)
        except Report.DoesNotExist:
            return Response({'error': 'Report not found'}, status=404)
        
        if not report.file_path or report.status != 'completed':
            return Response({'error': 'Report file not available'}, status=404)
        
        # Build full file path
        file_path = os.path.join(settings.MEDIA_ROOT, report.file_path)
        
        if not os.path.exists(file_path):
            return Response({'error': 'Report file not found on server'}, status=404)
        
        # Return file response
        response = FileResponse(
            open(file_path, 'rb'),
            as_attachment=True,
            filename=f"{report.name}.{report.format}"
        )
        
        return response
        
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@api_view(['DELETE'])
def delete_report(request, report_id):
    """Delete a specific report"""
    try:
        import os
        from django.conf import settings
        
        # FIXED: Actually delete the report from database and file system
        try:
            report = Report.objects.get(id=report_id)
        except Report.DoesNotExist:
            return Response({'error': 'Report not found'}, status=404)
        
        # Delete file if it exists
        if report.file_path:
            file_path = os.path.join(settings.MEDIA_ROOT, report.file_path)
            if os.path.exists(file_path):
                os.remove(file_path)
        
        # Delete database record
        report.delete()
        
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


# ─── Teacher Dashboard APIs ─────────────────────────────────────────────────

def _get_default_teacher():
    teacher = Teacher.objects.filter(is_active=True).first()
    if teacher:
        return teacher
    return Teacher.objects.create(
        name='Demo Teacher',
        email='demo.teacher@smartclass.com',
        password_hash=hash_password('demo123'),
        subject='General',
        is_active=True,
    )


def _get_request_teacher(request):
    teacher_id = request.session.get('teacher_id')
    if teacher_id:
        teacher = Teacher.objects.filter(id=teacher_id, is_active=True).first()
        if teacher:
            return teacher
    return _get_default_teacher()


def _serialize_topic(topic):
    return {
        'id': topic.id,
        'subject': topic.subject,
        'unit': topic.unit,
        'topic': topic.topic,
        'status': topic.status,
        'delayed': bool(topic.is_delayed),
        'plannedDate': topic.planned_date.isoformat() if topic.planned_date else None,
        'revisedDate': topic.revised_date.isoformat() if topic.revised_date else None,
        'checkpointAssigned': bool(topic.checkpoint_assigned),
        'checkpointRate': round(float(topic.checkpoint_completion_rate or 0.0), 1),
    }


def _ensure_teacher_seed_data(teacher):
    if SyllabusTopic.objects.filter(teacher=teacher).exists():
        return

    today = timezone.now().date()
    seed_topics = [
        ('Computer Science', 'Unit 1', 'Introduction to Algorithms', 'completed', 85.0),
        ('Computer Science', 'Unit 1', 'Sorting Algorithms', 'in-progress', 65.0),
        ('Computer Science', 'Unit 2', 'Data Structures', 'pending', 0.0),
    ]

    created = []
    for idx, (subject, unit, title, state, rate) in enumerate(seed_topics):
        created.append(SyllabusTopic.objects.create(
            teacher=teacher,
            subject=subject,
            unit=unit,
            topic=title,
            status=state,
            planned_date=today - timedelta(days=(3 - idx)),
            checkpoint_assigned=(state != 'pending'),
            checkpoint_completion_rate=rate,
        ))

    if len(created) > 1:
        DailyLectureTopic.objects.get_or_create(topic=created[1], lecture_date=today)

    for topic in created:
        for student in Student.objects.filter(is_active=True):
            base = 45 + ((student.id * 13 + topic.id * 11) % 46)
            StudentTopicProgress.objects.get_or_create(
                student=student,
                topic=topic,
                defaults={
                    'completion_percent': float(base),
                    'needs_extra_lecture': base < 60,
                }
            )


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def teacher_dashboard_data(request):
    try:
        teacher = _get_request_teacher(request)
        _ensure_teacher_seed_data(teacher)

        topics = list(SyllabusTopic.objects.filter(teacher=teacher).order_by('created_at'))
        today = timezone.now().date()

        planner_qs = (
            DailyLectureTopic.objects
            .filter(topic__teacher=teacher, lecture_date=today)
            .select_related('topic')
            .order_by('topic__created_at')
        )
        planner = [{
            'id': p.id,
            'topicId': p.topic_id,
            'subject': p.topic.subject,
            'topic': p.topic.topic,
            'unit': p.topic.unit,
            'done': bool(p.is_completed),
        } for p in planner_qs]

        progress_rows = {
            row['topic_id']: row
            for row in (
                StudentTopicProgress.objects
                .filter(topic__teacher=teacher)
                .values('topic_id')
                .annotate(
                    total_students=Count('id'),
                    responded_students=Count('id', filter=Q(completion_percent__gte=60)),
                )
            )
        }

        checkpoints = []
        for idx, topic in enumerate(topics, start=1):
            stats = progress_rows.get(topic.id, {})
            checkpoints.append({
                'id': topic.id,
                'topicNumber': idx,
                'topic': topic.topic,
                'checkpointAssigned': bool(topic.checkpoint_assigned),
                'checkpointRate': round(float(topic.checkpoint_completion_rate or 0.0), 1),
                'respondedStudents': int(stats.get('responded_students') or 0),
                'totalStudents': int(stats.get('total_students') or 0),
            })

        delayed = [{
            'id': t.id,
            'topic': t.topic,
            'plannedDate': t.planned_date.isoformat() if t.planned_date else None,
            'revisedDate': t.revised_date.isoformat() if t.revised_date else None,
        } for t in topics if t.is_delayed]

        lagging = [{
            'studentId': row.student.student_id,
            'name': row.student.name,
            'topicId': row.topic_id,
            'topic': row.topic.topic,
            'engagement': round(float(row.completion_percent or 0.0), 1),
        } for row in (
            StudentTopicProgress.objects
            .select_related('student', 'topic')
            .filter(topic__teacher=teacher, completion_percent__lt=60)
            .order_by('completion_percent')[:25]
        )]

        extra_by_student = {
            row['student__student_id']: int(row['count'])
            for row in (
                ExtraLecturePlan.objects
                .filter(topic__teacher=teacher)
                .values('student__student_id')
                .annotate(count=Count('id'))
            )
        }

        performance = []
        for student in Student.objects.filter(is_active=True).order_by('name'):
            att_qs = Attendance.objects.filter(student=student)
            att_total = att_qs.count()
            att_present = att_qs.filter(is_present=True).count()
            attendance_pct = (att_present / att_total * 100.0) if att_total > 0 else 0.0

            eng_avg = EngagementRecord.objects.filter(student=student).aggregate(v=Avg('engagement_score')).get('v') or 0.0
            completion_avg = (
                StudentTopicProgress.objects
                .filter(student=student, topic__teacher=teacher)
                .aggregate(v=Avg('completion_percent'))
                .get('v') or 0.0
            )

            performance.append({
                'studentId': student.student_id,
                'name': student.name,
                'engagement': round(float(eng_avg), 1),
                'attendance': round(float(attendance_pct), 1),
                'completion': round(float(completion_avg), 1),
                'extraLectures': int(extra_by_student.get(student.student_id, 0)),
            })

        feedback = [{
            'id': f.id,
            'lecture': f.lecture_title,
            'rating': round(float(f.rating or 0.0), 1),
            'comment': f.comment,
            'submittedAt': f.submitted_at.isoformat(),
        } for f in LectureFeedback.objects.order_by('-submitted_at')[:30]]

        avg_rating = LectureFeedback.objects.aggregate(v=Avg('rating')).get('v') or 0.0
        active_session = ClassSession.objects.filter(status='active').order_by('-start_time').first()

        return Response({
            'success': True,
            'session': {
                'id': active_session.id if active_session else None,
                'className': active_session.class_name if active_session else None,
                'subject': active_session.subject if active_session else None,
                'topicCount': len(topics),
                'topicNames': [t.topic for t in topics],
            },
            'topics': [_serialize_topic(t) for t in topics],
            'planner': planner,
            'checkpoints': checkpoints,
            'delayed': delayed,
            'lagging': lagging,
            'performance': performance,
            'feedback': feedback,
            'avgRating': round(float(avg_rating), 1),
        })
    except Exception as e:
        logger.error(f"Teacher dashboard data error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_add_syllabus_topic(request):
    try:
        teacher = _get_request_teacher(request)
        subject = str(request.data.get('subject') or '').strip()
        unit = str(request.data.get('unit') or '').strip()
        topic_text = str(request.data.get('topic') or '').strip()
        if not subject or not unit or not topic_text:
            return Response({'success': False, 'error': 'subject, unit and topic are required'}, status=400)

        topic = SyllabusTopic.objects.create(
            teacher=teacher,
            subject=subject,
            unit=unit,
            topic=topic_text,
            planned_date=timezone.now().date(),
        )

        for student in Student.objects.filter(is_active=True):
            StudentTopicProgress.objects.get_or_create(
                student=student,
                topic=topic,
                defaults={'completion_percent': 50.0, 'needs_extra_lecture': True},
            )

        return Response({'success': True, 'topic': _serialize_topic(topic)})
    except Exception as e:
        logger.error(f"Add syllabus topic error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_update_topic_status(request, topic_id):
    try:
        teacher = _get_request_teacher(request)
        topic = SyllabusTopic.objects.get(id=topic_id, teacher=teacher)
        new_status = str(request.data.get('status') or '').strip().lower()
        if new_status not in {'pending', 'in-progress', 'completed'}:
            return Response({'success': False, 'error': 'Invalid status'}, status=400)

        topic.status = new_status
        if new_status == 'completed':
            topic.checkpoint_assigned = True
            topic.checkpoint_completion_rate = max(float(topic.checkpoint_completion_rate or 0.0), 75.0)
            StudentTopicProgress.objects.filter(topic=topic).update(completion_percent=85.0, needs_extra_lecture=False)
        topic.save()
        return Response({'success': True, 'topic': _serialize_topic(topic)})
    except SyllabusTopic.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        logger.error(f"Update topic status error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_add_daily_topic(request):
    try:
        teacher = _get_request_teacher(request)
        topic_id = request.data.get('topic_id')
        if not topic_id:
            return Response({'success': False, 'error': 'topic_id is required'}, status=400)

        topic = SyllabusTopic.objects.get(id=topic_id, teacher=teacher)
        plan, _ = DailyLectureTopic.objects.get_or_create(topic=topic, lecture_date=timezone.now().date())
        return Response({'success': True, 'plan': {'id': plan.id, 'topicId': topic.id, 'topic': topic.topic, 'unit': topic.unit, 'done': bool(plan.is_completed)}})
    except SyllabusTopic.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        logger.error(f"Add daily topic error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_complete_daily_topic(request, plan_id):
    try:
        teacher = _get_request_teacher(request)
        plan = DailyLectureTopic.objects.select_related('topic').get(id=plan_id, topic__teacher=teacher)
        plan.is_completed = True
        plan.completed_at = timezone.now()
        plan.save(update_fields=['is_completed', 'completed_at'])

        topic = plan.topic
        topic.status = 'completed'
        topic.checkpoint_assigned = True
        topic.checkpoint_completion_rate = max(float(topic.checkpoint_completion_rate or 0.0), 75.0)
        topic.save(update_fields=['status', 'checkpoint_assigned', 'checkpoint_completion_rate'])
        StudentTopicProgress.objects.filter(topic=topic).update(completion_percent=85.0, needs_extra_lecture=False)

        return Response({'success': True, 'nextTopicLaunched': None})
    except DailyLectureTopic.DoesNotExist:
        return Response({'success': False, 'error': 'Planned topic not found'}, status=404)
    except Exception as e:
        logger.error(f"Complete daily topic error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_schedule_extra_lecture(request):
    try:
        teacher = _get_request_teacher(request)
        student_id = str(request.data.get('student_id') or '').strip()
        topic_id = request.data.get('topic_id')
        if not student_id or not topic_id:
            return Response({'success': False, 'error': 'student_id and topic_id are required'}, status=400)

        student = Student.objects.get(student_id=student_id)
        topic = SyllabusTopic.objects.get(id=topic_id, teacher=teacher)
        plan = ExtraLecturePlan.objects.create(
            student=student,
            topic=topic,
            scheduled_date=timezone.now().date() + timedelta(days=2),
        )
        StudentTopicProgress.objects.filter(student=student, topic=topic).update(needs_extra_lecture=True)
        return Response({'success': True, 'message': f'Extra lecture scheduled for {student.name}', 'planId': plan.id})
    except Student.DoesNotExist:
        return Response({'success': False, 'error': 'Student not found'}, status=404)
    except SyllabusTopic.DoesNotExist:
        return Response({'success': False, 'error': 'Topic not found'}, status=404)
    except Exception as e:
        logger.error(f"Schedule extra lecture error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['POST'])
@authentication_classes([])
@permission_classes([])
def teacher_add_feedback(request):
    try:
        lecture = str(request.data.get('lecture') or '').strip()
        comment = str(request.data.get('comment') or '').strip()
        rating = float(request.data.get('rating') or 0.0)
        if not lecture:
            return Response({'success': False, 'error': 'lecture is required'}, status=400)
        if rating <= 0 or rating > 5:
            return Response({'success': False, 'error': 'rating must be between 0 and 5'}, status=400)

        feedback = LectureFeedback.objects.create(lecture_title=lecture, comment=comment, rating=rating)
        return Response({'success': True, 'feedback': {
            'id': feedback.id,
            'lecture': feedback.lecture_title,
            'rating': round(float(feedback.rating or 0.0), 1),
            'comment': feedback.comment,
            'submittedAt': feedback.submitted_at.isoformat(),
        }})
    except Exception as e:
        logger.error(f"Add feedback error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)


@api_view(['GET'])
@authentication_classes([])
@permission_classes([])
def database_tables_overview(request):
    try:
        tables = {
            'teachers': {'count': Teacher.objects.count()},
            'students': {'count': Student.objects.count()},
            'class_sessions': {'count': ClassSession.objects.count()},
            'syllabus_topics': {'count': SyllabusTopic.objects.count()},
            'daily_lecture_topics': {'count': DailyLectureTopic.objects.count()},
            'student_topic_progress': {'count': StudentTopicProgress.objects.count()},
            'extra_lecture_plans': {'count': ExtraLecturePlan.objects.count()},
            'lecture_feedback': {'count': LectureFeedback.objects.count()},
            'attendance': {'count': Attendance.objects.count()},
            'engagement_records': {'count': EngagementRecord.objects.count()},
            'reports': {'count': Report.objects.count()},
        }
        return Response({'success': True, 'tables': tables})
    except Exception as e:
        logger.error(f"Database tables overview error: {e}")
        return Response({'success': False, 'error': str(e)}, status=500)