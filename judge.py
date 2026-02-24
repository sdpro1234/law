from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app, jsonify
from flask_login import login_required, login_user, current_user
from models import db, User, Case, Hearing, JudgeClient, Appointment, CaseHistory
from datetime import datetime
from werkzeug.utils import secure_filename
import os
import uuid
from sqlalchemy.exc import OperationalError
from sqlalchemy import text
import json
import ai_recommendation_engine as ai_engine

judge_bp = Blueprint('judge', __name__, url_prefix='/judge')


@judge_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        name = request.form.get('name')

        court_name = request.form.get('court_name')
        judge_id_number = request.form.get('judge_id_number')
        file = request.files.get('verification_document')

        if User.query.filter_by(email=email).first():
            flash('Email already exists.')
            return redirect(url_for('judge.register'))

        new_user = User(email=email, role='judge', name=name)
        new_user.set_password(password)
        # Judges should be verified by admin before elevated privileges
        new_user.is_verified = False
        new_user.court_name = court_name
        new_user.judge_id_number = judge_id_number

        # Handle optional verification document upload
        if file and file.filename:
            safe = secure_filename(file.filename)
            ext = os.path.splitext(safe)[1]
            filename = f"judge_{uuid.uuid4().hex}{ext}"
            upload_folder = current_app.config.get('UPLOAD_FOLDER') or os.path.join('static', 'uploads')
            if not os.path.exists(upload_folder):
                os.makedirs(upload_folder)
            save_path = os.path.join(upload_folder, filename)
            file.save(save_path)
            # store relative path
            new_user.verification_document = os.path.join('static', 'uploads', filename).replace('\\','/')

        db.session.add(new_user)
        db.session.commit()
        flash('Judge registration submitted. Awaiting admin verification.')
        return redirect(url_for('auth.login'))
    return render_template('judge/register.html')


@judge_bp.route('/dashboard')
@login_required
def dashboard():
    if getattr(current_user, 'role', None) != 'judge':
        abort(403)
    # Show cases assigned to this judge and open cases
    try:
        assigned = Case.query.filter_by(judge_id=current_user.id).all()
    except OperationalError:
        # Try to add the missing column for SQLite development and retry once
        try:
            conn = db.engine.connect()
            conn.execute(text('ALTER TABLE "case" ADD COLUMN client2_id INTEGER'))
            conn.close()
            assigned = Case.query.filter_by(judge_id=current_user.id).all()
        except Exception:
            current_app.logger.exception('Failed to add missing client2_id column')
            raise
    open_cases = Case.query.filter_by(status='open').all()
    # Count how many clients this judge has registered via the judge UI
    try:
        managed_count = JudgeClient.query.filter_by(judge_id=current_user.id).count()
    except Exception:
        managed_count = 0
    return render_template('judge/dashboard.html', assigned=assigned, open_cases=open_cases, managed_count=managed_count)


@judge_bp.route('/cases')
@login_required
def list_cases():
    if getattr(current_user, 'role', None) != 'judge':
        abort(403)
    cases = Case.query.order_by(Case.timestamp.desc()).all()
    return render_template('judge/list_cases.html', cases=cases)


@judge_bp.route('/cases/new', methods=['GET', 'POST'])
@login_required
def new_case():
    if getattr(current_user, 'role', None) != 'judge':
        abort(403)
    if request.method == 'POST':
        client_id = request.form.get('client_id')
        client2_id = request.form.get('client2_id')
        # Description removed from form; allow empty description
        description = (request.form.get('description') or '').strip()
        category = request.form.get('category') or 'General'
        if not client_id:
            flash('Client is required')
            return redirect(url_for('judge.new_case'))

        # Create a single Case record; set client2_id when a secondary client is provided
        try:
            kwargs = dict(client_id=int(client_id), description=description, category=category, judge_id=current_user.id)
            if client2_id and str(client2_id).strip() and int(client2_id) != int(client_id):
                kwargs['client2_id'] = int(client2_id)
            case = Case(**kwargs)
            db.session.add(case)
            db.session.commit()
            flash('Case registered successfully')
            return redirect(url_for('judge.dashboard'))
        except Exception:
            db.session.rollback()
            flash('Failed to create case.', 'danger')
            return redirect(url_for('judge.new_case'))
    # Provide a minimal client list for selection
    clients = User.query.filter_by(role='client').all()
    return render_template('judge/new_case.html', clients=clients)


@judge_bp.route('/register_clients', methods=['GET', 'POST'])
@login_required
def register_clients():
    if getattr(current_user, 'role', None) != 'judge':
        abort(403)
    if request.method == 'POST':
        # Expect up to two client blocks: name1,email1,password1 and name2,email2,password2
        created = 0
        for i in (1, 2):
            name = (request.form.get(f'name{i}') or '').strip()
            email = (request.form.get(f'email{i}') or '').strip()
            password = (request.form.get(f'password{i}') or '').strip()
            phone = (request.form.get(f'phone{i}') or '').strip()
            address = (request.form.get(f'address{i}') or '').strip()
            # Skip entirely empty blocks
            if not email and not name and not phone and not address and not password:
                continue

            existing = User.query.filter_by(email=email).first() if email else None
            if existing:
                # If a user exists and is a client, update contact fields if provided
                if existing.role != 'client':
                    flash(f'User with email {email} exists but is not a client. Skipping.', 'warning')
                    continue
                if phone:
                    existing.phone_number = phone
                if address:
                    existing.address = address
                # If password provided, update it
                if password:
                    existing.set_password(password)
                if name:
                    existing.name = name
                try:
                    db.session.add(existing)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    flash(f'Failed to update existing user {email}.', 'danger')
                    continue
                user = existing
            else:
                # Need email and password to create a new client
                if not email or not password:
                    flash(f'Email and password required to create a new client (block {i}). Skipping.', 'warning')
                    continue
                user = User(email=email, role='client', name=name)
                user.set_password(password)
                user.phone_number = phone or None
                user.address = address or None
                db.session.add(user)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    flash(f'Failed to create user {email}.', 'danger')
                    continue

            # Associate with judge (if not already linked)
            try:
                exists_link = JudgeClient.query.filter_by(judge_id=current_user.id, client_id=user.id).first()
                if not exists_link:
                    jc = JudgeClient(judge_id=current_user.id, client_id=user.id)
                    db.session.add(jc)
                    db.session.commit()
                created += 1
            except Exception:
                db.session.rollback()
                flash(f'Failed to link user {user.email} to judge.', 'danger')
                continue
        if created:
            flash(f'Created and linked {created} client user(s).', 'success')
        else:
            flash('No users created. Provide email and password for at least one user.', 'warning')
        return redirect(url_for('judge.dashboard'))
    return render_template('judge/register_clients.html')


@judge_bp.route('/cases/<int:case_id>')
@login_required
def view_case(case_id):
    if getattr(current_user, 'role', None) != 'judge':
        abort(403)
    case = Case.query.get_or_404(case_id)
    hearings = Hearing.query.filter_by(case_id=case.id).order_by(Hearing.scheduled_at.desc()).all()
    return render_template('judge/view_case.html', case=case, hearings=hearings)


@judge_bp.route('/cases/<int:case_id>/ai_report', methods=['POST'])
@login_required
def ai_report(case_id):
    if getattr(current_user, 'role', None) != 'judge':
        abort(403)
    case = Case.query.get_or_404(case_id)
    mode = request.form.get('mode')
    try:
        resp_text = ai_engine.get_ai_case_report_and_recommendations(case.description or '', mode=mode)
        data = json.loads(resp_text)
        return jsonify({'ok': True, 'data': data})
    except Exception:
        current_app.logger.exception('AI report generation failed')
        return jsonify({'ok': False, 'error': 'AI report generation failed'}), 500


@judge_bp.route('/cases/<int:case_id>/verify_jurisdiction', methods=['POST'])
@login_required
def verify_jurisdiction(case_id):
    if getattr(current_user, 'role', None) != 'judge':
        abort(403)
    case = Case.query.get_or_404(case_id)
    # Prevent overwriting an existing judge assignment
    if case.judge_id and case.judge_id != current_user.id:
        flash('Case already assigned to another judge.', 'warning')
        return redirect(url_for('judge.view_case', case_id=case_id))

    case.jurisdiction_verified = True
    case.judge_id = current_user.id
    db.session.commit()
    flash('Jurisdiction verified and judge assigned to case')
    return redirect(url_for('judge.view_case', case_id=case_id))


@judge_bp.route('/cases/<int:case_id>/schedule_hearing', methods=['POST'])
@login_required
def schedule_hearing(case_id):
    if getattr(current_user, 'role', None) != 'judge':
        abort(403)
    case = Case.query.get_or_404(case_id)
    when = request.form.get('scheduled_at')
    notes = request.form.get('notes')
    # Only the judge assigned to the case may schedule hearings
    if not case.judge_id:
        flash('Assign jurisdiction to this case before scheduling hearings.', 'warning')
        return redirect(url_for('judge.view_case', case_id=case_id))
    if case.judge_id != current_user.id:
        abort(403)
    try:
        scheduled_at = datetime.fromisoformat(when)
    except Exception:
        flash('Invalid date format. Use ISO format: YYYY-MM-DDTHH:MM')
        return redirect(url_for('judge.view_case', case_id=case_id))
    hearing = Hearing(case_id=case.id, judge_id=current_user.id, scheduled_at=scheduled_at, notes=notes)
    try:
        db.session.add(hearing)

        # Add a case history entry recording the scheduled hearing
        hist_details = f"Hearing scheduled at {scheduled_at.isoformat()} by Judge {current_user.name or current_user.email}."
        if notes:
            hist_details += f" Notes: {notes}"
        history = CaseHistory(case_id=case.id, actor_id=current_user.id, action='hearing_scheduled', details=hist_details)
        db.session.add(history)

        # Create Appointment entries for involved users so they see the hearing in their schedules
        notified = []
        try:
            if case.client_id:
                ap = Appointment(timestamp=scheduled_at, type='hearing', status='confirmed', notes=f'Hearing for case #{case.id}', user_id=case.client_id, case_id=case.id)
                db.session.add(ap)
                notified.append(case.client_id)
            if case.client2_id:
                ap2 = Appointment(timestamp=scheduled_at, type='hearing', status='confirmed', notes=f'Hearing for case #{case.id}', user_id=case.client2_id, case_id=case.id)
                db.session.add(ap2)
                notified.append(case.client2_id)
            if case.lawyer_id:
                ap3 = Appointment(timestamp=scheduled_at, type='hearing', status='confirmed', notes=f'Hearing for case #{case.id}', user_id=case.lawyer_id, case_id=case.id)
                db.session.add(ap3)
                notified.append(case.lawyer_id)
        except Exception:
            current_app.logger.exception('Failed to create appointment records for hearing')
        # Create a case message so participants see a notification in their case chat
        try:
            if notified:
                from models import Message
                msg = Message(content=hist_details, case_id=case.id, sender_id=current_user.id)
                db.session.add(msg)
        except Exception:
            current_app.logger.exception('Failed to create case message for hearing notification')

        db.session.commit()
        flash('Hearing scheduled and participants notified')
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Failed to schedule hearing')
        flash('Failed to schedule hearing', 'danger')
    return redirect(url_for('judge.view_case', case_id=case_id))
