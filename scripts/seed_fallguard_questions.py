from dotenv import load_dotenv
load_dotenv()

import os
import json
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.services.fallguard import FallGuardService

QUESTIONS = [
    {
        "question": "What is your current age?",
        "options": [
            {"text": "Less than 65", "score": 0},
            {"text": "65–74", "score": 1},
            {"text": "75–84", "score": 2},
            {"text": "85+", "score": 3}
        ],
        "order": 1
    },
    {
        "question": "Do you live alone or with others?",
        "options": [
            {"text": "With family", "score": 0},
            {"text": "With a caregiver", "score": 1},
            {"text": "Alone", "score": 2}
        ],
        "order": 2
    },
    {
        "question": "Have you fallen in the last 12 months?",
        "options": [
            {"text": "No", "score": 0},
            {"text": "Yes, 1–2 times", "score": 1},
            {"text": "Yes, 3+ times", "score": 2}
        ],
        "order": 3
    },
    {
        "question": "Do you use mobility aids regularly?",
        "options": [
            {"text": "No", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Always", "score": 2}
        ],
        "order": 4
    },
    {
        "question": "Do you feel unsteady when walking or standing?",
        "options": [
            {"text": "Never", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Frequently", "score": 2}
        ],
        "order": 5
    },
    {
        "question": "Can you stand up from a chair without needing to push yourself up with your hands?",
        "options": [
            {"text": "Yes, easily", "score": 0},
            {"text": "With difficulty", "score": 1},
            {"text": "Not at all", "score": 2}
        ],
        "order": 6
    },
    {
        "question": "Do you experience dizziness or lightheadedness?",
        "options": [
            {"text": "Never", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Frequently", "score": 2}
        ],
        "order": 7
    },
    {
        "question": "Do you have pain or stiffness in your knees, hips, or back?",
        "options": [
            {"text": "No", "score": 0},
            {"text": "Mild", "score": 1},
            {"text": "Severe", "score": 2}
        ],
        "order": 8
    },
    {
        "question": "Do you have foot pain, swelling, or wear ill-fitting shoes?",
        "options": [
            {"text": "No", "score": 0},
            {"text": "Mild discomfort", "score": 1},
            {"text": "Severe pain", "score": 2}
        ],
        "order": 9
    },
    {
        "question": "Do you have chronic conditions (e.g., Diabetes, Arthritis, Hypertension, etc.)?",
        "options": [
            {"text": "None", "score": 0},
            {"text": "One condition", "score": 1},
            {"text": "Two or more conditions", "score": 2},
            {"text": "Three or more conditions", "score": 2}
        ],
        "order": 10
    },
    {
        "question": "Have you experienced confusion, forgetfulness, or trouble concentrating in the last 6 months?",
        "options": [
            {"text": "Not at all", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Frequently", "score": 2}
        ],
        "order": 11
    },
    {
        "question": "Do you experience confusion or difficulty recognizing your surroundings at night?",
        "options": [
            {"text": "Not at all", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Frequently", "score": 2}
        ],
        "order": 12
    },
    {
        "question": "Do you have trouble seeing clearly, even with glasses?",
        "options": [
            {"text": "No difficulty", "score": 0},
            {"text": "Some difficulty", "score": 1},
            {"text": "Significant difficulty", "score": 2}
        ],
        "order": 13
    },
    {
        "question": "Do you struggle to hear conversations, even in quiet settings (e.g., at home, religious gatherings, or family meetings)?",
        "options": [
            {"text": "No", "score": 0},
            {"text": "Mild difficulty", "score": 1},
            {"text": "Severe difficulty or use of a hearing aid", "score": 2}
        ],
        "order": 14
    },
    {
        "question": "Do you have loose rugs or uneven floors in your home?",
        "options": [
            {"text": "No", "score": 0},
            {"text": "Yes", "score": 1}
        ],
        "order": 15
    },
    {
        "question": "Is your home well-lit, especially hallways and staircases?",
        "options": [
            {"text": "Well-lit", "score": 0},
            {"text": "Partially lit", "score": 1},
            {"text": "Poorly lit", "score": 1}
        ],
        "order": 16
    },
    {
        "question": "Do you frequently experience total darkness due to power outages and lack of backup power?",
        "options": [
            {"text": "Never", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Frequently", "score": 1}
        ],
        "order": 17
    },
    {
        "question": "Do you have grab bars in your bathroom and railings on stairs?",
        "options": [
            {"text": "Fully accessible", "score": 0},
            {"text": "Partially accessible", "score": 1},
            {"text": "No aids present", "score": 2}
        ],
        "order": 18
    },
    {
        "question": "Is it safe for you to walk outside your home without risk of falling due to poor road conditions, open drains, or uneven pathways?",
        "options": [
            {"text": "Yes", "score": 0},
            {"text": "No", "score": 1}
        ],
        "order": 19
    },
    {
        "question": "How often do you engage in physical activity like walking or stretching?",
        "options": [
            {"text": "Daily", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Rarely or never", "score": 1}
        ],
        "order": 20
    },
    {
        "question": "Do you take medications that make you feel drowsy or dizzy?",
        "options": [
            {"text": "Daily", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Rarely or never", "score": 1}
        ],
        "order": 21
    },
    {
        "question": "Do you have someone who regularly checks on your well-being?",
        "options": [
            {"text": "Yes, daily", "score": 0},
            {"text": "Occasionally", "score": 1},
            {"text": "Rarely or never", "score": 1}
        ],
        "order": 22
    },
    {
        "question": "Do you eat meals that include a mix of protein, vegetables, and carbohydrates at least twice daily?",
        "options": [
            {"text": "Yes", "score": 1},
            {"text": "No", "score": 0}
        ],
        "order": 23
    },
    {
        "question": "How often do you drink alcohol?",
        "options": [
            {"text": "Never", "score": 1},
            {"text": "Occasionally (1–2 times per month)", "score": 0},
            {"text": "Regularly (1–2 times per week)", "score": 0},
            {"text": "Daily or almost daily", "score": 0}
        ],
        "order": 24
    }
]

def main():
    db: Session = SessionLocal()
    service = FallGuardService(db)
    for q in QUESTIONS:
        service.create_question(q)
    print("Seeded FallGuard questions.")

if __name__ == "__main__":
    main()