from flask import Flask, render_template, render_template_string, request, redirect, url_for, flash, g, abort, make_response
import os
from flask_login import LoginManager, login_required, current_user
from models import db, User, AccessLog, RateLimitLog
import time
import random
import json
from datetime import datetime, UTC
from auth import auth_bp
from admin import admin_bp
from client import client_bp
from lawyer import lawyer_bp
from judge import judge_bp


def create_app():
    app = Flask(__name__)
    # AI helper placed here (clean implementation)
    def generate_ai_response(prompt):
        """Use the installed Gemini Python SDK to generate content.

        Returns a string on success or a dict {'error': msg} on failure.
        """
        now_ts = time.time()
        failures = getattr(app, 'ai_failure_count', 0)
        last_fail_ts = getattr(app, 'ai_last_failure_ts', 0)
        threshold = app.config.get('AI_CIRCUIT_BREAK_THRESHOLD', 3)
        cooldown = app.config.get('AI_CIRCUIT_BREAK_COOLDOWN', 300)
        if failures >= threshold and (now_ts - last_fail_ts) < cooldown:
            app.logger.warning('AI circuit open - skipping external call and returning error/fallback')
            return {"error": "AI temporarily disabled due to repeated failures. Using fallback."}

        cache_ttl = app.config.get('AI_CACHE_TTL', 300)
        cache = getattr(app, 'ai_cache', None)
        if cache is None:
            app.ai_cache = {}
            cache = app.ai_cache

        cached = cache.get(prompt)
        if cached:
            text, ts = cached
            if now_ts - ts < cache_ttl:
                app.logger.debug('AI response served from cache for prompt')
                return text
            else:
                del cache[prompt]

        try:
            try:
                from google import genai
            except Exception:
                try:
                    import google.generativeai as genai
                except Exception:
                    genai = None

            if genai is None:
                app.logger.warning('No Gemini SDK available (google-genai not installed)')
                app.ai_failure_count = getattr(app, 'ai_failure_count', 0) + 1
                app.ai_last_failure_ts = time.time()
                return {"error": "AI SDK not installed. Using fallback."}

            # Build model candidate list
            cfg_model = app.config.get('GEMINI_MODEL')
            candidates = [cfg_model] if cfg_model else []
            candidates.extend(['models/gemini-1.5-flash-latest', 'models/gemini-1.5-flash', 'models/gemini-1.5'])

            # Use client-style API when available
            if hasattr(genai, 'Client'):
                # Prefer explicit config, fallback to GEMINI_API_KEY env var
                api_key = app.config.get('AI_API_KEY') or os.environ.get('GEMINI_API_KEY')
                if not api_key or not str(api_key).strip():
                    app.logger.warning('Gemini SDK available but no API key configured; using fallback')
                    app.ai_failure_count = getattr(app, 'ai_failure_count', 0) + 1
                    app.ai_last_failure_ts = time.time()
                    return {"error": "AI API key not configured. Using fallback."}
                client = genai.Client(api_key=api_key)
                attempted = []
                model_list = []
                for mname in candidates:
                    if not mname:
                        continue
                    try:
                        attempted.append(mname)
                        try:
                            resp = client.models.generate_content(model=mname, contents=prompt)
                        except TypeError:
                            resp = client.models.generate_content(model=mname, content=prompt)
                        ai_text = getattr(resp, 'text', None) or (resp.output and resp.output[0].content[0].text if getattr(resp, 'output', None) else None)
                        if ai_text:
                            cache[prompt] = (ai_text, time.time())
                            app.ai_failure_count = 0
                            app.ai_last_failure_ts = 0
                            app.logger.info('AI call succeeded with model %s', mname)
                            return ai_text
                    except Exception:
                        app.logger.debug('Attempt with model %s failed', mname)

                # If candidates failed, try listing models from the client
                try:
                    if hasattr(client.models, 'list'):
                        res = client.models.list()
                        iterable = getattr(res, 'data', None) or res
                        for it in iterable:
                            mn = getattr(it, 'name', None) or getattr(it, 'id', None) or getattr(it, 'model', None)
                            if mn and mn not in attempted:
                                model_list.append(mn)
                    elif hasattr(client, 'list_models'):
                        res = client.list_models()
                        iterable = getattr(res, 'data', None) or res
                        for it in iterable:
                            mn = getattr(it, 'name', None) or getattr(it, 'id', None) or getattr(it, 'model', None)
                            if mn and mn not in attempted:
                                model_list.append(mn)
                except Exception:
                    app.logger.debug('Listing models via client failed')

                for mn in model_list:
                    try:
                        try:
                            resp = client.models.generate_content(model=mn, contents=prompt)
                        except TypeError:
                            resp = client.models.generate_content(model=mn, content=prompt)
                        ai_text = getattr(resp, 'text', None) or (resp.output and resp.output[0].content[0].text if getattr(resp, 'output', None) else None)
                        if ai_text:
                            cache[prompt] = (ai_text, time.time())
                            app.ai_failure_count = 0
                            app.ai_last_failure_ts = 0
                            app.logger.info('AI call succeeded with listed model %s', mn)
                            return ai_text
                    except Exception:
                        app.logger.debug('Attempt with listed model %s failed', mn)

                app.ai_failure_count = getattr(app, 'ai_failure_count', 0) + 1
                app.ai_last_failure_ts = time.time()
                app.logger.error('No usable Gemini model found via client API')
                return {"error": "AI temporarily unavailable. Using fallback."}

            # Older-style GenerativeModel API
            if hasattr(genai, 'GenerativeModel'):
                for mname in candidates:
                    try:
                        m = genai.GenerativeModel(mname)
                        resp = m.generate_content(prompt)
                        ai_text = getattr(resp, 'text', None)
                        if ai_text:
                            cache[prompt] = (ai_text, time.time())
                            app.ai_failure_count = 0
                            app.ai_last_failure_ts = 0
                            return ai_text
                    except Exception:
                        app.logger.debug('GenerativeModel attempt %s failed', mname)

                app.ai_failure_count = getattr(app, 'ai_failure_count', 0) + 1
                app.ai_last_failure_ts = time.time()
                app.logger.error('No usable Gemini model found via GenerativeModel API')
                return {"error": "AI temporarily unavailable. Using fallback."}

        except Exception as e:
            app.logger.exception('Unexpected error calling Gemini SDK: %s', e)
            app.ai_failure_count = getattr(app, 'ai_failure_count', 0) + 1
            app.ai_last_failure_ts = time.time()
            return {"error": "AI call failed. Using fallback."}
        # end generate_ai_response try/except

    # Make AI function available globally inside app
    app.generate_ai_response = generate_ai_response

    # Ensure secret key is set for sessions (use env var in production)
    env_sk = os.environ.get('SECRET_KEY')
    if env_sk and env_sk.strip():
        app.config['SECRET_KEY'] = env_sk
    else:
        # Fallback to a development key to allow sessions locally
        app.config['SECRET_KEY'] = 'dev-secret-change-me'
    # Also set the Flask secret_key property explicitly so sessions work
    app.secret_key = app.config.get('SECRET_KEY')
    # Log whether a secret key is configured (do not log the key itself)
    app.logger.info('SECRET_KEY set: %s', bool(app.secret_key))

    # Initialize Flask-Login
    login_manager = LoginManager()
    login_manager.login_view = 'auth.login'
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        try:
            return db.session.get(User, int(user_id))
        except Exception:
            return None


    try:
        from auth import auth_bp
        from admin import admin_bp
        from client import client_bp
        from lawyer import lawyer_bp
        from judge import judge_bp

        app.register_blueprint(auth_bp)
        app.logger.info('Registered auth blueprint')
        app.register_blueprint(admin_bp, url_prefix="/admin")
        app.logger.info('Registered admin blueprint')
        app.register_blueprint(client_bp, url_prefix="/client")
        app.logger.info('Registered client blueprint')
        app.register_blueprint(lawyer_bp, url_prefix="/lawyer")
        app.logger.info('Registered lawyer blueprint')
        app.register_blueprint(judge_bp, url_prefix="/judge")
        app.logger.info('Registered judge blueprint')
    except Exception as e:
        app.logger.error(f'Failed to register blueprints: {e}', exc_info=True)
        raise

   
    app.rate_limit_store = {}
    RATE_LIMIT_WINDOW = 60  # seconds
    APPOINTMENTS_LIMIT = 10  # max requests per `RATE_LIMIT_WINDOW` per IP

    @app.before_request
    def _simple_rate_limiter():
        try:
            # Only enforce on the client appointments endpoint
            # Use endpoint name if available, fallback to path check.
            target_endpoint = 'client.appointments'
            if request.endpoint != target_endpoint and not (request.path or '').startswith('/client/appointments'):
                return None

            # Identify client IP (respect X-Forwarded-For if present)
            # Detect and ignore browser prefetch/navigation hints which can
            # cause background GETs (e.g., link prefetch). Do not count these
            # towards the rate limit -- just log them for diagnostics.
            purpose = (request.headers.get('Purpose') or '').lower()
            x_purpose = (request.headers.get('X-Purpose') or '').lower()
            x_moz = (request.headers.get('X-Moz') or '').lower()
            sec_fetch_mode = (request.headers.get('Sec-Fetch-Mode') or '').lower()
            if 'prefetch' in purpose or 'prefetch' in x_purpose or 'prefetch' in x_moz or sec_fetch_mode == 'no-cors':
                # Do not log prefetch requests at INFO level to avoid log noise.
                app.logger.debug(f"Ignoring prefetch-like request for {request.path} Purpose:{purpose} X-Moz:{x_moz} SecFetchMode:{sec_fetch_mode}")
                return None
            ip = None
            xff = request.headers.get('X-Forwarded-For')
            if xff:
                ip = xff.split(',')[0].strip()
            else:
                ip = request.remote_addr or 'unknown'

            key = (ip, 'appointments')
            now_ts = time.time()
            recent = app.rate_limit_store.get(key, [])
            # Keep only timestamps within the window
            recent = [t for t in recent if now_ts - t < RATE_LIMIT_WINDOW]
            recent.append(now_ts)
            app.rate_limit_store[key] = recent

            if len(recent) > APPOINTMENTS_LIMIT:
                # Log and block
                app.logger.warning(f"Rate limit exceeded for {ip} on /client/appointments ({len(recent)} reqs in {RATE_LIMIT_WINDOW}s)")
                # Persist the rate-limited event for admin review (best-effort)
                try:
                    sess = request.cookies.get(app.session_cookie_name, None)
                    ua = request.headers.get('User-Agent') if request else None
                    referer = request.headers.get('Referer') if request else None
                    rlog = RateLimitLog(ip_address=ip, session_cookie=sess, endpoint='/client/appointments', user_agent=ua, referer=referer)
                    db.session.add(rlog)
                    db.session.commit()
                except Exception:
                    app.logger.exception('Failed to write RateLimitLog')
                return make_response(("Too many requests - try again later."), 429)

            # Occasional cleanup to avoid unbounded growth
            if now_ts % 60 < 1:
                # Remove keys that haven't had activity in RATE_LIMIT_WINDOW
                stale = [k for k, v in app.rate_limit_store.items() if not any(now_ts - t < RATE_LIMIT_WINDOW for t in v)]
                for k in stale:
                    del app.rate_limit_store[k]

        except Exception:
            # Never block requests due to rate limiter errors
            app.logger.exception('Rate limiter failed')
            return None

   
    # Ensure SQLAlchemy is initialized with the Flask app
    app.config.setdefault('SQLALCHEMY_TRACK_MODIFICATIONS', False)
    # Provide a sensible default database URI if none is set in env/config
    app.config.setdefault('SQLALCHEMY_DATABASE_URI', 'sqlite:///legaltech.db')
    db.init_app(app)

    with app.app_context():
        db.create_all()
        # Ensure the new `client2_id` column exists on the `case` table for development SQLite.
        # This is a lightweight, best-effort fix to avoid manual migrations when running locally.
        try:
            from sqlalchemy import text
            conn = db.engine.connect()
            # Get columns for the `case` table
            res = conn.execute(text("PRAGMA table_info('case')"))
            cols = [row[1] for row in res.fetchall()]
            if 'client2_id' not in cols:
                try:
                    conn.execute(text('ALTER TABLE "case" ADD COLUMN client2_id INTEGER'))
                    app.logger.info('Added missing column case.client2_id')
                except Exception:
                    app.logger.exception('Failed to add case.client2_id column')
            conn.close()
        except Exception:
            app.logger.exception('Failed to verify/ensure case.client2_id column')

        # Ensure a default admin with the provided credentials exists.
        # If an admin with the target email doesn't exist but another admin does,
        # update that admin's email/password. Otherwise create a new admin.
        default_admin_email = 'admin@gmail.com'
        default_admin_password = 'admin123'

        # Ensure there is an admin account. Prefer updating any existing admin,
        # otherwise promote a user with the target email or create a new admin.
        existing_admin = User.query.filter_by(role='admin').first()
        if existing_admin:
            # Update existing admin to use the standard email/password
            existing_admin.email = default_admin_email
            existing_admin.set_password(default_admin_password)
            db.session.commit()
            print(f"[INFO] Existing admin updated to {default_admin_email}.")
        else:
            # No admin exists; promote a user with the default email if present
            user_by_email = User.query.filter_by(email=default_admin_email).first()
            if user_by_email:
                user_by_email.role = 'admin'
                user_by_email.set_password(default_admin_password)
                db.session.commit()
                print(f"[INFO] User {default_admin_email} promoted to admin.")
            else:
                # Create a fresh admin user
                admin = User(
                    email=default_admin_email,
                    role="admin",
                    name="Admin User"
                )
                admin.set_password(default_admin_password)
                db.session.add(admin)
                db.session.commit()
                print(f"[INFO] Default admin user created: {default_admin_email}.")

  
    @app.route("/")
    def index():
        # Render the full `base.html` which includes the premium hero and sections.
        return render_template('base.html')


    @app.route('/dashboard')
    @login_required
    def dashboard_router():
        # Redirect to role-specific dashboards
        role = getattr(current_user, 'role', None)
        if role == 'client':
            return redirect(url_for('client.dashboard'))
        if role == 'lawyer':
            return redirect(url_for('lawyer.dashboard'))
        if role == 'judge':
            return redirect(url_for('judge.dashboard'))
        if role == 'admin':
            return redirect(url_for('admin.dashboard'))
        # Fallback
        flash('No dashboard available for your account type.')
        return redirect(url_for('index'))

    # Authentication routes are provided by the `auth` blueprint.

    
    @app.context_processor
    def inject_now():
        # Helper to control when sensitive profile fields may be viewed.
        def can_view_sensitive(target_user):
            """Return True when the currently logged-in user is allowed to view
            sensitive fields (e.g., bar number, direct contact) for `target_user`.

            Rules:
            - Admins may always view sensitive fields.
            - The owner of the profile may view their own sensitive fields.
            - Other users (clients, anonymous) may NOT view sensitive fields.
            """
            try:
                # `current_user` is provided by Flask-Login
                if not hasattr(current_user, 'is_authenticated') or not current_user.is_authenticated:
                    return False
                if getattr(current_user, 'role', None) == 'admin':
                    allowed = True
                elif getattr(current_user, 'id', None) == getattr(target_user, 'id', None):
                    allowed = True
                else:
                    allowed = False

                # If allowed and the viewer is not the owner, record an access log
                # but only once per-request per-target to avoid noisy duplicates.
                if allowed and getattr(current_user, 'id', None) != getattr(target_user, 'id', None):
                    try:
                        if not hasattr(g, 'sensitive_access_logged'):
                            g.sensitive_access_logged = set()
                        target_id = getattr(target_user, 'id', None)
                        if target_id and target_id not in g.sensitive_access_logged:
                            ip = request.remote_addr if request else None
                            ua = request.headers.get('User-Agent') if request else None
                            log = AccessLog(viewer_id=current_user.id, target_user_id=target_id,
                                            action='view_sensitive', ip_address=ip, user_agent=ua)
                            db.session.add(log)
                            db.session.commit()
                            g.sensitive_access_logged.add(target_id)
                    except Exception:
                        # Do not let logging failures break view permissions.
                        app.logger.exception('Failed to write AccessLog')

                return allowed
            except Exception:
                return False

        # Indicate whether an AI endpoint/key appears configured
        ai_key = app.config.get('AI_API_KEY')
        ai_url = app.config.get('AI_API_URL')
        ai_available = bool(ai_key and ai_url)

        # Recent case histories for lawyer sidebar (populated only for lawyers)
        recent_case_histories = []

        # Provide lightweight sidebar counts for the current user to avoid
        # repeating DB queries in multiple templates. These are best-effort
        # and default to zero when not applicable or on errors.
        sidebar_counts = {}
        try:
            if hasattr(current_user, 'is_authenticated') and current_user.is_authenticated:
                if getattr(current_user, 'role', None) == 'client':
                    # Client counts: upcoming appointments, unread messages, active cases
                    upcoming = 0
                    unread = 0
                    active_cases = 0
                    try:
                        from models import Appointment, Message, Case
                        upcoming = Appointment.query.filter_by(user_id=current_user.id, status='confirmed').filter(Appointment.timestamp > datetime.now(UTC)).count()
                        all_my_cases = Case.query.filter_by(client_id=current_user.id).with_entities(Case.id).all()
                        my_case_ids = [c.id for c in all_my_cases]
                        unread = Message.query.filter(Message.case_id.in_(my_case_ids)).filter(Message.sender_id != current_user.id).count()
                        active_cases = Case.query.filter_by(client_id=current_user.id, status='accepted').count()
                    except Exception:
                        app.logger.exception('Failed to compute client sidebar counts')
                    sidebar_counts = {'upcoming_appointments': upcoming, 'unread_messages': unread, 'active_cases': active_cases}

                elif getattr(current_user, 'role', None) == 'lawyer':
                    # Lawyer counts: pending appointment requests, upcoming confirmed appts, active cases
                    pending = 0
                    upcoming = 0
                    active_cases = 0
                    try:
                        from models import Appointment, Case, CaseHistory
                        pending = Appointment.query.join(Case).filter(Case.lawyer_id == current_user.id, Appointment.status == 'requested').count()
                        upcoming = Appointment.query.join(Case).filter(Case.lawyer_id == current_user.id, Appointment.status == 'confirmed', Appointment.timestamp > datetime.now(UTC)).count()
                        active_cases = Case.query.filter_by(lawyer_id=current_user.id, status='accepted').count()
                        try:
                            recent_objs = CaseHistory.query.join(Case).filter(Case.lawyer_id == current_user.id).order_by(CaseHistory.timestamp.desc()).limit(5).all()
                            # Convert to simple objects for templates (safer across contexts)
                            recent_case_histories = [
                                {
                                    'case_id': r.case_id,
                                    'action': r.action,
                                    'details': r.details,
                                    'timestamp': r.timestamp,
                                    'actor_name': (r.actor.name if getattr(r, 'actor', None) else None)
                                }
                                for r in recent_objs
                            ]
                        except Exception:
                            app.logger.exception('Failed to load recent CaseHistory for sidebar')
                    except Exception:
                        app.logger.exception('Failed to compute lawyer sidebar counts')
                    sidebar_counts = {'pending_appointments': pending, 'upcoming_appointments': upcoming, 'active_cases': active_cases}
        except Exception:
            app.logger.exception('Failed to prepare sidebar counts')

        return {
            'now': datetime.now(UTC),
            'can_view_sensitive': can_view_sensitive,
            'ai_available': ai_available,
            'sidebar_counts': sidebar_counts,
            'recent_case_histories': recent_case_histories
        }

   
    @app.route('/.well-known/appspecific/com.chrome.devtools.json')
    def chrome_devtools_json():
        return '{}', 200, {'Content-Type': 'application/json'}

    return app


app = create_app()



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)


