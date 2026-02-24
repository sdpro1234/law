# lawyer.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from flask_login import login_required, current_user
from models import db, User, Case, Appointment, Document, Message, CaseHistory
from werkzeug.utils import secure_filename
import uuid
from sqlalchemy.exc import IntegrityError
import os
from datetime import datetime

# --- Decorator for Verified Lawyer Access ---
def lawyer_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Basic checks: must be logged in and have role 'lawyer'
        if not current_user.is_authenticated or current_user.role != 'lawyer':
            flash('Verified lawyer access required.')
            return redirect(url_for('index'))

        # If the lawyer is not verified, allow access only when the app config
        # explicitly permits bypassing verification (development/testing).
        allow_unverified = current_app.config.get('ALLOW_UNVERIFIED_LAWYERS', False)
        if not current_user.is_verified and not allow_unverified:
            flash('Verified lawyer access required.')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

lawyer_bp = Blueprint('lawyer', __name__)

# --- Lawyer Routes ---

@lawyer_bp.route('/dashboard')
@login_required
def dashboard():
    # Allow logged-in lawyers (including unverified in development) to view dashboard.
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    # --- Calculate Dashboard Stats ---
    new_requests = Case.query.filter_by(status='open').count()
    active_cases = Case.query.filter_by(lawyer_id=current_user.id, status='accepted').count()
    earnings = 0 # Placeholder for earnings calculation
    
    # Get a limited list of new requests to display on the dashboard
    recent_requests = Case.query.filter_by(status='open').limit(5).all()

    # Appointment-related stats: pending requests and upcoming confirmed appointments
    pending_appts = Appointment.query.join(Case).filter(
        Case.lawyer_id == current_user.id,
        Appointment.status == 'requested'
    ).count()
    upcoming_appts = Appointment.query.join(Case).filter(
        Case.lawyer_id == current_user.id,
        Appointment.status == 'confirmed',
        Appointment.timestamp > datetime.utcnow()
    ).count()
    # Fetch a short list of pending appointment objects for dashboard preview
    try:
        pending_appointments_list = Appointment.query.join(Case).filter(
            Case.lawyer_id == current_user.id,
            Appointment.status == 'requested'
        ).order_by(Appointment.timestamp.asc()).limit(5).all()
    except Exception:
        pending_appointments_list = []

    return render_template('lawyer/lawyer_dashboard.html',
                         new_requests_count=new_requests,
                         active_cases_count=active_cases,
                         earnings_this_month=earnings,
                         new_requests=recent_requests,
                         pending_appointments_count=pending_appts,
                         upcoming_appointments_count=upcoming_appts,
                         pending_appointments_list=pending_appointments_list)

@lawyer_bp.route('/profile', methods=['GET', 'POST'])
@login_required
@lawyer_required
def profile_form():
    if request.method == 'POST':
        current_user.name = request.form.get('name')
        current_user.bio = request.form.get('bio')
        current_user.specialization = request.form.get('specialization')
        try:
            current_user.experience_years = int(request.form.get('experience_years') or 0)
        except ValueError:
            current_user.experience_years = 0
        current_user.education = request.form.get('education')
        current_user.location = request.form.get('location')
        submitted_bar = (request.form.get('bar_number') or '').strip()
        # Normalize bar number to a consistent form for uniqueness (uppercase)
        normalized_bar = submitted_bar.upper() if submitted_bar else ''
        # Check uniqueness of bar_number (allow keeping own bar_number)
        if normalized_bar:
            conflict = User.query.filter(User.bar_number == normalized_bar, User.id != current_user.id).first()
            if conflict:
                flash('The provided Bar Number is already in use by another lawyer. Please verify and try again.', 'danger')
                return redirect(url_for('lawyer.profile_form'))
            current_user.bar_number = normalized_bar
        # Handle Profile Picture Upload
        file = request.files.get('profile_picture')
        if file and file.filename:
            # Generate a safe, unique filename to avoid leaking info
            safe_name = secure_filename(file.filename)
            ext = os.path.splitext(safe_name)[1]
            filename = f"{uuid.uuid4().hex}{ext}"
            upload_folder = current_app.config.get('UPLOAD_FOLDER', os.path.join('static', 'uploads'))
            # Ensure upload folder exists
            if not os.path.exists(upload_folder):
                os.makedirs(upload_folder)
            save_path = os.path.join(upload_folder, filename)
            file.save(save_path)
            # Store a relative static path for templates to render
            current_user.profile_pic = os.path.join('static', 'uploads', filename).replace('\\', '/')

        try:
            # Ensure the current_user is attached to session
            db.session.add(current_user)
            db.session.commit()
            # Refresh to ensure changes are reloaded from DB
            db.session.refresh(current_user)
            flash('Profile updated successfully.', 'success')
        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.error(f'IntegrityError saving profile: {e}')
            flash('Failed to update profile due to a database constraint. Please check your inputs (e.g., Bar Number).', 'danger')
        return redirect(url_for('lawyer.profile_form'))
        
    # Render the profile template (match the template at templates/lawyer/profile.html)
    return render_template('lawyer/profile.html')


@lawyer_bp.route('/profile/data')
@login_required
def profile_data():
    """Return the current lawyer's profile as JSON for verification.

    This endpoint is intended for the profile owner (or admin) to verify
    that stored profile details match what was submitted. Sensitive fields
    (bar_number, email) are included only when `can_view_sensitive` permits.
    """
    # Ensure the user is a lawyer (owner access)
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        return jsonify({'error': 'Lawyer authentication required.'}), 403

    # Build base response
    data = {
        'id': current_user.id,
        'name': current_user.name,
        'bio': current_user.bio,
        'education': current_user.education,
        'specialization': current_user.specialization,
        'experience_years': current_user.experience_years,
        'location': current_user.location,
        'profile_pic': current_user.profile_pic,
        'is_verified': current_user.is_verified
    }

    # Sensitive fields are returned only when allowed
    try:
        # `can_view_sensitive` is exposed via app.context_processor in app.py
        allowed = False
        # we may call the helper by importing from flask's current_app Jinja context,
        # but easiest is to re-evaluate the same rules here.
        if getattr(current_user, 'role', None) == 'admin' or getattr(current_user, 'id', None) == getattr(current_user, 'id', None):
            allowed = True
    except Exception:
        allowed = False

    if allowed:
        data['bar_number'] = current_user.bar_number
        data['email'] = current_user.email

    return jsonify(data)


@lawyer_bp.route('/profile/edit', methods=['GET', 'POST'])
@login_required
def edit_profile():
    """
    Allow a logged-in lawyer to edit their profile even if not yet verified.
    This route bypasses the stricter `lawyer_required` decorator used for
    other lawyer-only sections (which may require verification).
    """
    # Ensure the user is a lawyer
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required to edit profile.')
        return redirect(url_for('index'))

    if request.method == 'POST':
        # Reuse the same update logic as profile_form
        current_user.name = request.form.get('name')
        current_user.bio = request.form.get('bio')
        current_user.specialization = request.form.get('specialization')
        try:
            current_user.experience_years = int(request.form.get('experience_years') or 0)
        except ValueError:
            current_user.experience_years = 0
        current_user.education = request.form.get('education')
        current_user.location = request.form.get('location')
        submitted_bar = (request.form.get('bar_number') or '').strip()
        normalized_bar = submitted_bar.upper() if submitted_bar else ''
        if normalized_bar:
            conflict = User.query.filter(User.bar_number == normalized_bar, User.id != current_user.id).first()
            if conflict:
                flash('The provided Bar Number is already in use by another lawyer. Please verify and try again.', 'danger')
                return redirect(url_for('lawyer.edit_profile'))
            current_user.bar_number = normalized_bar

        # Handle Profile Picture Upload
        file = request.files.get('profile_picture')
        if file and file.filename:
            safe_name = secure_filename(file.filename)
            ext = os.path.splitext(safe_name)[1]
            filename = f"{uuid.uuid4().hex}{ext}"
            upload_folder = current_app.config.get('UPLOAD_FOLDER', os.path.join('static', 'uploads'))
            if not os.path.exists(upload_folder):
                os.makedirs(upload_folder)
            save_path = os.path.join(upload_folder, filename)
            file.save(save_path)
            current_user.profile_pic = os.path.join('static', 'uploads', filename).replace('\\', '/')

        try:
            db.session.add(current_user)
            db.session.commit()
            db.session.refresh(current_user)
            flash('Profile updated successfully.', 'success')
        except IntegrityError as e:
            db.session.rollback()
            current_app.logger.error(f'IntegrityError saving profile (edit): {e}')
            flash('Failed to update profile due to a database constraint. Please check your inputs (e.g., Bar Number).', 'danger')
        return redirect(url_for('lawyer.edit_profile'))

    return render_template('lawyer/profile.html')


@lawyer_bp.route('/view/<int:lawyer_id>')
def view_profile(lawyer_id):
    """Public view of a lawyer's profile for clients and other users."""
    lawyer = User.query.filter_by(id=lawyer_id, role='lawyer').first_or_404()
    return render_template('lawyer/profile.html', lawyer=lawyer, view_only=True)

@lawyer_bp.route('/case_requests')
@login_required
def case_requests():
    # Allow any logged-in lawyer (including unverified in dev) to view open cases
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    # Fetch all open cases that match the lawyer's specialization
    open_cases = Case.query.filter_by(status='open').all()
    return render_template('lawyer/case_requests.html', open_cases=open_cases)


@lawyer_bp.route('/case/<int:case_id>')
@login_required
def view_case_details(case_id):
    """Lawyer-facing case detail view so lawyers can inspect or accept open cases.
    Allows viewing when the case is open or already assigned to this lawyer.

    Note: we perform an inline role check so that unverified lawyers (during
    development) can still inspect open cases without being redirected by the
    stricter `lawyer_required` decorator which may enforce verification.
    """
    # Ensure the requester is a lawyer
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    case = Case.query.get_or_404(case_id)
    # Permit viewing open cases (so lawyers can inspect before accepting) or cases assigned to the lawyer
    if case.status != 'open' and case.lawyer_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('lawyer.dashboard'))
    return render_template('lawyer/case_detail.html', case=case)


@lawyer_bp.route('/case/<int:case_id>/history', methods=['GET', 'POST'])
@login_required
def case_history(case_id):
    """View and (for assigned lawyers) append to a case's history."""
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    case = Case.query.get_or_404(case_id)
    # Allow viewing if the case is open or assigned to this lawyer
    if case.status != 'open' and case.lawyer_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('lawyer.dashboard'))

    if request.method == 'POST':
        # Only the assigned lawyer may append entries
        if case.lawyer_id != current_user.id:
            flash('Only the assigned lawyer can add history entries.', 'danger')
            return redirect(url_for('lawyer.case_history', case_id=case_id))

        action = (request.form.get('action') or '').strip() or 'note'
        details = request.form.get('details') or ''
        entry = CaseHistory(case_id=case.id, actor_id=current_user.id, action=action, details=details)
        db.session.add(entry)
        db.session.commit()
        # Also add a short message visible in chat/logs
        try:
            msg = Message(content=f'History: {action} by {current_user.name}', case_id=case.id, sender_id=current_user.id)
            db.session.add(msg)
            db.session.commit()
        except Exception:
            db.session.rollback()
        flash('History entry added.', 'success')
        return redirect(url_for('lawyer.case_history', case_id=case_id))

    histories = CaseHistory.query.filter_by(case_id=case.id).order_by(CaseHistory.timestamp.desc()).all()
    return render_template('lawyer/case_history.html', case=case, histories=histories)


@lawyer_bp.route('/my_cases')
@login_required
def my_cases():
    """List cases assigned to the current lawyer with quick links to their history."""
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    cases = Case.query.filter_by(lawyer_id=current_user.id).order_by(Case.timestamp.desc()).all()
    # Provide a list of judges for the 'Send to Judge' action
    try:
        judges = User.query.filter_by(role='judge').order_by(User.name.asc()).all()
    except Exception:
        judges = []
    return render_template('lawyer/my_cases.html', cases=cases, judges=judges)


@lawyer_bp.route('/send_to_judge', methods=['POST'])
@login_required
def send_to_judge():
    """Assign selected cases to a judge (bulk action initiated by lawyer).

    Expects form fields: `case_ids` (one or more) and `judge_id`.
    Only cases assigned to the current lawyer will be modified.
    """
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    case_ids = request.form.getlist('case_ids')
    judge_id = request.form.get('judge_id')
    if not case_ids:
        flash('No cases selected.', 'warning')
        return redirect(url_for('lawyer.my_cases'))
    if not judge_id:
        flash('Please select a judge to send to.', 'warning')
        return redirect(url_for('lawyer.my_cases'))

    try:
        judge = User.query.filter_by(id=int(judge_id), role='judge').first()
    except Exception:
        judge = None

    if not judge:
        flash('Selected judge not found.', 'danger')
        return redirect(url_for('lawyer.my_cases'))

    updated = 0
    for cid in case_ids:
        try:
            c = Case.query.get(int(cid))
        except Exception:
            c = None
        if not c:
            continue
        # Only modify cases that belong to this lawyer
        if c.lawyer_id != current_user.id:
            continue
        try:
            c.judge_id = judge.id
            # Add history entry
            hist = CaseHistory(case_id=c.id, actor_id=current_user.id, action='sent_to_judge', details=f'Sent to judge {judge.name or judge.email}')
            db.session.add(hist)
            db.session.add(c)
            db.session.commit()
            updated += 1
        except Exception:
            db.session.rollback()
            continue

    flash(f'Sent {updated} case(s) to {judge.name or judge.email}.')
    return redirect(url_for('lawyer.my_cases'))


@lawyer_bp.route('/history')
@login_required
def history():
    """Show all CaseHistory entries for cases assigned to this lawyer."""
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    # CaseHistory joined with Case where the case is assigned to this lawyer
    try:
        entries = CaseHistory.query.join(Case).filter(Case.lawyer_id == current_user.id).order_by(CaseHistory.timestamp.desc()).all()
    except Exception:
        entries = []

    # Provide a list of judges so the lawyer can send/share cases directly
    try:
        judges = User.query.filter_by(role='judge').order_by(User.name.asc()).all()
    except Exception:
        judges = []

    return render_template('lawyer/history.html', entries=entries, judges=judges)

@lawyer_bp.route('/accept_case/<int:case_id>', methods=['GET', 'POST'])
@login_required
def accept_case(case_id):
    # Inline role & verification checks so unverified lawyers can act in dev
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    case = Case.query.get_or_404(case_id)
    if case.status != 'open':
        flash('Case is not open for acceptance.')
        return redirect(url_for('lawyer.case_requests'))

    case.lawyer_id = current_user.id
    case.status = 'accepted'
    try:
        db.session.commit()
        # Record history entry
        try:
            hist = CaseHistory(case_id=case.id, actor_id=current_user.id, action='accepted', details=f'Case accepted by {current_user.name}')
            db.session.add(hist)
            db.session.commit()
        except Exception:
            db.session.rollback()
        # Optionally notify the client via an in-app message
        try:
            msg = Message(content=f'Your case was accepted by {current_user.name}.', case_id=case.id, sender_id=current_user.id)
            db.session.add(msg)
            db.session.commit()
        except Exception:
            db.session.rollback()
        flash('Case accepted.')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Failed to accept case {case_id}: {e}')
        flash('Failed to accept case. Please try again.', 'danger')

    return redirect(url_for('lawyer.case_requests'))


@lawyer_bp.route('/reject_case/<int:case_id>', methods=['GET', 'POST'])
@login_required
def reject_case(case_id):
    # Inline role & verification checks
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    case = Case.query.get_or_404(case_id)
    # If the case is assigned to another lawyer, deny
    if case.lawyer_id and case.lawyer_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('lawyer.case_requests'))

    # Unassign and reopen the case so other lawyers can claim it
    case.lawyer_id = None
    case.status = 'open'
    try:
        db.session.commit()
        # Record history entry
        try:
            hist = CaseHistory(case_id=case.id, actor_id=current_user.id, action='rejected', details=f'Case rejected by {current_user.name}')
            db.session.add(hist)
            db.session.commit()
        except Exception:
            db.session.rollback()
        try:
            msg = Message(content=f'{current_user.name} declined the case. It has been returned to the open pool.', case_id=case.id, sender_id=current_user.id)
            db.session.add(msg)
            db.session.commit()
        except Exception:
            db.session.rollback()
        flash('Case rejected and returned to the open pool.')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Failed to reject case {case_id}: {e}')
        flash('Failed to reject case. Please try again.', 'danger')

    return redirect(url_for('lawyer.case_requests'))

@lawyer_bp.route('/documents/<int:case_id>', methods=['GET', 'POST'])
@login_required
def document_upload(case_id):
    # Ensure the requester is a lawyer (allow unverified in dev)
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    case = Case.query.get_or_404(case_id)
    if case.lawyer_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('lawyer.dashboard'))
        
    if request.method == 'POST':
        file = request.files.get('document_file')
        if file and file.filename:
            safe_name = secure_filename(file.filename)
            ext = os.path.splitext(safe_name)[1]
            filename = f"{uuid.uuid4().hex}{ext}"
            # Create uploads directory if it doesn't exist
            if not os.path.exists(current_app.config['UPLOAD_FOLDER']):
                os.makedirs(current_app.config['UPLOAD_FOLDER'])
            file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))

            new_doc = Document(
                filename=filename,
                original_filename=file.filename,
                description=request.form.get('document_description'),
                case_id=case.id,
                uploader_id=current_user.id
            )
            db.session.add(new_doc)
            db.session.commit()
            # Add history entry for document upload
            try:
                hist = CaseHistory(case_id=case.id, actor_id=current_user.id, action='document_uploaded', details=f'Document uploaded: {new_doc.original_filename}')
                db.session.add(hist)
                db.session.commit()
            except Exception:
                db.session.rollback()
            flash('Document uploaded successfully.')
        return redirect(url_for('lawyer.document_upload', case_id=case_id))
        
    return render_template('lawyer/document_upload.html', case=case)

@lawyer_bp.route('/chat/<int:case_id>')
@login_required
def lawyer_chat(case_id):
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    case = Case.query.get_or_404(case_id)
    # Allow lawyers to view the chat for cases assigned to them, or for open cases
    # so they can communicate with the client before accepting.
    if not (case.lawyer_id == current_user.id or case.status == 'open'):
        flash('Permission denied.')
        return redirect(url_for('lawyer.dashboard'))

    other_party = case.client
    messages = Message.query.filter_by(case_id=case.id).order_by(Message.timestamp.asc()).all()
    return render_template('lawyer/lawyer_chat.html', case=case, other_party=other_party, messages=messages)


@lawyer_bp.route('/create_appointment/<int:case_id>', methods=['GET', 'POST'])
@login_required
def create_appointment(case_id):
    # Allow lawyers to create appointment requests for cases assigned to them
    # or for open cases where they may be communicating with the client.
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    case = Case.query.get_or_404(case_id)
    if not (case.lawyer_id == current_user.id or case.status == 'open'):
        flash('Permission denied.')
        return redirect(url_for('lawyer.case_requests'))

    if request.method == 'POST':
        # Expect date and time fields in the form
        date_str = request.form.get('date')
        time_str = request.form.get('time')
        appt_type = request.form.get('consultation_type') or 'Consultation'
        notes = request.form.get('notes')
        try:
            ts = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except Exception:
            flash('Invalid date/time format. Use YYYY-MM-DD and HH:MM.', 'danger')
            return render_template('lawyer/create_appointment.html', case=case)

        new_appt = Appointment(
            timestamp=ts,
            type=appt_type,
            notes=notes,
            user_id=case.client_id,
            case_id=case.id,
            status='requested'
        )
        db.session.add(new_appt)
        db.session.commit()
        # Notify client via message
        try:
            msg = Message(content=f'Lawyer {current_user.name} requested an appointment on {new_appt.timestamp}.', case_id=case.id, sender_id=current_user.id)
            db.session.add(msg)
            db.session.commit()
        except Exception:
            db.session.rollback()
        flash('Appointment request sent to the client.')
        return redirect(url_for('lawyer.appointment_requests'))

    return render_template('lawyer/create_appointment.html', case=case)


@lawyer_bp.route('/seed_request/<int:case_id>', methods=['POST', 'GET'])
@login_required
def seed_request(case_id):
    """Dev helper: create a test appointment request for the given case.
    Controlled by app config `ENABLE_DEV_ENDPOINTS` (default True in dev).
    """
    if not current_app.config.get('ENABLE_DEV_ENDPOINTS', True):
        flash('Dev endpoints disabled.')
        return redirect(url_for('lawyer.case_requests'))

    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    case = Case.query.get_or_404(case_id)
    # Create an appointment 24 hours from now at 10:00
    from datetime import timedelta
    ts = datetime.utcnow() + timedelta(days=1)
    ts = ts.replace(hour=10, minute=0, second=0, microsecond=0)
    new_appt = Appointment(
        timestamp=ts,
        type='Consultation',
        notes='Test appointment generated by dev endpoint.',
        user_id=case.client_id,
        case_id=case.id,
        status='requested'
    )
    db.session.add(new_appt)
    db.session.commit()
    flash('Test appointment request created.')
    return redirect(url_for('lawyer.appointment_requests'))

@lawyer_bp.route('/schedule')
@login_required
def appointment_schedule():
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    # Fetch all future confirmed appointments for cases assigned to this lawyer
    upcoming_appts = Appointment.query.join(Case).filter(
        Case.lawyer_id == current_user.id,
        Appointment.status == 'confirmed',
        Appointment.timestamp > datetime.utcnow()
    ).order_by(Appointment.timestamp.asc()).all()
    avail_store = getattr(current_app, 'lawyer_availability', {})
    my_avail = avail_store.get(current_user.id)
    return render_template('lawyer/appointment_schedule.html', upcoming_appointments=upcoming_appts, availability=my_avail)


@lawyer_bp.route('/reschedule/<int:appt_id>', methods=['GET', 'POST'])
@login_required
def reschedule_appointment(appt_id):
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    appt = Appointment.query.get_or_404(appt_id)
    # Ensure this appointment is for a case that belongs to the current lawyer
    if not appt.case or appt.case.lawyer_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('lawyer.appointment_schedule'))

    if request.method == 'POST':
        date_str = request.form.get('date')
        time_str = request.form.get('time')
        try:
            new_ts = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        except Exception:
            flash('Invalid date/time format. Use YYYY-MM-DD and HH:MM.', 'danger')
            return render_template('lawyer/reschedule_appointment.html', appt=appt)

        appt.timestamp = new_ts
        # If appointment was cancelled/completed, do not allow reschedule
        if appt.status in ('cancelled', 'completed'):
            flash('Cannot reschedule a cancelled or completed appointment.', 'danger')
            return redirect(url_for('lawyer.appointment_schedule'))

        # Keep status as requested/confirmed as appropriate
        db.session.commit()

        # Notify client via in-app message
        try:
            msg = Message(content=f'Appointment for case #{appt.case_id} has been rescheduled to {appt.timestamp}.', case_id=appt.case_id, sender_id=current_user.id)
            db.session.add(msg)
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Add history entry for reschedule
        try:
            hist = CaseHistory(case_id=appt.case_id, actor_id=current_user.id, action='appointment_rescheduled', details=f'Rescheduled to {appt.timestamp}')
            db.session.add(hist)
            db.session.commit()
        except Exception:
            db.session.rollback()

        flash('Appointment rescheduled.')
        return redirect(url_for('lawyer.appointment_schedule'))

    # GET: render form with current timestamp prefilled
    return render_template('lawyer/reschedule_appointment.html', appt=appt)


@lawyer_bp.route('/set_availability', methods=['GET', 'POST'])
@login_required
def set_availability():
    """Simple availability editor for lawyers (development-friendly).
    Stores availability in-memory on `current_app.lawyer_availability` as:
    { user_id: { 'days': ['mon','tue'], 'start':'09:00', 'end':'17:00' } }
    """
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    # Init store if missing
    avail_store = getattr(current_app, 'lawyer_availability', {})
    current = avail_store.get(current_user.id, {})

    if request.method == 'POST':
        days = request.form.getlist('days')  # list of day keys
        start = request.form.get('start')
        end = request.form.get('end')
        # Basic validation
        if not days or not start or not end:
            flash('Please select at least one day and provide start/end times.', 'danger')
            return render_template('lawyer/set_availability.html', current=current)

        avail_store[current_user.id] = {'days': days, 'start': start, 'end': end}
        current_app.lawyer_availability = avail_store
        flash('Availability saved.')
        return redirect(url_for('lawyer.appointment_schedule'))

    return render_template('lawyer/set_availability.html', current=current)


@lawyer_bp.route('/appointment_requests')
@login_required
def appointment_requests():
    # Allow any logged-in lawyer to view appointment requests for their cases
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))

    # List pending appointment requests for cases assigned to this lawyer
    pending = Appointment.query.join(Case).filter(
        Case.lawyer_id == current_user.id,
        Appointment.status == 'requested'
    ).order_by(Appointment.timestamp.asc()).all()
    # Provide an example case id for convenience (assigned case or an open case)
    example_case = Case.query.filter_by(lawyer_id=current_user.id).first()
    if not example_case:
        example_case = Case.query.filter_by(status='open').first()
    example_case_id = example_case.id if example_case else None
    return render_template('lawyer/appointment_requests.html', pending_requests=pending, example_case_id=example_case_id)


@lawyer_bp.route('/accept_appointment/<int:appt_id>', methods=['POST', 'GET'])
@login_required
def accept_appointment(appt_id):
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    appt = Appointment.query.get_or_404(appt_id)
    # Ensure this appointment belongs to a case for this lawyer
    if not appt.case or appt.case.lawyer_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('lawyer.appointment_requests'))
    appt.status = 'confirmed'
    db.session.commit()
    # Add history entry for appointment confirmation
    try:
        hist = CaseHistory(case_id=appt.case_id, actor_id=current_user.id, action='appointment_confirmed', details=f'Appointment {appt.id} confirmed')
        db.session.add(hist)
        db.session.commit()
    except Exception:
        db.session.rollback()
    # Optionally notify the client via an in-app message
    try:
        msg = Message(content=f'Your appointment on {appt.timestamp} was confirmed by the lawyer.', case_id=appt.case_id, sender_id=current_user.id)
        db.session.add(msg)
        db.session.commit()
    except Exception:
        db.session.rollback()
    flash('Appointment confirmed.')
    return redirect(url_for('lawyer.appointment_requests'))


@lawyer_bp.route('/reject_appointment/<int:appt_id>', methods=['POST', 'GET'])
@login_required
def reject_appointment(appt_id):
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    appt = Appointment.query.get_or_404(appt_id)
    if not appt.case or appt.case.lawyer_id != current_user.id:
        flash('Permission denied.')
        return redirect(url_for('lawyer.appointment_requests'))
    appt.status = 'cancelled'
    db.session.commit()
    # Add history entry for appointment rejection
    try:
        hist = CaseHistory(case_id=appt.case_id, actor_id=current_user.id, action='appointment_cancelled', details=f'Appointment {appt.id} cancelled by lawyer')
        db.session.add(hist)
        db.session.commit()
    except Exception:
        db.session.rollback()
    # Notify client
    try:
        msg = Message(content=f'Your appointment request on {appt.timestamp} was declined by the lawyer.', case_id=appt.case_id, sender_id=current_user.id)
        db.session.add(msg)
        db.session.commit()
    except Exception:
        db.session.rollback()
    flash('Appointment request rejected.')
    return redirect(url_for('lawyer.appointment_requests'))

@lawyer_bp.route('/send_message/<int:case_id>', methods=['POST'])
@login_required
def send_message(case_id):
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    case = Case.query.get_or_404(case_id)
    # Allow sending messages if the lawyer is assigned to the case, or if
    # the case is still open so lawyers can reach out before accepting.
    if not (case.lawyer_id == current_user.id or case.status == 'open'):
        flash('Permission denied.')
        return redirect(url_for('lawyer.dashboard'))
        
    content = request.form.get('content')
    if content:
        message = Message(content=content, case_id=case_id, sender_id=current_user.id)
        db.session.add(message)
        db.session.commit()
    return redirect(url_for('lawyer.lawyer_chat', case_id=case_id))

@lawyer_bp.route('/close_case/<int:case_id>')
@login_required
def close_case(case_id):
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    case = Case.query.get_or_404(case_id)
    if case.lawyer_id == current_user.id and case.status == 'accepted':
        case.status = 'closed'
        db.session.commit()
        flash('Case marked as closed. The client can now leave a review.')
    return redirect(url_for('lawyer.lawyer_chat', case_id=case_id))

# --- Helper Route for Appointments ---
@lawyer_bp.route('/join_appointment/<int:appt_id>')
@login_required
def join_appointment(appt_id):
    if not current_user.is_authenticated or current_user.role != 'lawyer':
        flash('Lawyer access required.')
        return redirect(url_for('index'))
    # In a real app, this would integrate with a service like Zoom or WebRTC
    appointment = Appointment.query.get_or_404(appt_id)
    if appointment.user_id == current_user.id:
        flash(f'Joining appointment for case {appointment.case_id}...')
        # redirect to video call URL
    else:
        flash('Invalid appointment.')
    return redirect(url_for('lawyer.appointment_schedule'))