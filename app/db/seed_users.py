import asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import engine 
from app.db.models.user import User, UserRole
from app.db.base import Base  # Import your Base to access metadata
from app.core.security import hash_password

async def seed_users():
    
    
    users_data = [
        {
            "email": "mwaikomo@gmail.com",
            "full_name": "Mwai Komo",
            "password": "mwai@2026_qos!",
            "role": UserRole.admin,
        },
        {
            "email": "jane.kariuki@yahoo.com",
            "full_name": "Jane Kariuki",
            "password": "jane@2026_qos!",
            "role": UserRole.sales,
        },
        {
            "email": "njerikariuki2014@gmail.com",
            "full_name": "Njeri Kariuki",
            "password": "jane@2026_qos!",
            "role": UserRole.accountant,
        },
        {
            "email": "komomary51@gmail.com",
            "full_name": "Mary Komo",
            "password": "mary@2026_qos!",
            "role": UserRole.accountant,
        },
        {
            "email": "tansimwai@gmail.com",
            "full_name": "Tansi Mwai",
            "password": "tansi@2026_qos!",
            "role": UserRole.accountant,
        },
    ]

    # Use the session factory from your session.py or create one here
    async with AsyncSession(engine) as session:
        for user_data in users_data:
            result = await session.execute(
                select(User).where(User.email == user_data["email"])
            )
            existing = result.scalar_one_or_none()

            if existing:
                print(f"{user_data['email']} already exists, skipping.")
                continue

            user = User(
                email=user_data["email"],
                full_name=user_data["full_name"],
                hashed_password=hash_password(user_data["password"]),
                role=user_data["role"],
                is_active=True,
            )

            session.add(user)

        await session.commit()

    print("Users seeded successfully.")

if __name__ == "__main__":
    asyncio.run(seed_users())