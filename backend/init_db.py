from database.database import Base as AppBase
from auth.models import Base as AuthBase
from database.database import engine

print("Creating all tables...")
AuthBase.metadata.create_all(bind=engine)
AppBase.metadata.create_all(bind=engine)
print("All tables created successfully.")
