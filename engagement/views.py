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
from datetime import datetime, timedelta, date

from django.http import StreamingHttpResponse, JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.db.models import Avg, Count, Max, Min
from rest_framework.decorators import api_view
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
                'present_today': attendance.is_present if attendance else False,
                'current_emotion': latest.emotion if latest else 'unknown',
                'current_engagement': latest.engagement_score if latest else 0,
                'last_updated': latest.timestamp.isoformat() if latest else None,
            })
        return Response({'students': result, 'total': len(result)})
    except Exception as e:
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
def add_student(request):
    try:
        data = request.data
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        seat_row = data.get('seat_row', 1)
        seat_col = data.get('seat_col', 1)
        if not name:
            return Response({'error': 'Name is required'}, status=400)
        count = Student.objects.count() + 1
        student_id = f"STU{count:03d}"
        student = Student.objects.create(
            student_id=student_id, name=name, email=email,
            seat_row=seat_row, seat_col=seat_col,
        )
        return Response({'success': True, 'student': {
            'id': student.id, 'student_id': student.student_id, 'name': student.name,
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

        from .video_stream import start_stream
        cam_source = int(camera_source) if camera_source.isdigit() else 0
        started = start_stream(source=cam_source, session_id=session.id)

        today = timezone.now().date()
        for student in Student.objects.filter(is_active=True):
            Attendance.objects.get_or_create(
                student=student, session=session, date=today,
                defaults={'is_present': True, 'arrival_time': timezone.now()}
            )

        return Response({'success': True, 'session': {
            'id': session.id, 'class_name': session.class_name,
            'start_time': session.start_time.isoformat(), 'camera_started': started,
        }})
    except Exception as e:
        logger.error(f"Start session error: {e}")
        return Response({'error': str(e)}, status=500)


@csrf_exempt
@api_view(['POST'])
def end_session(request, session_id):
    try:
        session = ClassSession.objects.get(id=session_id)
        session.status = 'ended'
        session.end_time = timezone.now()
        session.save()
        from .video_stream import stop_stream
        stop_stream()
        return Response({'success': True, 'duration': session.duration_minutes})
    except ClassSession.DoesNotExist:
        return Response({'error': 'Session not found'}, status=404)
    except Exception as e:
        return Response({'error': str(e)}, status=500)


# ─── Live Monitoring Endpoints ────────────────────────────────────────────────

@api_view(['GET'])
def video_feed(request):
    try:
        from .video_stream import get_video_stream, generate_mjpeg_frames
        stream = get_video_stream()
        if not stream.is_running:
            return HttpResponse("Video stream not started.", status=503)
        response = StreamingHttpResponse(
            generate_mjpeg_frames(),
            content_type='multipart/x-mixed-replace; boundary=frame'
        )
        response['Cache-Control'] = 'no-cache'
        return response
    except Exception as e:
        return HttpResponse(f"Stream error: {e}", status=500)


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

        recent_alerts = Alert.objects.filter(
            is_resolved=False,
            timestamp__gte=timezone.now() - timedelta(hours=1)
        ).order_by('-timestamp')[:10]
        alerts_data = [{
            'id': a.id, 'type': a.alert_type, 'severity': a.severity,
            'message': a.message,
            'student_name': a.student.name if a.student else 'Class',
            'time': a.timestamp.strftime('%H:%M:%S'),
        } for a in recent_alerts]

        if analysis:
            return Response({
                'stream_active': stream_status['is_running'],
                'fps': stream_status['fps'],
                'session_id': active_session.id if active_session else None,
                'present_count': analysis['present_count'],
                'avg_engagement': analysis['class_avg_engagement'],
                'emotion_distribution': analysis['emotion_distribution'],
                'students': analysis['students'][:12],
                'timeline': timeline,
                'alerts': alerts_data,
                'timestamp': analysis['timestamp'],
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

@api_view(['GET'])
def list_alerts(request):
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
        return Response({'success': True, 'students_created': created, 'total_students': Student.objects.count()})
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
