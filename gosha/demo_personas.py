"""Sample CVs for the public demo feed (gosha/api/demo.py).

Synthetic: the names, schools and projects are invented, so the demo shows
the real ranking without a real person's CV. One is in Romanian on purpose
— real users upload both.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DemoPersona:
    id: str
    label: str
    summary: str
    cv: str


DEMO_PERSONAS: tuple[DemoPersona, ...] = (
    DemoPersona(
        id="python-backend",
        label="Python backend student",
        summary="3rd-year CS, FastAPI/Django, one backend internship",
        cv=(
            "Andrei Pop — Computer Science student, Technical University of "
            "Cluj-Napoca (3rd year). Backend-focused: builds APIs and data-heavy "
            "services. Skills: Python, FastAPI, Flask, Django REST Framework, "
            "SQLAlchemy, PostgreSQL, Redis, Docker, Git, Linux, pytest, REST API "
            "design, basic AWS (EC2, S3). Experience: backend intern at a small "
            "startup (summer 2025) — REST endpoints in FastAPI for an invoicing "
            "app, pytest suites that took coverage from 40% to 75%, background "
            "jobs moved to Celery with Redis. Projects: library management API "
            "(Django, PostgreSQL, Docker Compose); Telegram bot for bus schedules "
            "(asyncio, httpx); a compiler for a toy language. Romanian (native), "
            "English (C1)."
        ),
    ),
    DemoPersona(
        id="frontend-ro",
        label="Frontend student (CV in Romanian)",
        summary="2nd-year CS, React/TypeScript, written in Romanian",
        cv=(
            "Bianca Sârbu — studentă în anul 2 la Calculatoare, Universitatea "
            "Tehnică din Cluj-Napoca. Dezvoltatoare front-end, atentă la detalii "
            "și la experiența utilizatorului. Competențe: HTML, CSS, Sass, "
            "JavaScript, TypeScript, React, Angular (de bază), Tailwind CSS, "
            "consum de API-uri REST, Git, Figma, design responsive, accesibilitate "
            "web. Experiență: voluntar în departamentul IT al unei asociații "
            "studențești — am refăcut site-ul asociației în React și am "
            "implementat formulare de înscriere. Proiecte: aplicație to-do cu "
            "React și Firebase; magazin online demonstrativ în Angular; "
            "portofoliu personal cu animații CSS. Engleză (B2)."
        ),
    ),
    DemoPersona(
        id="data-ml",
        label="Data / ML student",
        summary="AI master's, NLP research, pandas/PyTorch/SQL",
        cv=(
            "Radu Toma — Master's student in Artificial Intelligence, UTCN. "
            "Interested in NLP and applied machine learning. Skills: Python, "
            "NumPy, pandas, scikit-learn, PyTorch, Hugging Face Transformers, "
            "sentence-transformers, SQL, Power BI, Jupyter, MLflow, statistics, "
            "linear algebra. Experience: research assistant in an NLP lab (2025) "
            "— fine-tuned BERT models for Romanian text classification, built "
            "evaluation pipelines. Projects: semantic search over course notes "
            "with embeddings; Kaggle regression (top 15%); thesis on "
            "retrieval-augmented generation. Romanian (native), English (C1)."
        ),
    ),
    DemoPersona(
        id="devops",
        label="DevOps / cloud student",
        summary="3rd-year CS, Docker/Kubernetes/Terraform, home-lab cluster",
        cv=(
            "Cristian Lazăr — Computer Science student, UTCN (3rd year). Likes "
            "automating infrastructure and keeping systems running. Skills: Linux "
            "administration, Bash, Python, Docker, Kubernetes (k3s), Helm, "
            "Terraform, Ansible, GitHub Actions, GitLab CI, AWS (EC2, S3, IAM), "
            "Nginx, Prometheus, Grafana, networking basics. Experience: sysadmin "
            "intern (2024) — automated user provisioning with Ansible, set up "
            "monitoring dashboards. Projects: home-lab k3s cluster with GitOps "
            "(Argo CD); CI/CD pipeline with automated deploys; AWS Certified "
            "Cloud Practitioner. Romanian (native), English (C1)."
        ),
    ),
)

BY_ID = {persona.id: persona for persona in DEMO_PERSONAS}
