"""
Scheduler logic to check for upcoming meetings.
"""
from datetime import datetime, timedelta
from src.database import DatabaseManager

class MeetingScheduler:
    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def get_due_meeting(self):
        """Checks for meetings that are due to start within the next minute."""
        now = datetime.now()
        meetings = self.db.get_all_meetings()
        
        for m in meetings:
            if not m.is_enabled or m.status != "Waiting":
                continue
                
            start_time = datetime.fromisoformat(m.start_time)
            
            # Check if it's time to start (or slightly past due)
            if now >= start_time and now <= (start_time + timedelta(minutes=5)):
                return m
        return None
