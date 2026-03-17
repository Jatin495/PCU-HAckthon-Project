"""
SmartClass Monitor - Engagement App URL Patterns
All /api/ routes handled here.
"""

from django.urls import path
from . import views

urlpatterns = [
    # Health check
    path('health/', views.api_health, name='api_health'),
    
    # Auth
    path('auth/login/', views.login, name='login'),
    path('auth/logout/', views.logout_view, name='logout'),
    
    # Dashboard
    path('dashboard/stats/', views.dashboard_stats, name='dashboard_stats'),
    path('dashboard/timeline/', views.engagement_timeline, name='engagement_timeline'),
    path('dashboard/heatmap/', views.classroom_heatmap, name='classroom_heatmap'),
    
    # Students
    path('students/', views.list_students, name='list_students'),
    path('students/add/', views.add_student, name='add_student'),
    path('students/<str:student_id>/', views.student_detail, name='student_detail'),
    
    # Sessions
    path('sessions/', views.list_sessions, name='list_sessions'),
    path('sessions/start/', views.start_session, name='start_session'),
    path('sessions/<int:session_id>/end/', views.end_session, name='end_session'),
    path('sessions/<int:session_id>/report/', views.session_report, name='session_report'),
    
    # Live monitoring
    path('live/feed/', views.video_feed, name='video_feed'),
    path('live/data/', views.live_data, name='live_data'),
    path('live/frame/', views.stream_frame, name='stream_frame'),
    
    # Alerts
    path('alerts/', views.list_alerts, name='list_alerts'),
    path('alerts/<int:alert_id>/resolve/', views.resolve_alert, name='resolve_alert'),
    
    # Attendance
    path('attendance/', views.attendance_report, name='attendance_report'),
    
    # Analytics
    path('analytics/', views.analytics_summary, name='analytics_summary'),
    
    # Setup / Demo
    path('setup/seed/', views.seed_demo_data, name='seed_demo_data'),
]
