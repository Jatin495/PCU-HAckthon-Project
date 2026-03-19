"""
SmartClass Monitor - Engagement App URL Patterns
All /api/ routes handled here.
"""

from django.urls import path
from . import views
from . import camera

urlpatterns = [
    # Health check
    path('health/', views.api_health, name='api_health'),

    # Camera endpoints (using consolidated camera)
    path('simple_camera_feed/', camera.simple_camera_feed, name='simple_camera_feed'),
    path('start_simple_camera/', camera.start_simple_camera, name='start_simple_camera'),
    path('stop_simple_camera/', camera.stop_simple_camera, name='stop_simple_camera'),
    path('emotion_stats/', camera.get_emotion_stats, name='emotion_stats'),

    # Auth
    path('auth/login/', views.login, name='login'),
    path('auth/logout/', views.logout_view, name='logout'),

    # Dashboard
    path('dashboard/stats/', views.dashboard_stats, name='dashboard_stats'),
    path('dashboard/timeline/', views.engagement_timeline, name='engagement_timeline'),
    path('dashboard/heatmap/', views.classroom_heatmap, name='classroom_heatmap'),

    # Students
    path('students/', views.list_students, name='list_students'),
    path('students/overview/', views.students_overview, name='students_overview'),
    path('students/add/', views.add_student, name='add_student'),
    path('students/<str:student_id>/', views.student_detail, name='student_detail'),
    
    # Face Recognition
    # path('register-face/', views.register_student_face, name='register_student_face'),

    # Sessions
    path('sessions/', views.list_sessions, name='list_sessions'),
    path('sessions/start/', views.start_session, name='start_session'),
    path('sessions/<int:session_id>/end/', views.end_session, name='end_session'),
    path('sessions/<int:session_id>/report/', views.session_report, name='session_report'),

    # Live monitoring
    path('live/feed/', views.video_feed, name='video_feed'),
    path('live/data/', views.live_data, name='live_data'),
    path('live/frame/', views.stream_frame, name='stream_frame'),
    path('live/stop/', views.stop_stream_force, name='stop_stream_force'),

    # Alert System
    path('check-alert/', views.check_engagement_alert, name='check_engagement_alert'),
    path('create-test-data/', views.create_test_engagement_data, name='create_test_engagement_data'),
    path('alerts/', views.list_alerts, name='list_alerts'),
    path('alerts/<int:alert_id>/resolve/', views.resolve_alert, name='resolve_alert'),

    # Reports
    path('reports/generate/', views.generate_report, name='generate_report'),
    path('reports/list/', views.list_reports, name='list_reports'),
    path('reports/download/<str:report_id>/', views.download_report, name='download_report'),
    path('reports/delete/<str:report_id>/', views.delete_report, name='delete_report'),
    path('reports/templates/', views.report_templates, name='report_templates'),
    path('reports/schedule/', views.schedule_report, name='schedule_report'),

    # Attendance
    path('attendance/', views.attendance_report, name='attendance_report'),

    # Analytics
    path('analytics/', views.analytics_summary, name='analytics_summary'),

    # Setup / Demo
    path('setup/seed/', views.seed_demo_data, name='seed_demo_data'),

    # Database overview
    path('database/tables/', views.database_tables_overview, name='database_tables_overview'),

    # Teacher dashboard
    path('teacher/dashboard-data/', views.teacher_dashboard_data, name='teacher_dashboard_data'),
    path('teacher/syllabus/topics/add/', views.teacher_add_syllabus_topic, name='teacher_add_syllabus_topic'),
    path('teacher/syllabus/topics/<int:topic_id>/status/', views.teacher_update_topic_status, name='teacher_update_topic_status'),
    path('teacher/planner/add/', views.teacher_add_daily_topic, name='teacher_add_daily_topic'),
    path('teacher/planner/<int:plan_id>/complete/', views.teacher_complete_daily_topic, name='teacher_complete_daily_topic'),
    path('teacher/extra-lectures/schedule/', views.teacher_schedule_extra_lecture, name='teacher_schedule_extra_lecture'),
    path('teacher/feedback/add/', views.teacher_add_feedback, name='teacher_add_feedback'),
]
