"""
SmartClass Monitor - Django Admin Configuration
"""
from django.contrib import admin
from .models import Teacher, Student, ClassSession, Attendance, EngagementRecord, ClassEngagementSnapshot, Alert


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'subject', 'is_active', 'created_at')
    search_fields = ('name', 'email')
    list_filter = ('is_active',)


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ('student_id', 'name', 'email', 'seat_row', 'seat_col', 'is_active', 'created_at')
    search_fields = ('student_id', 'name', 'email')
    list_filter = ('is_active',)


@admin.register(ClassSession)
class ClassSessionAdmin(admin.ModelAdmin):
    list_display = ('class_name', 'subject', 'teacher', 'start_time', 'status', 'duration_minutes')
    list_filter = ('status',)
    search_fields = ('class_name', 'subject')


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ('student', 'session', 'date', 'is_present', 'arrival_time')
    list_filter = ('is_present', 'date')
    search_fields = ('student__name',)


@admin.register(EngagementRecord)
class EngagementRecordAdmin(admin.ModelAdmin):
    list_display = ('student', 'session', 'timestamp', 'engagement_score', 'emotion', 'face_detected')
    list_filter = ('emotion', 'face_detected')
    search_fields = ('student__name',)
    ordering = ('-timestamp',)


@admin.register(ClassEngagementSnapshot)
class ClassEngagementSnapshotAdmin(admin.ModelAdmin):
    list_display = ('session', 'timestamp', 'avg_engagement', 'present_count', 'confusion_alert')
    list_filter = ('confusion_alert', 'low_engagement_alert')


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ('alert_type', 'severity', 'student', 'session', 'timestamp', 'is_resolved')
    list_filter = ('alert_type', 'severity', 'is_resolved')
    actions = ['mark_resolved']

    def mark_resolved(self, request, queryset):
        from django.utils import timezone
        queryset.update(is_resolved=True, resolved_at=timezone.now())
    mark_resolved.short_description = "Mark selected alerts as resolved"
