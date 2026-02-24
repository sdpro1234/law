# client.py
import requests
import json
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, make_response
from flask_login import login_required, current_user
from models import db, User, Case, Review, Appointment, Message
from models import MessageRead
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import time

# Simple in-memory protections for dev: per-user last-generate timestamp and a tiny cache
# Keys: user_id -> last timestamp (float)
_last_generate_times = {}
# Cache: (user_id, hint_hash) -> (timestamp, description, used_fallback)
_generate_cache = {}


def _keyword_fallback(case_description):
    """
    Deterministic keyword-based fallback that returns a dict with
    a `recommendations` list. Is defined at module level so it can be
    referenced from multiple functions without scoping issues.
    """
    desc = (case_description or '').lower()
    suggestions = []

    def add_once(spec, reason):
        for s in suggestions:
            if s['specialization'] == spec:
                return
        suggestions.append({'specialization': spec, 'reason': reason})

    if any(k in desc for k in ['property', 'real estate', 'lease', 'title', 'mortgage']):
        add_once('Real Estate Law', 'Matter involves property or real-estate disputes.')
    if any(k in desc for k in ['contract', 'agreement', 'breach', 'terms']):
        add_once('Contract Law', 'Issue likely centers on contractual obligations.')
    if any(k in desc for k in ['divorce', 'custody', 'family']):
        add_once('Family Law', 'Family law matters such as divorce or custody.')
    if any(k in desc for k in ['fraud', 'theft', 'assault', 'crime', 'criminal']):
        add_once('Criminal Law', 'Allegations of criminal conduct may require defense.')
    if any(k in desc for k in ['employment', 'wage', 'dismiss', 'harass']):
        add_once('Employment Law', 'Employment dispute or workplace-related claim.')
    if any(k in desc for k in ['tax', 'irs', 'taxes']):
        add_once('Tax Law', 'Tax-related issues and filings.')

    # Ensure we have at least 3 recommendations
    if len(suggestions) < 3:
        add_once('Civil Litigation', 'General civil litigation expertise for disputes.')

    return {'recommendations': suggestions[:3]}

from ai_recommendation_engine import get_ai_recommendations as engine_get_ai_recommendations
from ai_recommendation_engine import get_ai_case_report_and_recommendations
from case_module import save_documents, classify_case_text, generate_questions_for_specialization


def get_ai_recommendations(case_description):
    """Delegate to the centralized `ai_recommendation_engine` module and
    return the same JSON string shape the rest of the app expects.
    """
    try:
        return engine_get_ai_recommendations(case_description)
    except Exception:
        current_app.logger.exception('AI engine failed; using keyword fallback')
        return json.dumps(_keyword_fallback(case_description))


    # (fallback function moved to module level to avoid scoping issues)

# --- Decorator for Client Access ---
def client_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'client':
            flash('Client access required.')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

client_bp = Blueprint('client', __name__)

# --- Client Routes ---

@client_bp.route('/dashboard')
@login_required
@client_required
def dashboard():
    # --- Calculate Dashboard Stats ---
    active_cases = Case.query.filter_by(client_id=current_user.id, status='accepted').count()
    upcoming_appts = Appointment.query.filter_by(user_id=current_user.id, status='confirmed').filter(Appointment.timestamp > datetime.now(timezone.utc)).count()
    
    # Simple way to count unread messages from lawyers
    all_my_cases = Case.query.filter_by(client_id=current_user.id).with_entities(Case.id).all()
    my_case_ids = [c.id for c in all_my_cases]
    unread_messages = Message.query.filter(Message.case_id.in_(my_case_ids)).filter(Message.sender_id != current_user.id).count()

    recent_cases = Case.query.filter_by(client_id=current_user.id).order_by(Case.timestamp.desc()).limit(5).all()

    # Also include a small list of available lawyers for quick discovery from the dashboard
    try:
        lawyer_query = User.query.filter_by(role='lawyer')
        # prefer verified first for the dashboard
        available_lawyers = lawyer_query.order_by(User.is_verified.desc(), User.name.asc()).limit(8).all()
        specializations = db.session.query(User.specialization).filter(User.role=='lawyer').distinct().all()
        specializations = [s[0] for s in specializations if s[0]]
    except Exception:
        available_lawyers = []
        specializations = []

    return render_template('client/user_dashboard.html', 
                         active_cases_count=active_cases,
                         upcoming_appointments_count=upcoming_appts,
                         unread_messages_count=unread_messages,
                         recent_cases=recent_cases,
                         available_lawyers=available_lawyers,
                         specializations=specializations)

@client_bp.route('/submit_case', methods=['GET', 'POST'])
@login_required
@client_required
def submit_case():
    if request.method == 'POST':
        description = request.form.get('description')
        # If user left the description blank or it's too short, try to generate one via AI
        if not description or len(description.strip()) < 30:
            hint = request.form.get('hint') or session.get('prefill_description') or ''
            try:
                gen_prompt = (
                    "You are a legal assistant. Create a clear, concise case description (2-4 sentences) suitable for submitting to a lawyer based on the notes below.\n\n"
                    f"Notes: {hint}"
                )
                gen = current_app.generate_ai_response(gen_prompt)
                if gen and not (isinstance(gen, dict) and gen.get('error')):
                    # prefer string result
                    description = gen if isinstance(gen, str) else str(gen)
                else:
                    current_app.logger.debug('AI generate_description returned error/fallback; using hint as description')
                    description = hint or description or 'Client provided minimal details.'
            except Exception:
                current_app.logger.exception('Failed to auto-generate case description; using provided text or hint')
                description = hint or description or 'Client provided minimal details.'
        ai_recommendations_json = get_ai_recommendations(description)

        # Check if the AI response is empty or invalid
        if not ai_recommendations_json or ai_recommendations_json.strip() == "":
            flash('Could not fetch AI recommendations at this time. Please try again.', 'danger')
            return redirect(url_for('client.submit_case'))

        # Remove triple backticks if present
        if ai_recommendations_json.startswith("```json"):
            ai_recommendations_json = ai_recommendations_json.replace("```json", "").replace("```", "").strip()

        try:
            ai_data = json.loads(ai_recommendations_json)
        except json.JSONDecodeError as e:
            print(f"[ERROR] Failed to decode AI response: {e}")
            print(f"[DEBUG] Raw AI Response: {ai_recommendations_json}")
            flash('AI response was invalid. Please try again later.', 'danger')
            return redirect(url_for('client.submit_case'))

        recommended_specializations = [item['specialization'] for item in ai_data.get('recommendations', [])]
        lawyers = User.query.filter(
            User.role == 'lawyer',
            User.is_verified == True,
            User.specialization.in_(recommended_specializations)
        ).all()
        session['recommendations'] = [
            {
                'id': lawyer.id,
                'name': lawyer.name,
                'specialization': lawyer.specialization,
                'experience': lawyer.experience_years,
                'location': lawyer.location,
                'score': lawyer.experience_years + (5 if lawyer.location else 0)
            } for lawyer in lawyers
        ]
        new_case = Case(description=description, category="AI-Classified", client_id=current_user.id)
        db.session.add(new_case)
        db.session.commit()

        # --- Generate AI case report and attach as a message to the case ---
        try:
            report_prompt = (
                "You are a legal assistant. Given the following client case description, produce a concise case report (3-6 sentences) that summarizes facts, likely legal issues, and suggested next steps. Respond only with text.\n\n"
                f"Case description: {description}"
            )
            ai_report = current_app.generate_ai_response(report_prompt)
            # If helper returned an error dict, fallback to using the raw description
            if not ai_report or (isinstance(ai_report, dict) and ai_report.get('error')):
                current_app.logger.warning('AI report generation failed; using description as report fallback')
                ai_report_text = f"[Report unavailable] Summary: {description}"
            else:
                ai_report_text = ai_report if isinstance(ai_report, str) else str(ai_report)
        except Exception:
            current_app.logger.exception('Failed to generate AI case report')
            ai_report_text = f"[Report generation error] Summary: {description}"

        try:
            # Attach as a system message from the client so it's visible in chat
            msg = Message(content=f"AI Case Report:\n\n{ai_report_text}", case_id=new_case.id, sender_id=current_user.id)
            db.session.add(msg)
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Failed to save AI report message')

        # --- Auto-request an appointment with the top recommended lawyer when possible ---
        try:
            selected_lawyer_id = None
            # Prefer session recommendations if present
            recs = session.get('recommendations') or []
            if recs:
                selected_lawyer_id = recs[0].get('id')

            # If not available, pick a verified lawyer matching recommended specializations
            if not selected_lawyer_id:
                if recommended_specializations:
                    lawyer = User.query.filter(User.role=='lawyer', User.is_verified==True, User.specialization.in_(recommended_specializations)).first()
                    if lawyer:
                        selected_lawyer_id = lawyer.id

            # Fallback to any verified lawyer
            if not selected_lawyer_id:
                lawyer = User.query.filter_by(role='lawyer', is_verified=True).first()
                if lawyer:
                    selected_lawyer_id = lawyer.id

            if selected_lawyer_id:
                # Assign the lawyer to the case (client implicitly chose top recommendation)
                new_case.lawyer_id = selected_lawyer_id
                # Keep case open or mark accepted depending on business rule; here we'll mark accepted
                new_case.status = 'accepted'

                # Create an appointment request 3 days from now at 10:00 as a placeholder
                appt_time = datetime.now() + timedelta(days=3)
                appt = Appointment(
                    timestamp=appt_time,
                    type='video',
                    notes='Auto-requested appointment after case submission and AI report generation',
                    user_id=current_user.id,
                    case_id=new_case.id,
                    status='requested'
                )
                db.session.add(appt)
                db.session.commit()
                flash('Case submitted. An appointment request was sent to the recommended lawyer.', 'success')
            else:
                db.session.commit()
                flash('Case submitted. No suitable lawyer found to auto-request an appointment.', 'info')
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Failed to auto-request appointment')
            flash('Case submitted, but failed to request appointment automatically.', 'warning')

        return redirect(url_for('client.dashboard'))
        
    # Allow pre-filling the description when coming from case_questions
    prefill = session.pop('prefill_description', None)
    return render_template('client/case_submit.html', prefill_description=prefill)


@client_bp.route('/api/generate_description', methods=['POST'])
@login_required
@client_required
def api_generate_description():
    """Generate a concise case description from a short hint using the AI helper.

    Request JSON: {"hint": "..."}
    Response: {"description": "...", "used_fallback": true|false}
    """
    data = request.get_json(silent=True) or {}
    hint = data.get('hint') or data.get('notes') or ''
    if not hint:
        return make_response(json.dumps({'description': '', 'used_fallback': True}), 200, {'Content-Type': 'application/json'})

    # Rate limit / debounce: allow one generation per `AI_GENERATE_RATE_LIMIT_SECONDS` per user
    try:
        user_key = getattr(current_user, 'id', None) or session.get('user_id') or 'anonymous'
    except Exception:
        user_key = 'anonymous'

    rl_seconds = current_app.config.get('AI_GENERATE_RATE_LIMIT_SECONDS', 6)
    now_ts = time.time()
    last_ts = _last_generate_times.get(user_key)
    if last_ts and (now_ts - last_ts) < rl_seconds:
        # If the same hint was recently generated, return cached result if available
        hkey = (user_key, hash(hint))
        cached = _generate_cache.get(hkey)
        if cached and (now_ts - cached[0]) < (current_app.config.get('AI_GENERATE_CACHE_SECONDS', 300)):
            return make_response(json.dumps({'description': cached[1], 'used_fallback': cached[2], 'cached': True}), 200, {'Content-Type': 'application/json'})
        # Otherwise respond with a polite backoff message (200 with used_fallback)
        return make_response(json.dumps({'description': hint, 'used_fallback': True, 'rate_limited': True}), 200, {'Content-Type': 'application/json'})

    # mark last attempt now
    _last_generate_times[user_key] = now_ts

    prompt = (
        "You are a legal assistant. Create a clear, concise case description (2-4 sentences) suitable for submitting to a lawyer based on the notes below.\n\n"
        f"Notes: {hint}"
    )

    # Allow requester to specify generation mode (e.g., 'fir')
    mode = data.get('mode') or None
    date_time = data.get('date_time') or None
    location = data.get('location') or None
    try:
        # Use the combined case_report + recommendations generator and return the report portion
        combined = get_ai_case_report_and_recommendations(hint, mode=mode, date_time=date_time, location=location)
        parsed = None
        try:
            parsed = json.loads(combined) if isinstance(combined, str) else combined
        except Exception:
            parsed = None

        if parsed and parsed.get('case_report'):
            desc_text = parsed.get('case_report')
            # cache the generated text for this user+hint
            try:
                hkey = (user_key, hash(hint))
                _generate_cache[hkey] = (now_ts, desc_text, False)
            except Exception:
                pass
            return make_response(json.dumps({'description': desc_text, 'used_fallback': False}), 200, {'Content-Type': 'application/json'})

        # fallback to hint - cache fallback as used_fallback=True
        try:
            hkey = (user_key, hash(hint))
            _generate_cache[hkey] = (now_ts, hint, True)
        except Exception:
            pass
        return make_response(json.dumps({'description': hint, 'used_fallback': True}), 200, {'Content-Type': 'application/json'})
    except Exception:
        current_app.logger.exception('AI generate_description failed')
        try:
            hkey = (user_key, hash(hint))
            _generate_cache[hkey] = (now_ts, hint, True)
        except Exception:
            pass
        return make_response(json.dumps({'description': hint, 'used_fallback': True}), 200, {'Content-Type': 'application/json'})


@client_bp.route('/api/recommendations', methods=['POST'])
@login_required
@client_required
def api_recommendations():
    """
    JSON API endpoint that returns lawyer recommendations for a given
    case description. Expected JSON payload: {"description": "..."}
    Responds with: {"recommendations": [...], "used_fallback": true|false}
    """
    data = request.get_json(silent=True) or request.form or {}
    desc = data.get('description') or data.get('case_description') or ''

    # Build prompt identical to the internal helper
    prompt = (
        "You are an expert legal assistant. Based on the following case description, "
        "return a JSON object with 3 recommended lawyer specializations and a short reason for each. "
        "Respond ONLY with JSON.\n\n"
        f"Case description: {desc}"
    )

    # Call app-level AI helper directly so we can detect failures and decide
    # whether to use the fallback deterministically.
    try:
        ai_resp = current_app.generate_ai_response(prompt)
        # If helper returned an error dict, use fallback
        if not ai_resp or (isinstance(ai_resp, dict) and ai_resp.get('error')):
            raise RuntimeError('AI helper returned error')

        # Try to extract text from typical Gemini response
        parsed = None
        try:
            if isinstance(ai_resp, str):
                parsed = json.loads(ai_resp)
            else:
                candidates = ai_resp.get('candidates')
                if candidates:
                    text = candidates[0]['content']['parts'][0]['text']
                    parsed = json.loads(text)
        except Exception:
            parsed = None

        if parsed and isinstance(parsed, dict) and parsed.get('recommendations'):
            return make_response(json.dumps({'recommendations': parsed.get('recommendations'), 'used_fallback': False}), 200, {'Content-Type': 'application/json'})
        else:
            # Fall through to deterministic fallback
            raise RuntimeError('AI parse failed')

    except Exception:
        # Deterministic fallback
        fb = _keyword_fallback(desc)
        return make_response(json.dumps({'recommendations': fb.get('recommendations', []), 'used_fallback': True}), 200, {'Content-Type': 'application/json'})


@client_bp.route('/api/classify_case', methods=['POST'])
@login_required
@client_required
def api_classify_case():
    """Accepts multipart/form-data or JSON with `description` and optional files under `documents`.
    Returns classification (recommendations) and a set of generated follow-up questions.
    """
    desc = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        desc = body.get('description')
    else:
        desc = request.form.get('description')

    # Save any uploaded documents (not required)
    files = request.files.getlist('documents') if request.files else []
    saved = []
    if files:
        try:
            saved = save_documents(files)
        except Exception:
            current_app.logger.exception('Failed to save uploaded documents')

    # Classify using the centralized engine (returns JSON-like dict)
    try:
        classification = classify_case_text(desc)
        # classification expected shape: {'recommendations': [ {specialization, reason}, ... ]}
    except Exception:
        current_app.logger.exception('Classification failed; using fallback')
        classification = {'recommendations': [{'specialization': 'Civil Litigation', 'reason': 'fallback'}]}

    # Generate follow-up questions from the top recommendation
    top = None
    recs = classification.get('recommendations') or []
    if recs:
        top = recs[0].get('specialization')

    questions = generate_questions_for_specialization(top)

    resp = {
        'recommendations': recs,
        'used_fallback': False if classification and recs else True,
        'questions': questions,
        'saved_files': saved
    }
    return make_response(json.dumps(resp), 200, {'Content-Type': 'application/json'})


@client_bp.route('/process_questions', methods=['POST'])
@login_required
@client_required
def process_questions():
    # Collect question answers and create a brief description to prefill the submit form
    q1 = request.form.get('q1')
    q2 = request.form.get('q2')
    q3 = request.form.get('q3')
    summary = 'Answers: '
    summary += f'Attempted resolution: {q1 or "unknown"}. '
    summary += f'Documents present: {q2 or "unknown"}. '
    desired = {
        'move_fence': 'Have the fence moved',
        'financial_compensation': 'Receive financial compensation',
        'formal_agreement': 'Get a formal boundary agreement'
    }.get(q3, q3 or 'Other')
    summary += f'Desired outcome: {desired}.'
    # Store in session and forward to submit_case where user can refine
    session['prefill_description'] = summary
    return redirect(url_for('client.submit_case'))

@client_bp.route('/case_questions')
@login_required
@client_required
def case_questions():
    # This would be an intermediate step to refine case details before submission
    # For now, it's a placeholder.
    return render_template('client/case_questions.html')

@client_bp.route('/recommendations/<int:case_id>')
@login_required
@client_required
def recommendations(case_id):
    recommendations = session.get('recommendations', [])
    case = Case.query.get_or_404(case_id)
    return render_template('client/lawyer_recommendations.html', recommendations=recommendations, case=case)

@client_bp.route('/lawyer_profile/<int:lawyer_id>/<int:case_id>')
@login_required
@client_required
def lawyer_profile_view(lawyer_id, case_id):
    lawyer = User.query.get_or_404(lawyer_id)
    if lawyer.role != 'lawyer':
        flash('Invalid lawyer profile.')
        return redirect(url_for('client.dashboard'))
    # Mock some reviews for display (in a real app, this would come from the DB)
    # Use transient SimpleNamespace objects and pass them as a separate context
    # variable to avoid modifying the ORM relationship on `lawyer`.
    mock_reviews = [SimpleNamespace(rating=5, comment="Excellent service!") for _ in range(3)]
    return render_template('client/lawyer_profile_view.html', lawyer=lawyer, case=db.session.get(Case, case_id), reviews=mock_reviews)

@client_bp.route('/select_lawyer/<int:case_id>/<int:lawyer_id>')
@login_required
@client_required
def select_lawyer(case_id, lawyer_id):
    case = db.session.get(Case, case_id)
    if case and case.client_id == current_user.id and case.status == 'open':
        case.lawyer_id = lawyer_id
        case.status = 'accepted'
        db.session.commit()
        flash('Lawyer selected. You can now communicate on the case page.')
    return redirect(url_for('client.dashboard'))

@client_bp.route('/booking/<int:lawyer_id>/<int:case_id>', methods=['GET', 'POST'])
@login_required
@client_required
def booking_page(lawyer_id, case_id):
    lawyer = User.query.get_or_404(lawyer_id)
    case = Case.query.get_or_404(case_id)
    if request.method == 'POST':
        new_appt = Appointment(
            timestamp=datetime.strptime(f"{request.form.get('date')} {request.form.get('time')}", "%Y-%m-%d %H:%M"),
            type=request.form.get('consultation_type'),
            notes=request.form.get('notes'),
            user_id=current_user.id,
            case_id=case.id,
            status='requested'
        )
        db.session.add(new_appt)
        db.session.commit()
        flash('Appointment request sent! You will be notified once the lawyer confirms.')
        return redirect(url_for('client.dashboard'))
    return render_template('client/booking_page.html', lawyer=lawyer, case=case)


@client_bp.route('/request_appt_from_profile/<int:lawyer_id>', methods=['GET', 'POST'])
@login_required
@client_required
def request_appt_from_profile(lawyer_id):
    """Allows a client viewing a lawyer profile to create/select a case and then book an appointment."""
    lawyer = User.query.get_or_404(lawyer_id)
    if lawyer.role != 'lawyer':
        flash('Invalid lawyer selected.')
        return redirect(url_for('client.dashboard'))

    # Load client's cases for selection
    my_cases = Case.query.filter_by(client_id=current_user.id).order_by(Case.timestamp.desc()).all()

    if request.method == 'POST':
        selected_case_id = request.form.get('case_id')
        if selected_case_id and selected_case_id != 'new':
            case = db.session.get(Case, int(selected_case_id))
            if not case or case.client_id != current_user.id:
                flash('Invalid case selection.')
                return redirect(url_for('client.request_appt_from_profile', lawyer_id=lawyer_id))
        else:
            # Create a quick case and assign the lawyer (client chose this lawyer)
            desc = request.form.get('new_case_description') or f'Appointment request with {lawyer.name or lawyer.email}'
            case = Case(description=desc, category='Quick-Appointment', client_id=current_user.id, lawyer_id=lawyer.id, status='accepted')
            db.session.add(case)
            db.session.commit()

        # Redirect to booking page with the selected/created case
        return redirect(url_for('client.booking_page', lawyer_id=lawyer.id, case_id=case.id))

    return render_template('client/request_appt.html', lawyer=lawyer, cases=my_cases)

@client_bp.route('/chat/<int:case_id>')
@login_required
@client_required
def chat_page(case_id):
    case = Case.query.get_or_404(case_id)
    if case.client_id != current_user.id:
        flash('You do not have permission to view this case.')
        return redirect(url_for('client.dashboard'))
    
    other_party = case.lawyer
    messages = Message.query.filter_by(case_id=case.id).order_by(Message.timestamp.asc()).all()
    return render_template('client/chat_page.html', case=case, other_party=other_party, messages=messages)

@client_bp.route('/send_message/<int:case_id>', methods=['POST'])
@login_required
@client_required
def send_message(case_id):
    case = Case.query.get_or_404(case_id)
    if (case.client_id != current_user.id and case.lawyer_id != current_user.id):
        return redirect(url_for('index')) # Not part of the case
        
    content = request.form.get('content')
    if content:
        message = Message(content=content, case_id=case_id, sender_id=current_user.id)
        db.session.add(message)
        db.session.commit()
    return redirect(url_for('client.chat_page', case_id=case_id))

@client_bp.route('/case_detail/<int:case_id>')
@login_required
@client_required
def case_detail(case_id):
    case = Case.query.get_or_404(case_id)
    if case.client_id != current_user.id:
        flash('You do not have permission to view this case.')
        return redirect(url_for('client.dashboard'))
    return render_template('client/case_detail.html', case=case)

@client_bp.route('/feedback/<int:case_id>', methods=['GET', 'POST'])
@login_required
@client_required
def user_feedback(case_id):
    case = Case.query.get_or_404(case_id)
    if not case or case.client_id != current_user.id or case.review or case.status != 'closed':
        flash('Invalid request or case cannot be rated.')
        return redirect(url_for('client.dashboard'))
    lawyer = case.lawyer
    if request.method == 'POST':
        rating = int(request.form.get('rating'))
        comment = request.form.get('comment')
        new_review = Review(rating=rating, comment=comment, lawyer_id=case.lawyer_id, case_id=case.id)
        db.session.add(new_review)
        db.session.commit()
        flash('Thank you for your feedback!')
        return redirect(url_for('client.dashboard'))
        
    return render_template('client/user_feedback.html', lawyer=lawyer, case=case)

@client_bp.route('/view_all_cases')
@login_required
@client_required
def view_all_cases():
    """Displays all cases for the current user."""
    user_cases = Case.query.filter_by(client_id=current_user.id).order_by(Case.timestamp.desc()).all()
    return render_template('client/view_all_cases.html', cases=user_cases)

@client_bp.route('/messages')
@login_required
@client_required
def messages():
    # Protect against rapid repeated GETs (browser prefetch/polling) by
    # serving a short-lived cached response per session+ip in-process.
    try:
        requester = request.headers.get('X-Forwarded-For', request.remote_addr) or 'unknown'
        sess_cookie_name = getattr(current_app, 'session_cookie_name', 'session')
        sess_cookie = request.cookies.get(sess_cookie_name, '-')
        key = (requester, sess_cookie)

        cache_store = getattr(current_app, 'messages_cache', {})
        entry = cache_store.get(key)
        now_ts = time.time()
        CACHE_WINDOW = 5  # seconds
        if entry and (now_ts - entry.get('ts', 0)) < CACHE_WINDOW:
            # Return cached response (already a Flask response object)
            resp = entry.get('resp')
            # Ensure short private caching to reduce client revalidation
            resp.headers['Cache-Control'] = f'private, max-age={CACHE_WINDOW}'
            return resp

        # Not cached or stale: render fresh
        user_messages = Message.query.filter_by(sender_id=current_user.id).order_by(Message.timestamp.desc()).all()
        # Fetch read flags for the current user in bulk
        msg_ids = [m.id for m in user_messages]
        reads = {}
        if msg_ids:
            read_rows = MessageRead.query.filter(MessageRead.message_id.in_(msg_ids), MessageRead.user_id==current_user.id).all()
            reads = {r.message_id: r for r in read_rows}
        rendered = render_template('client/messages.html', messages=user_messages, message_reads=reads)
        resp = make_response(rendered)
        resp.headers['Cache-Control'] = f'private, max-age={CACHE_WINDOW}'

        # Store into in-process cache
        cache_store[key] = {'ts': now_ts, 'resp': resp}
        current_app.messages_cache = cache_store
        return resp
    except Exception:
        # Fallback to default behavior on any error
        user_messages = Message.query.filter_by(sender_id=current_user.id).order_by(Message.timestamp.desc()).all()
        reads = {}
        return render_template('client/messages.html', messages=user_messages, message_reads=reads)


@client_bp.route('/mark_message_read/<int:message_id>', methods=['POST'])
@login_required
@client_required
def mark_message_read(message_id):
    msg = Message.query.get_or_404(message_id)
    # Only participants can mark messages as read (sender or recipient on the case)
    case = msg.case
    allowed = False
    if case and (case.client_id == current_user.id or case.lawyer_id == current_user.id):
        allowed = True
    if not allowed:
        flash('Permission denied.')
        return redirect(url_for('client.messages'))

    existing = MessageRead.query.filter_by(message_id=message_id, user_id=current_user.id).first()
    if not existing:
        mr = MessageRead(message_id=message_id, user_id=current_user.id)
        db.session.add(mr)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return redirect(url_for('client.messages'))


@client_bp.route('/complete_appointment/<int:appt_id>', methods=['POST'])
@login_required
@client_required
def complete_appointment(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    if appt.user_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('client.appointments'))
    if appt.status != 'confirmed':
        flash('Only confirmed appointments can be marked completed.')
        return redirect(url_for('client.appointments'))
    appt.status = 'completed'
    db.session.commit()
    # Notify lawyer via message
    try:
        msg = Message(content=f'Client marked appointment on {appt.timestamp} as completed.', case_id=appt.case_id, sender_id=current_user.id)
        db.session.add(msg)
        db.session.commit()
    except Exception:
        db.session.rollback()
    flash('Appointment marked as completed.')
    return redirect(url_for('client.appointments'))

@client_bp.route('/appointments')
@login_required
@client_required
def appointments():
    user_appointments = Appointment.query.filter_by(user_id=current_user.id).order_by(Appointment.timestamp.desc()).all()
    # Provide a list of available (verified) lawyers to show when the user has no appointments
    try:
        # Optionally allow filtering by specialization via query param `spec`
        spec_filter = request.args.get('spec')
        # Prefer showing verified lawyers first
        lawyer_query = User.query.filter_by(role='lawyer', is_verified=True)
        if spec_filter:
            lawyer_query = lawyer_query.filter(User.specialization == spec_filter)
        available_lawyers = lawyer_query.order_by(User.name.asc()).all()
        verified_only = True
        # If no verified lawyers exist, fall back to showing all registered lawyers
        if not available_lawyers:
            fallback_q = User.query.filter_by(role='lawyer')
            if spec_filter:
                fallback_q = fallback_q.filter(User.specialization == spec_filter)
            available_lawyers = fallback_q.order_by(User.is_verified.desc(), User.name.asc()).all()
            verified_only = False
        # Gather available specializations for a simple filter UI (from all lawyers)
        specializations = db.session.query(User.specialization).filter(User.role=='lawyer').distinct().all()
        specializations = [s[0] for s in specializations if s[0]]
    except Exception:
        available_lawyers = []
        specializations = []
    # Log requester details to help diagnose repeated requests (IP and User-Agent)
    try:
        requester = request.headers.get('X-Forwarded-For', request.remote_addr) or 'unknown'
        ua = request.headers.get('User-Agent', 'unknown')
        referer = request.headers.get('Referer', '-')
        sess_cookie_name = getattr(current_app, 'session_cookie_name', 'session')
        sess_cookie = request.cookies.get(sess_cookie_name, '-')
        ts = datetime.now(timezone.utc).isoformat()

        # If the request was triggered from the messages page, treat it as a
        # likely prefetch/navigation hint and do not log at all to eliminate noise.
        if referer and '/client/messages' in referer:
            # Return a short-lived private cacheable response to reduce repeated
            # requests from browser speculative prefetching while leaving the
            # page usable for real navigation.
            resp = make_response(render_template('client/appointments.html', appointments=user_appointments))
            resp.headers['Cache-Control'] = 'private, max-age=20'
            return resp
        else:
            # Suppress duplicate logs from the same IP+session within a short window
            try:
                recent_store = getattr(current_app, 'appointments_log_recent', {})
                key = (requester, sess_cookie)
                last_ts = recent_store.get(key)
                LOG_SUPPRESSION_WINDOW = 60  # seconds
                now_ts = time.time()
                if not last_ts or (now_ts - last_ts) > LOG_SUPPRESSION_WINDOW:
                    current_app.logger.info(f"Client appointments requested at {ts} by {requester} UA:{ua} Referer:{referer} Session:{sess_cookie}")
                    recent_store[key] = now_ts
                    # attach back to app for cross-request persistence in this process
                    current_app.appointments_log_recent = recent_store
                else:
                    current_app.logger.debug(f"Suppressed duplicate appointments log for {requester} (session) within {LOG_SUPPRESSION_WINDOW}s")
            except Exception:
                current_app.logger.info(f"Client appointments requested at {ts} by {requester} UA:{ua} Referer:{referer} Session:{sess_cookie}")
    except Exception:
        current_app.logger.exception("Client appointments requested (failed to read requester info)")
    return render_template('client/appointments.html', appointments=user_appointments, available_lawyers=available_lawyers, specializations=specializations, verified_only=verified_only)


@client_bp.route('/lawyer/<int:lawyer_id>')
@login_required
@client_required
def public_lawyer_profile(lawyer_id):
    """Allow clients to view a lawyer's public profile without a case context."""
    lawyer = User.query.get_or_404(lawyer_id)
    # Provide a small set of mock reviews similar to other flows
    mock_reviews = [SimpleNamespace(rating=5, comment="Excellent service!") for _ in range(3)]
    return render_template('client/lawyer_profile_view.html', lawyer=lawyer, case=None, reviews=mock_reviews)


@client_bp.route('/lawyers')
@login_required
@client_required
def lawyers():
    """List all lawyers (discovery) with optional specialization filter."""
    try:
        spec = request.args.get('spec')
        q = User.query.filter_by(role='lawyer')
        if spec:
            q = q.filter(User.specialization == spec)
        lawyers = q.order_by(User.is_verified.desc(), User.name.asc()).all()
        specializations = db.session.query(User.specialization).filter(User.role=='lawyer').distinct().all()
        specializations = [s[0] for s in specializations if s[0]]
    except Exception:
        lawyers = []
        specializations = []
        spec = None
    return render_template('client/all_lawyers.html', lawyers=lawyers, specializations=specializations, spec_filter=spec)


@client_bp.route('/cancel_appointment/<int:appt_id>', methods=['POST','GET'])
@login_required
@client_required
def cancel_appointment(appt_id):
    appt = Appointment.query.get_or_404(appt_id)
    # Only the client who created the appointment may cancel it
    if appt.user_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('client.appointments'))
    # Allow cancelling requested or confirmed appointments
    if appt.status in ('requested', 'confirmed'):
        appt.status = 'cancelled'
        db.session.commit()
        # Notify lawyer via message if case has a lawyer assigned
        try:
            if appt.case and appt.case.lawyer_id:
                msg = Message(content=f'Client cancelled the appointment scheduled on {appt.timestamp}.', case_id=appt.case_id, sender_id=current_user.id)
                db.session.add(msg)
                db.session.commit()
        except Exception:
            db.session.rollback()
        flash('Appointment cancelled.')
    else:
        flash('Appointment cannot be cancelled.')
    return redirect(url_for('client.appointments'))