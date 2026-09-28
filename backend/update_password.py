from database.database import get_db
from auth.service import AuthService

db = next(get_db())

AuthService.reset_user_password(db, "admin", "admin123")
