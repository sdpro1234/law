import os
import json
import logging
from werkzeug.utils import secure_filename
from flask import current_app
from ai_recommendation_engine import get_ai_recommendations

ALLOWED_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.doc', '.docx', '.txt'}


def _is_allowed(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED_EXTENSIONS


def save_documents(file_storage_list):
    """Save uploaded files to the configured UPLOAD_FOLDER and return list of saved paths.
    `file_storage_list` is an iterable of Werkzeug `FileStorage` objects.
    """
    saved = []
    upload_folder = current_app.config.get('UPLOAD_FOLDER') or os.path.join('static', 'uploads')
    os.makedirs(upload_folder, exist_ok=True)

    for fs in file_storage_list:
        if not fs or not getattr(fs, 'filename', None):
            continue
        filename = secure_filename(fs.filename)
        if not filename:
            continue
        if not _is_allowed(filename):
            logging.debug('Skipping disallowed file type: %s', filename)
            continue
        dst = os.path.join(upload_folder, f"{int(current_app.loop_time() if hasattr(current_app, 'loop_time') else 0)}_{filename}")
        # fallback to a simpler naming if loop_time not available
        if not os.path.exists(os.path.dirname(dst)):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            fs.save(dst)
            saved.append(dst)
        except Exception:
            logging.exception('Failed to save uploaded file %s', filename)
    return saved


def classify_case_text(case_description: str):
    """
    Use existing AI engine (or fallback) to classify the case description.
    Returns a dict matching the previous app expectations: {"recommendations": [...]}
    """
    try:
        resp_json = get_ai_recommendations(case_description)
        # `get_ai_recommendations` returns a JSON string; parse to dict
        parsed = json.loads(resp_json) if isinstance(resp_json, str) else resp_json
        return parsed
    except Exception:
        logging.exception('AI classification failed; returning deterministic fallback')
        # create a very small fallback
        return {'recommendations': [{'specialization': 'Civil Litigation', 'reason': 'Fallback general expertise'}]}


def generate_questions_for_specialization(specialization: str):
    """Return dynamic follow-up questions based on the chosen specialization.
    This is a rule-based starter that can later be replaced with an LLM prompt generator.
    """
    spec = (specialization or '').lower()
    if 'family' in spec:
        return [
            {'id': 'marriage_date', 'question': 'When were you married (if applicable)?'},
            {'id': 'children', 'question': 'Do you have children from this relationship? Please list ages.'},
            {'id': 'separation', 'question': 'When did separation occur (if any)?'}
        ]
    if 'property' in spec or 'real estate' in spec:
        return [
            {'id': 'ownership_docs', 'question': 'Do you have title/ownership documents? Upload if available.'},
            {'id': 'dispute_start', 'question': 'When did the dispute start?'},
            {'id': 'previous_actions', 'question': 'Have you initiated any previous legal/administrative actions?'}
        ]
    if 'criminal' in spec or 'fraud' in spec or 'crime' in spec:
        return [
            {'id': 'police_report', 'question': 'Do you have a police report or FIR? If yes, upload.'},
            {'id': 'timeline', 'question': 'Provide a timeline of events leading to the incident.'},
            {'id': 'witnesses', 'question': 'Are there witnesses? Provide names and contacts.'}
        ]
    if 'employment' in spec:
        return [
            {'id': 'employer', 'question': 'Who is/was your employer (name) and what was your role?'},
            {'id': 'termination_date', 'question': 'If applicable, when were you dismissed/terminated?'},
            {'id': 'contracts', 'question': 'Do you have employment contract or payslips?'}
        ]

    # Generic fallback questions
    return [
        {'id': 'incident_date', 'question': 'When did the incident occur?'},
        {'id': 'short_summary', 'question': 'Please provide a short timeline of events.'},
        {'id': 'documents', 'question': 'Upload any supporting documents (photos, receipts, reports).'}
    ]
