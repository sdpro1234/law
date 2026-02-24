# auth.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_user, logout_user
from models import db, User
from werkzeug.utils import secure_filename
import os

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('index'))
        flash('Invalid credentials')
    return render_template('auth/login.html')

@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        role = request.form.get('role')
        name = request.form.get('name')

        # Judge-specific fields
        court_name = request.form.get('court_name')
        judge_id_number = request.form.get('judge_id_number')
        verification_file = request.files.get('verification_document')

        if User.query.filter_by(email=email).first():
            flash('Email already exists.')
            return redirect(url_for('auth.register'))

        new_user = User(email=email, role=role, name=name)
        # populate judge-specific fields when role is judge
        if role == 'judge':
            new_user.court_name = court_name
            new_user.judge_id_number = judge_id_number
            # Save uploaded verification document if provided
            if verification_file and verification_file.filename:
                upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads')
                os.makedirs(upload_folder, exist_ok=True)
                filename = secure_filename(verification_file.filename)
                # prefix with timestamp/user to reduce collisions
                import time
                unique_name = f"judge_ver_{int(time.time())}_{filename}"
                save_path = os.path.join(upload_folder, unique_name)
                verification_file.save(save_path)
                new_user.verification_document = unique_name
        new_user.set_password(password)
        
        if role == 'client':
            new_user.is_verified = True
        # Judges require admin verification; keep is_verified default False

        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        
        flash('Registration successful! Please complete your profile.')
        return redirect(url_for('index'))
    return render_template('auth/register.html')

@auth_bp.route('/logout')
def logout():
    from flask_login import logout_user
    logout_user()
    return redirect(url_for('index'))