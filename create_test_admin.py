from app.db.session import SessionLocal
from app.db.models.user import User, Admin
from app.core.security import get_password_hash
import uuid

def create_test_admin():
    db = SessionLocal()
    email = "admin@oaklycare.com"
    password = "admin"
    full_name = "Admin"
    phone_number = "08000000000"
    # Check if user already exists
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(
            id=uuid.uuid4(),
            email=email,
            full_name=full_name,
            phone_number=phone_number,
            hashed_password=get_password_hash(password),
            user_type="patient",
            is_active=True,
            is_verified=True
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        print(f"Created user: {user.id}")
    else:
        print(f"User already exists: {user.id}")
    # Check if admin already exists
    admin = db.query(Admin).filter(Admin.user_id == user.id).first()
    if not admin:
        admin = Admin(
            user_id=user.id,
            email=user.email,
            full_name=user.full_name,
            phone_number=user.phone_number,
            permissions='{"all": true}'
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
        print(f"Created admin: {admin.id}")
    else:
        print(f"Admin already exists: {admin.id}")
    db.close()

if __name__ == "__main__":
    create_test_admin() 