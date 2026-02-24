# models.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

# Initialize the SQLAlchemy object. It will be initialized with the app later.
db = SQLAlchemy()

class User(UserMixin, db.Model):
    """
    Represents a user in the system. Can be a client, lawyer, or admin.
    """
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(50), nullable=False) # 'client', 'lawyer', 'admin'
    name = db.Column(db.String(150), nullable=True)
    bio = db.Column(db.Text, nullable=True)
    education = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = db.Column(db.Boolean, default=True, nullable=False) # For soft-deactivation by admin
    profile_pic = db.Column(db.String(200), nullable=True) # Filename for the profile picture
    
    # Lawyer-specific fields
    specialization = db.Column(db.String(200), nullable=True)
    experience_years = db.Column(db.Integer, nullable=True)
    location = db.Column(db.String(150), nullable=True)
    # Contact fields
    phone_number = db.Column(db.String(50), nullable=True)
    address = db.Column(db.String(300), nullable=True)
    bar_number = db.Column(db.String(100), unique=True, nullable=True)
    is_verified = db.Column(db.Boolean, default=False)
    # Judge-specific fields
    court_name = db.Column(db.String(200), nullable=True)
    judge_id_number = db.Column(db.String(100), unique=True, nullable=True)
    verification_document = db.Column(db.String(200), nullable=True)  # filename of uploaded verification doc
    
    # --- Relationships ---
    # A user can be a client in many cases
    cases_as_client = db.relationship('Case', foreign_keys='Case.client_id', backref='client', lazy=True)
    # If a case can have a second client, expose that relationship too
    cases_as_client2 = db.relationship('Case', foreign_keys='Case.client2_id', backref='client2', lazy=True)
    # A user can be a lawyer in many cases
    cases_as_lawyer = db.relationship('Case', foreign_keys='Case.lawyer_id', backref='lawyer', lazy=True)
    # A user can be a judge in many cases
    cases_as_judge = db.relationship('Case', foreign_keys='Case.judge_id', backref='judge', lazy=True)
    # A lawyer can receive many reviews
    reviews_received = db.relationship('Review', backref='lawyer', lazy=True)
    # A user can send many messages
    messages_sent = db.relationship('Message', foreign_keys='Message.sender_id', backref='sender', lazy=True)
    # A user can have many appointments
    appointments = db.relationship('Appointment', backref='user', lazy=True)
    # A user can upload many documents
    documents_uploaded = db.relationship('Document', backref='uploader', lazy=True)
    # A user can make many complaints
    complaints_made = db.relationship('Complaint', foreign_keys='Complaint.complainant_id', backref='complainant', lazy=True)
    # A user can be the subject of many complaints
    complaints_about = db.relationship('Complaint', foreign_keys='Complaint.about_user_id', backref='about_user', lazy=True)

    def set_password(self, password):
        """Hashes the password and stores it."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Checks if the provided password matches the stored hash."""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        """Provides a developer-friendly representation of the User object."""
        return f'<User {self.email}>'

class Case(db.Model):
    """
    Represents a legal case submitted by a client and handled by a lawyer.
    """
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(50), default='open') # open, accepted, closed
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Foreign Keys
    client_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    # Optional second client for joint cases
    client2_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    lawyer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    # Optional judge assigned to the case
    judge_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    # Whether a judge has verified jurisdiction for this case
    jurisdiction_verified = db.Column(db.Boolean, default=False)
    
    # --- Relationships ---
    # A case can have many messages
    messages = db.relationship('Message', backref='case', lazy=True, cascade="all, delete-orphan")
    # A case can have one review (after it's closed)
    review = db.relationship('Review', uselist=False, backref='case_ref', lazy=True, cascade="all, delete-orphan")
    # A case can have many documents
    documents = db.relationship('Document', backref='case', lazy=True, cascade="all, delete-orphan")
    # A case can have many appointments
    appointments = db.relationship('Appointment', backref='case', lazy=True, cascade="all, delete-orphan")
    # A case can have many hearings
    hearings = db.relationship('Hearing', backref='case', lazy=True, cascade="all, delete-orphan")
    # A case can have many history entries (actions, status changes, notes)
    histories = db.relationship('CaseHistory', backref='case', lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Case {self.id}>'

    @property
    def status_changed_date(self):
        """Return a datetime representing when the case status last changed.
        If no dedicated field exists, fall back to the case `timestamp`.
        This is a read-only convenience for templates.
        """
        # Prefer an explicit DB field if added later (e.g., status_changed_at)
        return getattr(self, 'status_changed_at', None) or self.timestamp

    @property
    def status_change_reason(self):
        """Return a short reason for the status change if available.
        Falls back to an empty string when not present.
        """
        return getattr(self, 'status_change_note', None) or ''

    @property
    def lawyer_assigned_date(self):
        """Return when a lawyer was assigned to the case.
        Falls back to `lawyer_assigned_at` if present, otherwise the case `timestamp`.
        """
        return getattr(self, 'lawyer_assigned_at', None) or self.timestamp

    @property
    def closed_date(self):
        """Return when the case was closed. Falls back to `closed_at` if present,
        otherwise use the case `timestamp` as a safe default for templates.
        """
        return getattr(self, 'closed_at', None) or self.timestamp

class Message(db.Model):
    """
    Represents a message sent within a case's chat.
    """
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Foreign Keys
    case_id = db.Column(db.Integer, db.ForeignKey('case.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    def __repr__(self):
        return f'<Message {self.id}>'


class MessageRead(db.Model):
    """Tracks which users have read which messages without modifying the
    original Message model. This avoids schema changes to Message while
    allowing per-user read state.
    """
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey('message.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    read_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    message = db.relationship('Message', backref='reads')
    user = db.relationship('User', backref='message_reads')

    def __repr__(self):
        return f'<MessageRead msg={self.message_id} user={self.user_id}>'

class Review(db.Model):
    """
    Represents a review a client leaves for a lawyer after a case is closed.
    """
    id = db.Column(db.Integer, primary_key=True)
    rating = db.Column(db.Integer, nullable=False) # 1-5
    comment = db.Column(db.Text, nullable=True)
    
    # Foreign Keys
    lawyer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    case_id = db.Column(db.Integer, db.ForeignKey('case.id'), nullable=False)

    def __repr__(self):
        return f'<Review {self.id}>'

class Appointment(db.Model):
    """
    Represents an appointment scheduled between a client and a lawyer.
    """
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, nullable=False)
    type = db.Column(db.String(50), nullable=False) # 'video', 'phone', 'in-person'
    status = db.Column(db.String(50), default='requested') # 'requested', 'confirmed', 'completed', 'cancelled'
    notes = db.Column(db.Text, nullable=True)
    
    # Foreign Keys
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    case_id = db.Column(db.Integer, db.ForeignKey('case.id'), nullable=False)

    def __repr__(self):
        return f'<Appointment {self.id}>'

class Document(db.Model):
    """
    Represents a document uploaded and shared within a case.
    """
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False) # Secure filename on server
    original_filename = db.Column(db.String(200), nullable=False) # Original filename from user
    description = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Foreign Keys
    case_id = db.Column(db.Integer, db.ForeignKey('case.id'), nullable=False)
    uploader_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    def __repr__(self):
        return f'<Document {self.id}>'


class Hearing(db.Model):
    """
    Represents a hearing scheduled by a judge for a case.
    """
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey('case.id'), nullable=False)
    judge_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    scheduled_at = db.Column(db.DateTime, nullable=False)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(50), default='scheduled')  # scheduled, held, cancelled

    # Relationship back to judge (user)
    judge = db.relationship('User', foreign_keys=[judge_id], backref='hearings_held')

    def __repr__(self):
        return f'<Hearing {self.id} case={self.case_id} judge={self.judge_id}>'


class CaseHistory(db.Model):
    """
    Represents a chronological entry for a case. Lawyers, clients, or system
    actions can append entries describing status changes, notes, or other events.
    """
    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(db.Integer, db.ForeignKey('case.id'), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # who performed the action
    action = db.Column(db.String(200), nullable=False)  # short label e.g. 'status_changed'
    details = db.Column(db.Text, nullable=True)  # optional longer explanation
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    actor = db.relationship('User', foreign_keys=[actor_id], backref='case_histories')

    def __repr__(self):
        return f'<CaseHistory case={self.case_id} action={self.action} at={self.timestamp}>'

class Complaint(db.Model):
    """
    Represents a complaint filed by one user against another.
    """
    id = db.Column(db.Integer, primary_key=True)
    subject = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(50), default='open') # 'open', 'investigating', 'resolved'
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Foreign Keys
    complainant_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    about_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    def __repr__(self):
        return f'<Complaint {self.id}>'


class JudgeClient(db.Model):
    """
    Associates a judge user with client users that the judge has registered
    or manages. This allows judges to register clients they will create
    via the judge UI without modifying the main User table schema.
    """
    id = db.Column(db.Integer, primary_key=True)
    judge_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    judge = db.relationship('User', foreign_keys=[judge_id], backref='managed_clients', lazy=True)
    client = db.relationship('User', foreign_keys=[client_id], backref='managed_by_judge', lazy=True)

    def __repr__(self):
        return f'<JudgeClient judge={self.judge_id} client={self.client_id}>'


class AccessLog(db.Model):
    """Audit log for accesses to sensitive fields.

    Records when a user (viewer) accessed or was allowed to view sensitive
    information about another user (target_user).
    """
    id = db.Column(db.Integer, primary_key=True)
    viewer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(100), nullable=False)  # e.g., 'view_sensitive'
    ip_address = db.Column(db.String(45), nullable=True)
    user_agent = db.Column(db.String(300), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    viewer = db.relationship('User', foreign_keys=[viewer_id], backref='accesses_made')
    target_user = db.relationship('User', foreign_keys=[target_user_id], backref='accesses_received')

    def __repr__(self):
        return f'<AccessLog viewer={self.viewer_id} target={self.target_user_id} action={self.action}>'


class RateLimitLog(db.Model):
    """Persistent record of rate-limited requests for admin review."""
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(45), nullable=True)
    session_cookie = db.Column(db.String(300), nullable=True)
    endpoint = db.Column(db.String(200), nullable=False)
    user_agent = db.Column(db.String(300), nullable=True)
    referer = db.Column(db.String(300), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<RateLimitLog ip={self.ip_address} endpoint={self.endpoint} at={self.timestamp}>'