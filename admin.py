# admin.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from models import db, User, Case, Complaint, RateLimitLog

# --- Decorator for Admin Access ---
def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('Admin access required.')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

admin_bp = Blueprint('admin', __name__)

# --- Admin Dashboard ---
@admin_bp.route('/dashboard')
@login_required
@admin_required
def dashboard():
    total_users = User.query.count()
    total_cases = Case.query.count()
    pending_lawyers = User.query.filter_by(role='lawyer', is_verified=False).count()
    open_complaints = Complaint.query.filter_by(status='open').count()
    return render_template('admin/dashboard.html', 
                         total_users=total_users, 
                         total_cases=total_cases, 
                         pending_lawyers=pending_lawyers,
                         open_complaints=open_complaints)

# --- Lawyer Verification ---
@admin_bp.route('/verify_lawyers')
@login_required
@admin_required
def verify_lawyers():
    pending_lawyers = User.query.filter_by(role='lawyer', is_verified=False).all()
    return render_template('admin/verify_lawyers.html', lawyers=pending_lawyers)

@admin_bp.route('/approve_lawyer/<int:lawyer_id>')
@login_required
@admin_required
def approve_lawyer(lawyer_id):
    lawyer = db.session.get(User, lawyer_id)
    if lawyer:
        lawyer.is_verified = True
        db.session.commit()
        flash(f'Lawyer {lawyer.name} has been approved.')
    return redirect(url_for('admin.verify_lawyers'))

@admin_bp.route('/reject_lawyer/<int:lawyer_id>')
@login_required
@admin_required
def reject_lawyer(lawyer_id):
    lawyer = db.session.get(User, lawyer_id)
    if lawyer:
        db.session.delete(lawyer)
        db.session.commit()
        flash(f'Lawyer {lawyer.name} has been rejected and removed.')
    return redirect(url_for('admin.verify_lawyers'))

# --- User Management ---
@admin_bp.route('/manage_users')
@login_required
@admin_required
def manage_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/manage_users.html', users=users)

@admin_bp.route('/edit_user/<int:user_id>', methods=['GET', 'POST'])
@login_required
@admin_required
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        user.name = request.form.get('name')
        user.email = request.form.get('email')
        user.role = request.form.get('role')
        user.is_active = 'is_active' in request.form # Checkbox handling
        
        # Only update password if a new one is provided
        new_password = request.form.get('password')
        if new_password:
            user.set_password(new_password)
            
        db.session.commit()
        flash(f'User {user.name} has been updated.')
        return redirect(url_for('admin.manage_users'))
        
    return render_template('admin/edit_user.html', user=user)

@admin_bp.route('/deactivate_user/<int:user_id>')
@login_required
@admin_required
def deactivate_user(user_id):
    user = db.session.get(User, user_id)
    if user and user.id != current_user.id: # Prevent admin from deactivating themselves
        user.is_active = not user.is_active # Toggle active status
        status = "activated" if user.is_active else "deactivated"
        db.session.commit()
        flash(f'User {user.name} has been {status}.')
    return redirect(url_for('admin.manage_users'))

# --- Case and Complaint Management ---
@admin_bp.route('/view_cases')
@login_required
@admin_required
def view_cases():
    cases = Case.query.order_by(Case.timestamp.desc()).all()
    return render_template('admin/view_cases.html', cases=cases)


@admin_bp.route('/case/<int:case_id>')
@login_required
@admin_required
def view_case(case_id):
    """Admin-facing single case view for investigation and management."""
    case = Case.query.get_or_404(case_id)
    return render_template('admin/view_case_detail.html', case=case)


@admin_bp.route('/rate_limits')
@login_required
@admin_required
def rate_limits():
    """Shows recent rate-limited events for admin review."""
    logs = RateLimitLog.query.order_by(RateLimitLog.timestamp.desc()).limit(200).all()
    return render_template('admin/rate_limits.html', logs=logs)

@admin_bp.route('/complaints')
@login_required
@admin_required
def complaints():
    complaints = Complaint.query.order_by(Complaint.timestamp.desc()).all()
    return render_template('admin/complaints.html', complaints=complaints)

@admin_bp.route('/view_complaint/<int:complaint_id>')
@login_required
@admin_required
def view_complaint(complaint_id):
    complaint = Complaint.query.get_or_404(complaint_id)
    return render_template('admin/view_complaint_details.html', complaint=complaint)

@admin_bp.route('/resolve_complaint/<int:complaint_id>')
@login_required
@admin_required
def resolve_complaint(complaint_id):
    complaint = db.session.get(Complaint, complaint_id)
    if complaint:
        complaint.status = 'resolved'
        db.session.commit()
        flash(f'Complaint #{complaint.id} has been marked as resolved.')
    return redirect(url_for('admin.complaints'))
