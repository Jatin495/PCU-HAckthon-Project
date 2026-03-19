"""
SmartClass Monitor - Database Models
Tables: Teacher, Student, Session, EngagementRecord, EmotionRecord, Alert, Attendance
"""

from django.db import models
from django.utils import timezone
import json


class Teacher(models.Model):
    """Teacher/User accounts"""
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    password_hash = models.CharField(max_length=256)
    subject = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.email})"

    class Meta:
        db_table = 'teachers'


class Student(models.Model):
    """Student profiles"""
    student_id = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(blank=True, default='')
    seat_row = models.IntegerField(default=1)
    seat_col = models.IntegerField(default=1)
    face_encoding = models.TextField(blank=True, null=True)  # JSON encoded face embedding
    photo = models.ImageField(upload_to='students/', blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.student_id} - {self.name}"

    class Meta:
        db_table = 'students'


class ClassSession(models.Model):
    """Class sessions tracking"""
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('ended', 'Ended'),
        ('paused', 'Paused'),
    ]
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='sessions')
    class_name = models.CharField(max_length=100)
    subject = models.CharField(max_length=100)
    start_time = models.DateTimeField(default=timezone.now)
    end_time = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    camera_source = models.CharField(max_length=50, default='0')
    total_students = models.IntegerField(default=0)

    def __str__(self):
        return f"{self.class_name} - {self.start_time.strftime('%Y-%m-%d %H:%M')}"

    @property
    def duration_minutes(self):
        if self.end_time:
            delta = self.end_time - self.start_time
            return int(delta.total_seconds() / 60)
        elif self.status == 'active':
            delta = timezone.now() - self.start_time
            return int(delta.total_seconds() / 60)
        return 0

    class Meta:
        db_table = 'class_sessions'
        ordering = ['-start_time']


class Attendance(models.Model):
    """Daily attendance records"""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='attendance_records')
    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='attendance')
    date = models.DateField(default=timezone.now)
    is_present = models.BooleanField(default=False)
    arrival_time = models.DateTimeField(null=True, blank=True)
    departure_time = models.DateTimeField(null=True, blank=True)
    detection_confidence = models.FloatField(default=0.0)

    def __str__(self):
        status = "Present" if self.is_present else "Absent"
        return f"{self.student.name} - {self.date} - {status}"

    class Meta:
        db_table = 'attendance'
        unique_together = ['student', 'session', 'date']


class EngagementRecord(models.Model):
    """Per-student engagement scores recorded every 5 seconds"""
    EMOTION_CHOICES = [
        ('happy', 'Happy'),
        ('neutral', 'Neutral'),
        ('sad', 'Sad'),
        ('angry', 'Angry'),
        ('surprise', 'Surprise'),
        ('fear', 'Fear'),
        ('disgust', 'Disgust'),
        ('confused', 'Confused'),
        ('bored', 'Bored'),
        ('focused', 'Focused'),
        ('unknown', 'Unknown'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='engagement_records')
    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='engagement_records')
    timestamp = models.DateTimeField(default=timezone.now)
    
    # Engagement metrics
    engagement_score = models.FloatField(default=0.0)  # 0-100
    attention_score = models.FloatField(default=0.0)   # 0-100
    
    # Emotion detection
    emotion = models.CharField(max_length=20, choices=EMOTION_CHOICES, default='unknown')
    emotion_confidence = models.FloatField(default=0.0)
    emotion_scores = models.TextField(blank=True, default='{}')  # JSON: all emotion probabilities
    
    # Posture analysis (MediaPipe)
    head_angle = models.FloatField(default=0.0)        # Head tilt angle
    eye_contact = models.BooleanField(default=False)   # Looking at board/camera
    posture_score = models.FloatField(default=0.0)     # 0-100
    is_slouching = models.BooleanField(default=False)
    
    # Face detection data
    face_detected = models.BooleanField(default=False)
    face_confidence = models.FloatField(default=0.0)
    face_bbox = models.CharField(max_length=100, blank=True, default='')  # x,y,w,h
    
    # Frame reference
    frame_path = models.CharField(max_length=255, blank=True, default='')

    def __str__(self):
        return f"{self.student.name} - {self.timestamp} - Eng:{self.engagement_score:.0f}%"

    def get_emotion_scores(self):
        try:
            return json.loads(self.emotion_scores)
        except:
            return {}

    class Meta:
        db_table = 'engagement_records'
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['student', 'session']),
            models.Index(fields=['timestamp']),
        ]


class ClassEngagementSnapshot(models.Model):
    """Class-wide engagement snapshot every 5 seconds"""
    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='snapshots')
    timestamp = models.DateTimeField(default=timezone.now)
    
    # Aggregate metrics
    avg_engagement = models.FloatField(default=0.0)
    avg_attention = models.FloatField(default=0.0)
    present_count = models.IntegerField(default=0)
    
    # Emotion distribution (JSON)
    emotion_distribution = models.TextField(default='{}')
    
    # Alert flags
    confusion_alert = models.BooleanField(default=False)  # >30% confused
    low_engagement_alert = models.BooleanField(default=False)

    def get_emotion_distribution(self):
        try:
            return json.loads(self.emotion_distribution)
        except:
            return {}

    class Meta:
        db_table = 'class_snapshots'
        ordering = ['-timestamp']


class Alert(models.Model):
    """Alerts for teacher attention"""
    ALERT_TYPES = [
        ('low_engagement', 'Low Engagement'),
        ('confused', 'Confused'),
        ('distracted', 'Distracted'),
        ('absent', 'Absent'),
        ('class_confusion', 'Class Confusion'),
        ('bored', 'Bored'),
    ]
    SEVERITY_CHOICES = [
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    ]

    session = models.ForeignKey(ClassSession, on_delete=models.CASCADE, related_name='alerts')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='alerts', null=True, blank=True)
    alert_type = models.CharField(max_length=30, choices=ALERT_TYPES)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default='medium')
    message = models.TextField()
    timestamp = models.DateTimeField(default=timezone.now)
    is_resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        student_name = self.student.name if self.student else "Class"
        return f"{self.alert_type} - {student_name} - {self.timestamp}"

    class Meta:
        db_table = 'alerts'
        ordering = ['-timestamp']


class Report(models.Model):
    """Generated reports for analytics and insights"""
    REPORT_TYPES = [
        ('engagement', 'Engagement Report'),
        ('attendance', 'Attendance Report'),
        ('performance', 'Performance Report'),
        ('emotion', 'Emotion Analysis'),
        ('summary', 'Summary Report'),
    ]
    
    FORMATS = [
        ('csv', 'CSV'),
        ('pdf', 'PDF'),
        ('xlsx', 'Excel'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('generating', 'Generating'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    name = models.CharField(max_length=200)
    report_type = models.CharField(max_length=20, choices=REPORT_TYPES)
    format = models.CharField(max_length=5, choices=FORMATS, default='csv')
    file_path = models.CharField(max_length=500, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    generated_at = models.DateTimeField(null=True, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)  # in bytes
    
    # Report parameters
    date_from = models.DateField(null=True, blank=True)
    date_to = models.DateField(null=True, blank=True)
    student_ids = models.TextField(null=True, blank=True)  # JSON string of student IDs
    
    def __str__(self):
        return f"{self.name} - {self.get_report_type_display()}"
    
    class Meta:
        db_table = 'reports'
        ordering = ['-created_at']
