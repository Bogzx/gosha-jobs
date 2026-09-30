"""Seed a local dev database with realistic data for manual testing.

Usage:
    python scripts/seed_dev.py [sqlite+aiosqlite:///data/dev.db]

Creates one user (id printed at the end — use with /api/v1/auth/debug-login),
a CV on disk, and ~12 jobs with embeddings crafted so the feed shows a
spread of match scores.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

# Runnable as `python scripts/<name>.py` from anywhere: that puts scripts/
# on sys.path, not the repo root, so `import gosha` would fail.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gosha import database  # noqa: E402
from gosha.cover_letter import save_cv  # noqa: E402
from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes  # noqa: E402
from gosha.models import Job, Subscription, User  # noqa: E402

CV_TEXT = """Bogdan Example — Computer Science student, Cluj-Napoca
Skills: Python, FastAPI, React, TypeScript, Docker, PostgreSQL, Git
Projects: Discord job-hunting bot (Python, SQLAlchemy), e-commerce site (React)
Experience: Teaching assistant, algorithms; hackathon finalist x2
Education: BSc Computer Science, UBB Cluj (2024-2027)
Languages: Romanian (native), English (C1)"""

JOBS = [
    ("Python Developer Intern", "Accesa", "Cluj-Napoca, Romania", 0.95,
     "Join our backend team. Python, FastAPI, PostgreSQL, Docker. Mentorship included.", "indeed", 900, 1400),
    ("React Frontend Intern", "NTT Data", "Cluj-Napoca, Romania", 0.9,
     "Build UIs with React and TypeScript. Figma handoffs, design systems, Git workflow.", "linkedin", 800, 1200),
    ("Junior Full Stack Developer", "Betfair Romania", "Cluj-Napoca, Romania", 0.85,
     "Python + React stack, CI/CD with Docker, PostgreSQL. 1+ year of projects welcome.", "indeed", 1500, 2200),
    ("Software Engineer Intern", "UiPath", "Bucharest, Romania", 0.8,
     "Automation platform internship. C#, Python scripting, REST APIs.", "linkedin", 1000, 1500),
    ("DevOps Trainee", "Endava", "Iasi, Romania", 0.7,
     "Linux, Docker, Kubernetes basics, CI pipelines. Great for infrastructure-curious grads.", "glassdoor", None, None),
    ("Data Analyst Intern", "Bosch", "Cluj-Napoca, Romania", 0.65,
     "SQL, Python pandas, PowerBI dashboards for manufacturing analytics.", "indeed", 850, 1100),
    ("QA Automation Intern", "Cognizant Softvision", "Remote", 0.6,
     "Selenium, Python test automation, API testing with Postman.", "linkedin", None, None),
    ("Junior Java Developer", "Luxoft", "Bucharest, Romania", 0.5,
     "Java 17, Spring Boot microservices, banking domain.", "indeed", 1800, 2500),
    ("Embedded C Intern", "Continental", "Timisoara, Romania", 0.4,
     "C, microcontrollers, automotive protocols (CAN).", "glassdoor", 900, 1200),
    ("Marketing Data Assistant", "eMAG", "Bucharest, Romania", 0.3,
     "Excel, campaign reporting, some SQL a plus.", "indeed", None, None),
    ("Accountant Junior", "KPMG", "Cluj-Napoca, Romania", 0.15,
     "Bookkeeping, invoicing, Romanian fiscal legislation.", "linkedin", 1100, 1400),
    ("Senior Platform Architect", "Adobe", "Bucharest, Romania", 0.45,
     "10+ years experience. Distributed systems, Java, AWS at scale.", "linkedin", 5000, 7000),
]


async def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "sqlite+aiosqlite:///data/dev.db"
    await database.init_db(url)
    now = datetime.now(timezone.utc)

    rng = np.random.default_rng(42)
    user_vec = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
    user_vec /= np.linalg.norm(user_vec)

    async with database.get_session() as session:
        user = User(
            discord_user_id=111000111,
            username="gosha-dev",
            in_guild=True,
            cv_embedding=vec_to_bytes(user_vec),
        )
        session.add(user)
        await session.flush()

        save_cv(user.id, CV_TEXT)

        sub = Subscription(user_id=user.id, name="Cluj internships", max_age_days=14)
        sub.keywords = ["computer science internship"]
        sub.locations = ["cluj"]
        sub.experience_levels = ["intern", "junior"]
        session.add(sub)

        for i, (title, company, location, similarity, desc, source, smin, smax) in enumerate(JOBS):
            noise = rng.normal(size=EMBEDDING_DIM).astype(np.float32)
            noise -= (noise @ user_vec) * user_vec  # orthogonalize
            noise /= np.linalg.norm(noise)
            vec = similarity * user_vec + np.sqrt(1 - similarity**2) * noise

            job = Job(
                url=f"https://example.com/jobs/{i}",
                title=title,
                company=company,
                location=location,
                description=desc + "\n\n" + "Responsibilities include working with the team, code reviews, and shipping features. " * 3,
                source=source,
                salary_min=smin,
                salary_max=smax,
                salary_currency="EUR" if smin else None,
                first_seen_at=now - timedelta(days=i % 9, hours=i),
                last_seen_at=now,
                embedding=vec_to_bytes(vec.astype(np.float32)),
            )
            session.add(job)

        await session.commit()
        print(f"Seeded {len(JOBS)} jobs. Dev user id: {user.id}")
        print(f"Login: http://localhost:8000/api/v1/auth/debug-login?uid={user.id}")


if __name__ == "__main__":
    asyncio.run(main())
