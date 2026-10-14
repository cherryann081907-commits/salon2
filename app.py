"""
Lumora Salon — Multi-User Web-Based Management System
Built with Python, Flask, and Object-Oriented Programming.
Storage: in-memory Python lists only (no database), as required.

Key behaviours
  * Slot-based scheduling (30-minute grid) with overlap-aware double-booking prevention
  * Every booking is validated server-side inside a lock, so two clients can never
    take the same slot — even if they click at the same moment
  * Live availability API polled by the browser so booked slots disappear instantly
  * Full admin CRUD over bookings, schedules (time off), stylists, clients and accounts
  * Salted password hashing, password change for every role, admin password reset
  * CSRF protection on every POST request
"""

import os
import re
import math
import secrets
import hmac
import threading
from functools import wraps
from datetime import datetime, date, timedelta

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify
)
from markupsafe import Markup
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("LUMORA_SECRET_KEY", "lumora-salon-secret-key-change-in-production")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# =========================================================
#  SALON CONFIGURATION
# =========================================================
SLOT_MINUTES = 30                 # booking grid
SALON_OPEN = "09:00"
SALON_CLOSE = "19:00"
DEFAULT_WORK_DAYS = [1, 2, 3, 4, 5, 6]   # Mon=0 … Sun=6  → open Tue–Sun
MAX_ADVANCE_DAYS = 120
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# One lock guards every read-validate-write sequence on bookings.
booking_lock = threading.RLock()
# Bumped on every change so browsers can cheaply detect "something changed".
_data_version = {"v": 0}


def bump_version():
    _data_version["v"] += 1


# =========================================================
#  SMALL HELPERS
# =========================================================
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^[0-9+()\-.\s]{7,20}$")


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_minutes(hhmm):
    """'09:30' -> 570. Returns None when the text is not a valid HH:MM."""
    if not isinstance(hhmm, str) or not re.fullmatch(r"\d{2}:\d{2}", hhmm):
        return None
    h, m = int(hhmm[:2]), int(hhmm[3:])
    if h > 23 or m > 59:
        return None
    return h * 60 + m


def from_minutes(total):
    return f"{total // 60:02d}:{total % 60:02d}"


def fmt12(hhmm):
    m = to_minutes(hhmm)
    if m is None:
        return hhmm
    h, mm = divmod(m, 60)
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d} {suffix}"


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def password_errors(pw):
    errors = []
    if not pw or len(pw) < 8:
        errors.append("Password must be at least 8 characters.")
    elif not (re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)):
        errors.append("Password must contain at least one letter and one number.")
    if pw and len(pw) > 72:
        errors.append("Password is too long (72 characters max).")
    return errors


def profile_errors(name, email, phone):
    errors = []
    if not name or not (2 <= len(name) <= 60):
        errors.append("Name must be between 2 and 60 characters.")
    if not email or not EMAIL_RE.match(email):
        errors.append("Please enter a valid email address.")
    if phone and not PHONE_RE.match(phone):
        errors.append("Phone number looks invalid (digits, spaces, + ( ) - only).")
    return errors


def wants_json():
    return (request.headers.get("X-Requested-With") == "fetch"
            or request.accept_mimetypes.best == "application/json")


def safe_next(default_endpoint):
    nxt = request.form.get("next", "")
    if nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return url_for(default_endpoint)


# =========================================================
#  OOP MODELS
# =========================================================

class User:
    """Base user class. Demonstrates encapsulation: the password is stored only as a
    salted hash in a name-mangled private attribute."""

    _id_counter = 1

    def __init__(self, name, email, password, phone=""):
        self.id = User._id_counter
        User._id_counter += 1
        self.name = name
        self.email = email.lower().strip()
        self.__password_hash = generate_password_hash(password, method="pbkdf2:sha256")
        self.phone = phone
        self.created_at = datetime.now()
        self.active = True

    def check_password(self, raw_password):
        return check_password_hash(self.__password_hash, raw_password or "")

    def set_password(self, raw_password):
        self.__password_hash = generate_password_hash(raw_password, method="pbkdf2:sha256")

    @property
    def role(self):
        return "user"

    @property
    def role_label(self):
        return "User"

    def dashboard_endpoint(self):
        return "login"

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "email": self.email,
            "phone": self.phone, "role": self.role,
            "role_label": self.role_label, "active": self.active,
        }


class Admin(User):
    @property
    def role(self):
        return "admin"

    @property
    def role_label(self):
        return "Administrator"

    def dashboard_endpoint(self):
        return "admin_dashboard"


class Stylist(User):
    def __init__(self, name, email, password, phone="", specialty="Hair Styling", bio=""):
        super().__init__(name, email, password, phone)
        self.specialty = specialty
        self.bio = bio
        self.available = True                    # accepting new bookings?
        self.work_start = SALON_OPEN
        self.work_end = SALON_CLOSE
        self.work_days = list(DEFAULT_WORK_DAYS)

    @property
    def role(self):
        return "stylist"

    @property
    def role_label(self):
        return "Stylist"

    def dashboard_endpoint(self):
        return "stylist_dashboard"

    @property
    def work_days_label(self):
        return ", ".join(DAY_NAMES[d] for d in sorted(self.work_days)) or "None"

    def to_dict(self):
        data = super().to_dict()
        data.update({
            "specialty": self.specialty, "bio": self.bio, "available": self.available,
            "work_start": self.work_start, "work_end": self.work_end,
            "work_days": self.work_days,
        })
        return data


class Customer(User):
    def __init__(self, name, email, password, phone=""):
        super().__init__(name, email, password, phone)
        self.loyalty_points = 0

    @property
    def role(self):
        return "customer"

    @property
    def role_label(self):
        return "Client"

    def dashboard_endpoint(self):
        return "customer_dashboard"


class Service:
    _id_counter = 1

    def __init__(self, name, category, price, duration, description=""):
        self.id = Service._id_counter
        Service._id_counter += 1
        self.name = name
        self.category = category
        self.price = price
        self.duration = duration  # minutes
        self.description = description
        self.active = True

    def to_dict(self):
        return {"id": self.id, "name": self.name, "category": self.category,
                "price": self.price, "duration": self.duration,
                "description": self.description, "active": self.active}


class Appointment:
    _id_counter = 1
    STATUSES = ["Pending", "Confirmed", "Completed", "Cancelled"]

    def __init__(self, customer_id, stylist_id, service_id, appt_date, appt_time, notes=""):
        self.id = Appointment._id_counter
        Appointment._id_counter += 1
        self.customer_id = customer_id
        self.stylist_id = stylist_id
        self.service_id = service_id
        self.date = appt_date          # "YYYY-MM-DD"
        self.time = appt_time          # "HH:MM"
        self.status = "Pending"
        self.notes = notes
        self.created_at = datetime.now()


class TimeOff:
    """A blocked period in a stylist's schedule (day off, break, training…)."""
    _id_counter = 1

    def __init__(self, stylist_id, day, start, end, reason=""):
        self.id = TimeOff._id_counter
        TimeOff._id_counter += 1
        self.stylist_id = stylist_id
        self.date = day            # "YYYY-MM-DD"
        self.start = start         # "HH:MM"
        self.end = end             # "HH:MM"
        self.reason = reason

    @property
    def all_day(self):
        return self.start == "00:00" and self.end == "23:59"


# =========================================================
#  REPOSITORIES  (generic CRUD base + specialised subclasses)
# =========================================================

class Repository:
    """Generic in-memory repository wrapping a private Python list."""

    def __init__(self):
        self._items = []

    def add(self, item):
        self._items.append(item)
        bump_version()
        return item

    def all(self):
        return list(self._items)

    def get(self, item_id):
        item_id = to_int(item_id)
        if item_id is None:
            return None
        return next((i for i in self._items if i.id == item_id), None)

    def delete(self, item_id):
        item = self.get(item_id)
        if item:
            self._items.remove(item)
            bump_version()
            return True
        return False

    def update(self, item_id, **kwargs):
        item = self.get(item_id)
        if not item:
            return None
        for key, value in kwargs.items():
            if hasattr(item, key):
                setattr(item, key, value)
        bump_version()
        return item


class UserRepository(Repository):
    def add(self, item):
        if self.find_by_email(item.email):
            raise ValueError("An account with this email already exists.")
        return super().add(item)

    def find_by_email(self, email, exclude_id=None):
        email = (email or "").lower().strip()
        return next((u for u in self._items
                     if u.email == email and u.id != exclude_id), None)

    def by_role(self, role):
        return [u for u in self._items if u.role == role]


class ServiceRepository(Repository):
    def active_services(self):
        return [s for s in self._items if s.active]


class AppointmentRepository(Repository):
    def for_customer(self, customer_id):
        return sorted([a for a in self._items if a.customer_id == customer_id],
                      key=lambda a: (a.date, a.time), reverse=True)

    def for_stylist(self, stylist_id):
        return sorted([a for a in self._items if a.stylist_id == stylist_id],
                      key=lambda a: (a.date, a.time))

    def active_on(self, stylist_id, day):
        return [a for a in self._items
                if a.stylist_id == stylist_id and a.date == day and a.status != "Cancelled"]

    def remove_where(self, **match):
        doomed = [a for a in self._items
                  if all(getattr(a, k) == v for k, v in match.items())]
        for a in doomed:
            self._items.remove(a)
        if doomed:
            bump_version()
        return len(doomed)


class TimeOffRepository(Repository):
    def for_stylist(self, stylist_id):
        return sorted([t for t in self._items if t.stylist_id == stylist_id],
                      key=lambda t: (t.date, t.start))

    def on_day(self, stylist_id, day):
        return [t for t in self._items if t.stylist_id == stylist_id and t.date == day]

    def remove_where(self, **match):
        doomed = [t for t in self._items
                  if all(getattr(t, k) == v for k, v in match.items())]
        for t in doomed:
            self._items.remove(t)
        if doomed:
            bump_version()
        return len(doomed)


# =========================================================
#  IN-MEMORY DATA STORE (Python lists only — no database)
# =========================================================

users = UserRepository()
services = ServiceRepository()
appointments = AppointmentRepository()
time_offs = TimeOffRepository()


# =========================================================
#  SCHEDULER — the single source of truth for slot availability
# =========================================================

class Scheduler:
    """Computes the status of every slot for a stylist on a given day.

    Slot statuses
      available  – can be booked
      booked     – occupied by another (non-cancelled) appointment
      blocked    – stylist time-off / break
      past       – already gone
      too_long   – free, but the chosen service would run into a booked/blocked slot
                   or past closing time
    """

    @staticmethod
    def slots_needed(duration):
        return max(1, math.ceil((duration or SLOT_MINUTES) / SLOT_MINUTES))

    def service_slots(self, service_id):
        svc = services.get(service_id)
        return self.slots_needed(svc.duration if svc else SLOT_MINUTES)

    def grid(self, stylist):
        start, end = to_minutes(stylist.work_start), to_minutes(stylist.work_end)
        return list(range(start, end, SLOT_MINUTES))

    def day_slots(self, stylist, day, duration=None, exclude_id=None):
        d = parse_date(day)
        if d is None:
            return []
        if d.weekday() not in stylist.work_days:
            return []

        now = datetime.now()
        occupied = {}
        for a in appointments.active_on(stylist.id, day):
            if a.id == exclude_id:
                continue
            for k in range(self.service_slots(a.service_id)):
                occupied.setdefault(to_minutes(a.time) + k * SLOT_MINUTES, a)

        blocked = {}
        for t in time_offs.on_day(stylist.id, day):
            t_start, t_end = to_minutes(t.start), to_minutes(t.end)
            for m in self.grid(stylist):
                if m < t_end and m + SLOT_MINUTES > t_start:
                    blocked.setdefault(m, t)

        grid = self.grid(stylist)
        base = {}
        for m in grid:
            if d < now.date() or (d == now.date() and m <= now.hour * 60 + now.minute):
                base[m] = "past"
            elif m in occupied:
                base[m] = "booked"
            elif m in blocked:
                base[m] = "blocked"
            else:
                base[m] = "available"

        need = self.slots_needed(duration) if duration else 1
        slots = []
        for m in grid:
            status = base[m]
            if status == "available" and need > 1:
                for k in range(1, need):
                    if base.get(m + k * SLOT_MINUTES) != "available":
                        status = "too_long"
                        break
            slots.append({
                "minutes": m, "time": from_minutes(m), "label": fmt12(from_minutes(m)),
                "status": status, "appt": occupied.get(m), "timeoff": blocked.get(m),
            })
        return slots

    def check_slot(self, stylist, day, time_s, duration, exclude_id=None):
        """Returns (ok, message)."""
        d = parse_date(day)
        if d is None:
            return False, "Please choose a valid date."
        if d.weekday() not in stylist.work_days:
            return False, f"{stylist.name} doesn't work on {DAY_NAMES[d.weekday()]}s."
        slots = self.day_slots(stylist, day, duration, exclude_id)
        slot = next((s for s in slots if s["time"] == time_s), None)
        if slot is None:
            return False, "That time is outside this stylist's working hours."
        messages = {
            "available": "",
            "booked": f"{fmt12(time_s)} is already booked with {stylist.name}. Please pick another time.",
            "blocked": f"{stylist.name} is unavailable at {fmt12(time_s)}.",
            "past": "That time has already passed.",
            "too_long": "That service needs more time than is free at this slot.",
        }
        if slot["status"] == "available":
            return True, ""
        return False, messages[slot["status"]]

    def overlaps(self, stylist_id, day, time_s, duration, exclude_id=None):
        """True if [time, time+duration) collides with another active appointment."""
        start = to_minutes(time_s)
        end = start + self.slots_needed(duration) * SLOT_MINUTES
        for a in appointments.active_on(stylist_id, day):
            if a.id == exclude_id:
                continue
            a_start = to_minutes(a.time)
            a_end = a_start + self.service_slots(a.service_id) * SLOT_MINUTES
            if start < a_end and a_start < end:
                return True
        return False

    def customer_clash(self, customer_id, day, time_s, duration, exclude_id=None):
        start = to_minutes(time_s)
        end = start + self.slots_needed(duration) * SLOT_MINUTES
        for a in appointments.all():
            if (a.customer_id != customer_id or a.date != day
                    or a.status == "Cancelled" or a.id == exclude_id):
                continue
            a_start = to_minutes(a.time)
            a_end = a_start + self.service_slots(a.service_id) * SLOT_MINUTES
            if start < a_end and a_start < end:
                return True
        return False


scheduler = Scheduler()


def validate_booking(customer_id, stylist_id, service_id, day, time_s,
                     exclude_id=None, check_schedule=True):
    """Validates a booking request. Returns (errors, customer, stylist, service)."""
    errors = []
    customer = users.get(customer_id)
    stylist = users.get(stylist_id)
    service = services.get(service_id)

    if not customer or customer.role != "customer":
        errors.append("Please choose a valid client.")
    if not service or not service.active:
        errors.append("Please choose a valid service.")
    if not stylist or stylist.role != "stylist" or not stylist.active:
        errors.append("Please choose a valid stylist.")
        stylist = None
    elif not stylist.available:
        errors.append(f"{stylist.name} isn't accepting bookings right now.")

    d = parse_date(day)
    if not day:
        errors.append("Please choose a date.")
    elif d is None:
        errors.append("That date isn't valid.")
    elif d < date.today():
        errors.append("Please choose a date today or later.")
    elif d > date.today() + timedelta(days=MAX_ADVANCE_DAYS):
        errors.append(f"Bookings can be made up to {MAX_ADVANCE_DAYS} days ahead.")

    if not time_s:
        errors.append("Please select a time.")
    elif to_minutes(time_s) is None:
        errors.append("That time isn't valid.")

    if errors:
        return errors, customer, stylist, service

    if stylist and service and check_schedule:
        ok, msg = scheduler.check_slot(stylist, day, time_s, service.duration, exclude_id)
        if not ok:
            errors.append(msg)
    if not errors and customer and service:
        if scheduler.customer_clash(customer.id, day, time_s, service.duration, exclude_id):
            errors.append("This client already has an appointment overlapping that time.")
    return errors, customer, stylist, service


# =========================================================
#  SEED DATA
# =========================================================

def next_open_day(start, stylist):
    d = start
    for _ in range(14):
        if d.weekday() in stylist.work_days:
            return d
        d += timedelta(days=1)
    return start


def seed_data():
    users.add(Admin("Elena Novak", "admin@lumora.com", "admin123", "555-0100"))

    stylists_seed = [
        ("Maya Chen", "maya@lumora.com", "Color Specialist",
         "10+ years crafting balayage and vivid color transformations."),
        ("Jordan Reyes", "jordan@lumora.com", "Precision Cuts",
         "Known for sharp, modern cuts tailored to face shape."),
        ("Aisha Bello", "aisha@lumora.com", "Bridal & Updos",
         "Bridal styling specialist with an eye for elegant detail."),
    ]
    for name, email, spec, bio in stylists_seed:
        users.add(Stylist(name, email, "stylist123", "555-0101", spec, bio))

    for name, email in [("Sophie Turner", "sophie@example.com"), ("Liam Carter", "liam@example.com")]:
        users.add(Customer(name, email, "client123", "555-0202"))

    services_seed = [
        ("Signature Haircut", "Hair", 65, 45, "A tailored cut finished with a wash and style."),
        ("Balayage Color", "Color", 180, 150, "Hand-painted, sun-kissed dimensional color."),
        ("Full Blowout", "Styling", 45, 40, "Volumizing wash and professional blow-dry."),
        ("Bridal Updo", "Styling", 120, 90, "Elegant occasion styling with a pre-event trial."),
        ("Classic Manicure", "Nails", 35, 30, "Shape, cuticle care, and polish of your choice."),
        ("Gel Pedicure", "Nails", 55, 50, "Long-lasting gel finish with a relaxing soak."),
        ("Deep Conditioning Treatment", "Hair", 40, 30, "Restorative mask for strength and shine."),
        ("Keratin Smoothing", "Hair", 220, 180, "Frizz-free, smooth results that last for months."),
    ]
    for name, cat, price, dur, desc in services_seed:
        services.add(Service(name, cat, price, dur, desc))

    st = users.by_role("stylist")
    cu = users.by_role("customer")
    sv = services.all()
    today = date.today()

    sample = [
        (cu[0], st[0], sv[1], today + timedelta(days=1), "10:00", "Confirmed"),
        (cu[0], st[2], sv[3], today + timedelta(days=3), "13:30", "Pending"),
        (cu[1], st[1], sv[0], today - timedelta(days=2), "09:00", "Completed"),
        (cu[1], st[0], sv[6], today + timedelta(days=5), "15:00", "Pending"),
    ]
    for cust, stylist, svc, d, t, status in sample:
        if d >= today:
            d = next_open_day(d, stylist)
        a = appointments.add(Appointment(cust.id, stylist.id, svc.id, d.isoformat(), t))
        a.status = status

    # a sample day-off so the schedule board shows a blocked period
    d = next_open_day(today + timedelta(days=2), st[1])
    time_offs.add(TimeOff(st[1].id, d.isoformat(), "12:00", "13:00", "Lunch break"))


seed_data()


# =========================================================
#  AUTH / CSRF HELPERS
# =========================================================

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    user = users.get(uid)
    if user and not user.active:
        session.clear()
        return None
    return user


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify(ok=False, message="Please log in."), 401
            flash("Please log in to continue.", "error")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                if wants_json():
                    return jsonify(ok=False, message="Please log in."), 401
                flash("Please log in to continue.", "error")
                return redirect(url_for("login"))
            if user.role not in roles:
                if wants_json():
                    return jsonify(ok=False, message="Not allowed."), 403
                flash("You don't have access to that page.", "error")
                return redirect(url_for(user.dashboard_endpoint()))
            return f(*args, **kwargs)
        return wrapper
    return decorator


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def csrf_field():
    return Markup(f'<input type="hidden" name="_csrf" value="{csrf_token()}">')


@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("_csrf") or request.headers.get("X-CSRFToken") or ""
        if not hmac.compare_digest(sent, session.get("_csrf", "")):
            if wants_json():
                return jsonify(ok=False, message="Your session expired. Please refresh the page."), 400
            flash("Your session expired. Please try again.", "error")
            return redirect(request.referrer or url_for("index"))


@app.after_request
def no_cache_api(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.context_processor
def inject_globals():
    return {
        "current_user": current_user(), "current_year": datetime.now().year,
        "csrf_token": csrf_token, "csrf_field": csrf_field,
        "SALON_OPEN": SALON_OPEN, "SALON_CLOSE": SALON_CLOSE,
        "DAY_NAMES": DAY_NAMES,
    }


@app.template_filter("t12")
def t12_filter(value):
    return fmt12(value)


@app.template_filter("dlabel")
def dlabel_filter(value):
    d = parse_date(value)
    return d.strftime("%a, %b %d").replace(" 0", " ") if d else value


def enrich_appointment(a):
    cust = users.get(a.customer_id)
    stylist = users.get(a.stylist_id)
    service = services.get(a.service_id)
    duration = service.duration if service else 0
    end = from_minutes(to_minutes(a.time) + duration) if duration else a.time
    return {
        "obj": a, "id": a.id, "date": a.date, "time": a.time, "end": end,
        "status": a.status, "notes": a.notes,
        "customer_id": a.customer_id, "stylist_id": a.stylist_id, "service_id": a.service_id,
        "customer_name": cust.name if cust else "Unknown",
        "stylist_name": stylist.name if stylist else "Unknown",
        "service_name": service.name if service else "Unknown",
        "service_price": service.price if service else 0,
        "duration": duration,
    }


# =========================================================
#  PUBLIC ROUTES
# =========================================================

@app.route("/")
def index():
    return render_template(
        "index.html",
        services=services.active_services()[:6],
        stylists=[s for s in users.by_role("stylist") if s.active],
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for(current_user().dashboard_endpoint()))

    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        user = users.find_by_email(email)
        if user and user.check_password(password):
            if not user.active:
                flash("This account has been deactivated. Please contact the salon.", "error")
                return render_template("login.html")
            session.clear()
            session["user_id"] = user.id
            flash(f"Welcome back, {user.name.split()[0]}.", "success")
            return redirect(url_for(user.dashboard_endpoint()))
        flash("Incorrect email or password. Please try again.", "error")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for(current_user().dashboard_endpoint()))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = profile_errors(name, email, phone) + password_errors(password)
        if password != confirm:
            errors.append("Passwords do not match.")
        if email and users.find_by_email(email):
            errors.append("An account with this email already exists.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("register.html", form=request.form)

        customer = Customer(name, email, password, phone)
        users.add(customer)
        session.clear()
        session["user_id"] = customer.id
        flash("Account created. Welcome to Lumora!", "success")
        return redirect(url_for("customer_dashboard"))

    return render_template("register.html", form={})


@app.route("/logout")
def logout():
    session.clear()
    flash("You've been logged out.", "success")
    return redirect(url_for("index"))


# =========================================================
#  ACCOUNT (every role): profile + change password
# =========================================================

@app.route("/account")
@login_required
def account():
    return render_template("account.html")


@app.route("/account/profile", methods=["POST"])
@login_required
def account_profile():
    user = current_user()
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    errors = profile_errors(name, email, phone)
    if email and users.find_by_email(email, exclude_id=user.id):
        errors.append("Another account already uses that email.")
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        users.update(user.id, name=name, email=email, phone=phone)
        flash("Profile updated.", "success")
    return redirect(url_for("account"))


@app.route("/account/password", methods=["POST"])
@login_required
def account_password():
    user = current_user()
    current = request.form.get("current_password", "")
    new = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    errors = []
    if not user.check_password(current):
        errors.append("Your current password is incorrect.")
    errors += password_errors(new)
    if new != confirm:
        errors.append("The new passwords do not match.")
    if current and new == current:
        errors.append("Your new password must be different from the current one.")

    if errors:
        for e in errors:
            flash(e, "error")
    else:
        user.set_password(new)
        flash("Password changed successfully.", "success")
    return redirect(url_for("account"))


# =========================================================
#  LIVE AVAILABILITY API
# =========================================================

@app.route("/api/slots")
@login_required
def api_slots():
    user = current_user()
    stylist = users.get(request.args.get("stylist_id"))
    day = request.args.get("date", "")
    service = services.get(request.args.get("service_id"))
    exclude = to_int(request.args.get("exclude")) if user.role == "admin" else None

    if not stylist or stylist.role != "stylist":
        return jsonify(ok=False, message="Choose a stylist first."), 400
    d = parse_date(day)
    if d is None:
        return jsonify(ok=False, message="Choose a date first."), 400

    payload = {"ok": True, "version": _data_version["v"],
               "server_time": datetime.now().strftime("%I:%M:%S %p").lstrip("0"),
               "slots": [], "message": ""}
    if not stylist.available and user.role == "customer":
        payload["message"] = f"{stylist.name} isn't accepting bookings right now."
        return jsonify(payload)
    if d.weekday() not in stylist.work_days:
        payload["message"] = f"{stylist.name} is off on {DAY_NAMES[d.weekday()]}s — pick another date."
        return jsonify(payload)
    if d > date.today() + timedelta(days=MAX_ADVANCE_DAYS):
        payload["message"] = f"Bookings open up to {MAX_ADVANCE_DAYS} days ahead."
        return jsonify(payload)

    duration = service.duration if service else None
    for s in scheduler.day_slots(stylist, day, duration, exclude):
        payload["slots"].append({"time": s["time"], "label": s["label"], "status": s["status"]})
    payload["available"] = sum(1 for s in payload["slots"] if s["status"] == "available")
    return jsonify(payload)


@app.route("/api/schedule")
@role_required("admin", "stylist")
def api_schedule():
    user = current_user()
    day = request.args.get("date", date.today().isoformat())
    if parse_date(day) is None:
        return jsonify(ok=False, message="Invalid date."), 400

    stylist_list = users.by_role("stylist") if user.role == "admin" else [user]
    stylist_list = [s for s in stylist_list if s.active]
    times, columns = set(), []
    for s in stylist_list:
        slots = scheduler.day_slots(s, day)
        cells = {}
        for slot in slots:
            times.add(slot["time"])
            a, t = slot["appt"], slot["timeoff"]
            cell = {"status": slot["status"], "label": slot["label"]}
            if slot["status"] == "booked" and a:
                cell.update({
                    "appt_id": a.id,
                    "client": (users.get(a.customer_id).name if users.get(a.customer_id) else "Unknown"),
                    "service": (services.get(a.service_id).name if services.get(a.service_id) else "Unknown"),
                    "start": a.time == slot["time"], "appt_status": a.status,
                })
            if slot["status"] == "blocked" and t:
                cell.update({"reason": t.reason or "Time off", "timeoff_id": t.id})
            cells[slot["time"]] = cell
        d = parse_date(day)
        columns.append({"id": s.id, "name": s.name,
                        "off": d.weekday() not in s.work_days,
                        "closed": not s.available, "cells": cells})
    ordered = sorted(times, key=to_minutes)
    return jsonify(ok=True, date=day, times=[{"time": t, "label": fmt12(t)} for t in ordered],
                   stylists=columns, version=_data_version["v"],
                   is_admin=user.role == "admin",
                   server_time=datetime.now().strftime("%I:%M:%S %p").lstrip("0"))


# =========================================================
#  CUSTOMER ROUTES
# =========================================================

@app.route("/customer/dashboard")
@role_required("customer")
def customer_dashboard():
    user = current_user()
    my_appts = [enrich_appointment(a) for a in appointments.for_customer(user.id)]
    upcoming = [a for a in my_appts if a["status"] in ("Pending", "Confirmed")]
    history = [a for a in my_appts if a["status"] in ("Completed", "Cancelled")]
    max_day = (date.today() + timedelta(days=MAX_ADVANCE_DAYS)).isoformat()
    return render_template(
        "customer_dashboard.html",
        upcoming=upcoming, history=history,
        services=services.active_services(),
        stylists=[u for u in users.by_role("stylist") if u.available and u.active],
        today=date.today().isoformat(), max_day=max_day,
    )


@app.route("/customer/book", methods=["POST"])
@role_required("customer")
def book_appointment():
    user = current_user()
    f = request.form
    notes = f.get("notes", "").strip()[:300]

    # Validate + create atomically so two clients can never grab the same slot.
    with booking_lock:
        errors, _c, stylist, service = validate_booking(
            user.id, f.get("stylist_id"), f.get("service_id"), f.get("date"), f.get("time"))
        if not errors:
            appt = appointments.add(Appointment(
                user.id, stylist.id, service.id, f.get("date"), f.get("time"), notes))

    if errors:
        if wants_json():
            return jsonify(ok=False, message=errors[0], errors=errors), 409
        for e in errors:
            flash(e, "error")
        return redirect(url_for("customer_dashboard"))

    msg = (f"Booked! {service.name} with {stylist.name} on "
           f"{dlabel_filter(appt.date)} at {fmt12(appt.time)}.")
    if wants_json():
        return jsonify(ok=True, message=msg)
    flash(msg, "success")
    return redirect(url_for("customer_dashboard"))


@app.route("/customer/appointments/<int:appt_id>/cancel", methods=["POST"])
@role_required("customer")
def cancel_appointment(appt_id):
    user = current_user()
    appt = appointments.get(appt_id)
    if not appt or appt.customer_id != user.id:
        flash("Appointment not found.", "error")
    elif appt.status in ("Completed", "Cancelled"):
        flash(f"This appointment is already {appt.status.lower()}.", "error")
    else:
        appointments.update(appt_id, status="Cancelled")
        flash("Appointment cancelled — that time is open for others again.", "success")
    return redirect(url_for("customer_dashboard"))


# =========================================================
#  STYLIST ROUTES
# =========================================================

def set_status_checked(appt, new_status):
    """Changes status; re-activating a cancelled booking must re-check the slot."""
    with booking_lock:
        if appt.status == "Cancelled" and new_status != "Cancelled":
            service = services.get(appt.service_id)
            duration = service.duration if service else SLOT_MINUTES
            if (scheduler.overlaps(appt.stylist_id, appt.date, appt.time, duration, appt.id)
                    or scheduler.customer_clash(appt.customer_id, appt.date, appt.time, duration, appt.id)):
                return False, "That time has since been taken, so this booking can't be restored."
        appointments.update(appt.id, status=new_status)
    return True, ""


@app.route("/stylist/dashboard")
@role_required("stylist")
def stylist_dashboard():
    user = current_user()
    my_appts = [enrich_appointment(a) for a in appointments.for_stylist(user.id)]
    today_str = date.today().isoformat()
    today_appts = [a for a in my_appts if a["date"] == today_str and a["status"] != "Cancelled"]
    upcoming = [a for a in my_appts if a["date"] > today_str and a["status"] in ("Pending", "Confirmed")]
    past = [a for a in my_appts if a["date"] < today_str or a["status"] in ("Completed", "Cancelled")]
    return render_template("stylist_dashboard.html",
                           today_appts=today_appts, upcoming=upcoming, past=past, user=user)


@app.route("/stylist/appointments/<int:appt_id>/status", methods=["POST"])
@role_required("stylist")
def update_appointment_status(appt_id):
    user = current_user()
    appt = appointments.get(appt_id)
    new_status = request.form.get("status")
    if not appt or appt.stylist_id != user.id:
        flash("Appointment not found.", "error")
    elif new_status not in Appointment.STATUSES:
        flash("Invalid status.", "error")
    else:
        ok, msg = set_status_checked(appt, new_status)
        flash(f"Appointment marked as {new_status}." if ok else msg, "success" if ok else "error")
    return redirect(url_for("stylist_dashboard"))


@app.route("/stylist/availability", methods=["POST"])
@role_required("stylist")
def toggle_availability():
    user = current_user()
    users.update(user.id, available=not user.available)
    flash("You're now accepting bookings." if user.available else "New bookings are paused.", "success")
    return redirect(url_for("stylist_dashboard"))


def render_stylist_workspace(page, **extra):
    user = current_user()
    own_appts = [enrich_appointment(a) for a in appointments.for_stylist(user.id)]
    completed_transactions = [a for a in own_appts if a["status"] == "Completed"]
    return render_template(
        "stylist_workspace.html", page=page, user=user,
        clients=sorted(users.by_role("customer"), key=lambda c: c.name.lower()),
        active_clients=[c for c in users.by_role("customer") if c.active],
        own_appts=own_appts, salon_services=services.all(),
        active_services=services.active_services(),
        completed_transactions=completed_transactions,
        statuses=Appointment.STATUSES, today=date.today().isoformat(),
        max_day=(date.today() + timedelta(days=MAX_ADVANCE_DAYS)).isoformat(), **extra,
    )


@app.route("/stylist/records")
@role_required("stylist")
def stylist_records():
    return render_stylist_workspace("records")


@app.route("/stylist/add", methods=["GET", "POST"])
@role_required("stylist")
def stylist_add_information():
    if request.method == "POST":
        user = current_user()
        form = request.form
        if form.get("action") == "client":
            name = form.get("name", "").strip()
            email = form.get("email", "").strip().lower()
            phone = form.get("phone", "").strip()
            password = form.get("password", "")
            errors = profile_errors(name, email, phone) + password_errors(password)
            with booking_lock:
                if email and users.find_by_email(email):
                    errors.append("An account with this email already exists.")
                if not errors:
                    try:
                        users.add(Customer(name, email, password, phone))
                    except ValueError as exc:
                        errors.append(str(exc))
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                flash("Client added successfully.", "success")
        elif form.get("action") == "appointment":
            with booking_lock:
                errors, customer, stylist, service = validate_booking(
                    form.get("customer_id"), user.id, form.get("service_id"),
                    form.get("date"), form.get("time"))
                if customer and not customer.active:
                    errors.append("Choose an active client.")
                if not errors:
                    appointments.add(Appointment(
                        customer.id, stylist.id, service.id, form.get("date"),
                        form.get("time"), form.get("notes", "").strip()[:300]))
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                flash("Appointment created successfully.", "success")
        else:
            flash("Choose an information type to add.", "error")
        return redirect(url_for("stylist_add_information"))

    return render_stylist_workspace("add")


@app.route("/stylist/updates")
@role_required("stylist")
def stylist_updates():
    return render_stylist_workspace("updates")


@app.route("/stylist/search")
@role_required("stylist")
def stylist_search():
    query = request.args.get("q", "").strip()
    needle = query.casefold()
    client_matches = []
    appointment_matches = []
    transaction_matches = []
    if needle:
        client_matches = [c for c in users.by_role("customer")
                          if needle in " ".join((c.name, c.email, c.phone)).casefold()]
        own_appts = [enrich_appointment(a) for a in appointments.for_stylist(current_user().id)]
        for appt in own_appts:
            customer = users.get(appt["customer_id"])
            fields = (appt["date"], appt["time"], appt["service_name"],
                      appt["customer_name"], str(appt["id"]),
                      customer.email if customer else "", customer.phone if customer else "")
            if any(needle in str(value).casefold() for value in fields):
                appointment_matches.append(appt)
                if appt["status"] == "Completed":
                    transaction_matches.append(appt)
    return render_stylist_workspace(
        "search", query=query, client_matches=client_matches,
        appointment_matches=appointment_matches,
        transaction_matches=transaction_matches,
    )


@app.route("/stylist/process")
@role_required("stylist")
def stylist_process():
    return render_stylist_workspace("process")


# =========================================================
#  SCHEDULE BOARD + TIME OFF (admin: everyone, stylist: self)
# =========================================================

def timeoff_form_errors(stylist, day, start, end, all_day, exclude_id=None):
    errors = []
    d = parse_date(day)
    if d is None:
        errors.append("Please choose a valid date.")
    elif d < date.today():
        errors.append("Time off can't be set in the past.")
    if all_day:
        start, end = "00:00", "23:59"
    else:
        s, e = to_minutes(start), to_minutes(end)
        if s is None or e is None:
            errors.append("Please choose a start and end time.")
        elif e <= s:
            errors.append("End time must be after the start time.")
    if errors or not stylist:
        return errors, start, end
    s, e = to_minutes(start), to_minutes(end)
    for a in appointments.active_on(stylist.id, day):
        a_s = to_minutes(a.time)
        a_e = a_s + scheduler.service_slots(a.service_id) * SLOT_MINUTES
        if s < a_e and a_s < e:
            cust = users.get(a.customer_id)
            errors.append(f"{fmt12(a.time)} is already booked by {cust.name if cust else 'a client'} — "
                          "move or cancel that booking first.")
    for t in time_offs.on_day(stylist.id, day):
        if t.id != exclude_id and s < to_minutes(t.end) and to_minutes(t.start) < e:
            errors.append("This overlaps an existing time-off block.")
    return errors, start, end


@app.route("/schedule")
@role_required("admin", "stylist")
def schedule():
    user = current_user()
    day = request.args.get("date") or date.today().isoformat()
    if parse_date(day) is None:
        day = date.today().isoformat()
    if user.role == "admin":
        offs = sorted(time_offs.all(), key=lambda t: (t.date, t.start))
        stylist_list = [s for s in users.by_role("stylist") if s.active]
    else:
        offs = time_offs.for_stylist(user.id)
        stylist_list = [user]
    offs = [t for t in offs if t.date >= date.today().isoformat()]
    rows = [{"obj": t, "stylist": users.get(t.stylist_id)} for t in offs]
    return render_template("schedule.html", day=day, today=date.today().isoformat(),
                           timeoffs=rows, stylist_list=stylist_list,
                           active_page="schedule")


@app.route("/schedule/timeoff", methods=["POST"])
@role_required("admin", "stylist")
def timeoff_save():
    """Create (no id) or edit (with id) a time-off block."""
    user = current_user()
    f = request.form
    toff = time_offs.get(f.get("id")) if f.get("id") else None
    stylist_id = user.id if user.role == "stylist" else to_int(f.get("stylist_id"))
    stylist = users.get(stylist_id)
    if f.get("id") and (not toff or (user.role == "stylist" and toff.stylist_id != user.id)):
        flash("Time-off entry not found.", "error")
        return redirect(url_for("schedule"))
    if not stylist or stylist.role != "stylist":
        flash("Please choose a stylist.", "error")
        return redirect(url_for("schedule"))

    day, reason = f.get("date", ""), f.get("reason", "").strip()[:80]
    with booking_lock:
        errors, start, end = timeoff_form_errors(
            stylist, day, f.get("start", ""), f.get("end", ""), f.get("all_day") == "on",
            exclude_id=toff.id if toff else None)
        if not errors:
            if toff:
                time_offs.update(toff.id, stylist_id=stylist.id, date=day, start=start, end=end, reason=reason)
            else:
                time_offs.add(TimeOff(stylist.id, day, start, end, reason))
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        flash("Schedule updated — those slots now show as unavailable.", "success")
    return redirect(url_for("schedule", date=day if not errors else None))


@app.route("/schedule/timeoff/<int:off_id>/delete", methods=["POST"])
@role_required("admin", "stylist")
def timeoff_delete(off_id):
    user = current_user()
    toff = time_offs.get(off_id)
    if not toff or (user.role == "stylist" and toff.stylist_id != user.id):
        flash("Time-off entry not found.", "error")
    else:
        time_offs.delete(off_id)
        flash("Time off removed — those slots are bookable again.", "success")
    return redirect(url_for("schedule"))


# =========================================================
#  ADMIN — DASHBOARD & APPOINTMENTS
# =========================================================

@app.route("/admin/dashboard")
@role_required("admin")
def admin_dashboard():
    all_appts = [enrich_appointment(a) for a in appointments.all()]
    today_str = date.today().isoformat()
    stats = {
        "total_customers": len(users.by_role("customer")),
        "total_stylists": len(users.by_role("stylist")),
        "total_services": len(services.active_services()),
        "total_appointments": len(all_appts),
        "today": len([a for a in all_appts if a["date"] == today_str and a["status"] != "Cancelled"]),
        "pending": len([a for a in all_appts if a["status"] == "Pending"]),
        "revenue": sum(a["service_price"] for a in all_appts if a["status"] == "Completed"),
    }
    recent = sorted(all_appts, key=lambda a: (a["date"], a["time"]), reverse=True)[:8]
    return render_template("admin_dashboard.html", stats=stats, recent=recent, active_page="dashboard")


@app.route("/admin/appointments")
@role_required("admin")
def admin_appointments():
    all_appts = sorted([enrich_appointment(a) for a in appointments.all()],
                       key=lambda a: (a["date"], a["time"]), reverse=True)
    return render_template("admin_appointments.html", appts=all_appts,
                           statuses=Appointment.STATUSES, active_page="appointments")


def appointment_form_context(appt=None, form=None):
    return dict(
        appt=appt, form=form or {},
        customers=[u for u in users.by_role("customer") if u.active],
        stylists=[u for u in users.by_role("stylist") if u.active],
        services=[s for s in services.all() if s.active or (appt and s.id == appt.service_id)],
        statuses=Appointment.STATUSES, today=date.today().isoformat(),
        max_day=(date.today() + timedelta(days=MAX_ADVANCE_DAYS)).isoformat(),
        active_page="appointments",
    )


@app.route("/admin/appointments/new", methods=["GET", "POST"])
@role_required("admin")
def admin_appointment_new():
    if request.method == "POST":
        f = request.form
        with booking_lock:
            errors, customer, stylist, service = validate_booking(
                to_int(f.get("customer_id")), to_int(f.get("stylist_id")),
                to_int(f.get("service_id")), f.get("date"), f.get("time"))
            status = f.get("status", "Confirmed")
            if status not in Appointment.STATUSES:
                errors.append("Invalid status.")
            if not errors:
                a = appointments.add(Appointment(customer.id, stylist.id, service.id,
                                                 f.get("date"), f.get("time"), f.get("notes", "").strip()[:300]))
                a.status = status
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("appointment_form.html", **appointment_form_context(None, f))
        flash("Booking created.", "success")
        return redirect(url_for("admin_appointments"))
    return render_template("appointment_form.html", **appointment_form_context())


@app.route("/admin/appointments/<int:appt_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def admin_appointment_edit(appt_id):
    appt = appointments.get(appt_id)
    if not appt:
        flash("Appointment not found.", "error")
        return redirect(url_for("admin_appointments"))

    if request.method == "POST":
        f = request.form
        customer_id, stylist_id = to_int(f.get("customer_id")), to_int(f.get("stylist_id"))
        service_id, day, time_s = to_int(f.get("service_id")), f.get("date"), f.get("time")
        status = f.get("status", appt.status)
        schedule_changed = (stylist_id != appt.stylist_id or service_id != appt.service_id
                            or day != appt.date or time_s != appt.time)
        with booking_lock:
            errors = []
            if status not in Appointment.STATUSES:
                errors.append("Invalid status.")
            service = services.get(service_id)
            if status == "Cancelled" and not schedule_changed:
                pass                          # cancelling never needs a free slot
            elif schedule_changed:
                errors_v, _c, _s, service = validate_booking(
                    customer_id, stylist_id, service_id, day, time_s, exclude_id=appt.id)
                errors += errors_v
            else:                              # only client / status / notes changed
                if not users.get(customer_id) or users.get(customer_id).role != "customer":
                    errors.append("Please choose a valid client.")
                if (status != "Cancelled" and service and
                        (scheduler.overlaps(stylist_id, day, time_s, service.duration, appt.id)
                         or scheduler.customer_clash(customer_id, day, time_s, service.duration, appt.id))):
                    errors.append("That time overlaps another booking.")
            if not errors:
                appointments.update(appt.id, customer_id=customer_id, stylist_id=stylist_id,
                                    service_id=service_id, date=day, time=time_s, status=status,
                                    notes=f.get("notes", "").strip()[:300])
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("appointment_form.html", **appointment_form_context(appt, f))
        flash("Booking updated.", "success")
        return redirect(url_for("admin_appointments"))
    return render_template("appointment_form.html", **appointment_form_context(appt))


@app.route("/admin/appointments/<int:appt_id>/status", methods=["POST"])
@role_required("admin")
def admin_update_status(appt_id):
    new_status = request.form.get("status")
    appt = appointments.get(appt_id)
    if appt and new_status in Appointment.STATUSES:
        ok, msg = set_status_checked(appt, new_status)
        flash("Appointment updated." if ok else msg, "success" if ok else "error")
    else:
        flash("Could not update appointment.", "error")
    return redirect(url_for("admin_appointments"))


@app.route("/admin/appointments/<int:appt_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_appointment(appt_id):
    if appointments.delete(appt_id):
        flash("Appointment removed — the slot is free again.", "success")
    else:
        flash("Appointment not found.", "error")
    return redirect(url_for("admin_appointments"))


# =========================================================
#  ADMIN — SERVICES
# =========================================================

def service_form_errors(f):
    errors = []
    name = f.get("name", "").strip()
    if not name or len(name) > 60:
        errors.append("Service name is required (60 characters max).")
    try:
        price = float(f.get("price", ""))
        if price <= 0 or price > 10000:
            errors.append("Price must be between 0 and 10,000.")
    except ValueError:
        price = None
        errors.append("Price must be a valid number.")
    try:
        duration = int(f.get("duration", ""))
        if duration < 5 or duration > 480:
            errors.append("Duration must be between 5 and 480 minutes.")
    except ValueError:
        duration = None
        errors.append("Duration must be a valid number of minutes.")
    return errors, name, price, duration


@app.route("/admin/services", methods=["GET", "POST"])
@role_required("admin")
def admin_services():
    if request.method == "POST":
        errors, name, price, duration = service_form_errors(request.form)
        if any(s.name.lower() == name.lower() for s in services.all()):
            errors.append("A service with that name already exists.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            services.add(Service(name, request.form.get("category", "").strip() or "General",
                                 price, duration, request.form.get("description", "").strip()[:200]))
            flash("Service added.", "success")
        return redirect(url_for("admin_services"))
    return render_template("admin_services.html", services=services.all(), active_page="services")


@app.route("/admin/services/<int:service_id>/edit", methods=["POST"])
@role_required("admin")
def admin_edit_service(service_id):
    s = services.get(service_id)
    if not s:
        flash("Service not found.", "error")
        return redirect(url_for("admin_services"))
    errors, name, price, duration = service_form_errors(request.form)
    if any(o.name.lower() == name.lower() and o.id != s.id for o in services.all()):
        errors.append("Another service already uses that name.")
    if not errors and duration != s.duration:
        # a longer service could now collide with the next booking of an existing appointment
        for a in appointments.all():
            if a.service_id == s.id and a.status in ("Pending", "Confirmed") and a.date >= date.today().isoformat():
                if scheduler.overlaps(a.stylist_id, a.date, a.time, duration, a.id):
                    errors.append("Changing the duration would clash with an upcoming booking "
                                  f"on {dlabel_filter(a.date)} at {fmt12(a.time)}.")
                    break
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        services.update(s.id, name=name, price=price, duration=duration,
                        category=request.form.get("category", "").strip() or "General",
                        description=request.form.get("description", "").strip()[:200])
        flash("Service updated.", "success")
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/toggle", methods=["POST"])
@role_required("admin")
def admin_toggle_service(service_id):
    s = services.get(service_id)
    if s:
        services.update(s.id, active=not s.active)
        flash("Service updated.", "success")
    return redirect(url_for("admin_services"))


@app.route("/admin/services/<int:service_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_service(service_id):
    if any(a.service_id == service_id for a in appointments.all()):
        flash("This service is used by existing bookings, so it can't be deleted. Hide it instead.", "error")
    else:
        services.delete(service_id)
        flash("Service deleted.", "success")
    return redirect(url_for("admin_services"))


# =========================================================
#  ADMIN — STYLISTS
# =========================================================

def parse_work_hours(f):
    errors = []
    start, end = f.get("work_start", SALON_OPEN), f.get("work_end", SALON_CLOSE)
    s, e = to_minutes(start), to_minutes(end)
    if s is None or e is None:
        errors.append("Please enter valid working hours.")
    else:
        if s % SLOT_MINUTES or e % SLOT_MINUTES:
            errors.append(f"Working hours must fall on {SLOT_MINUTES}-minute marks.")
        if s < to_minutes(SALON_OPEN) or e > to_minutes(SALON_CLOSE):
            errors.append(f"Working hours must be within salon hours ({fmt12(SALON_OPEN)}–{fmt12(SALON_CLOSE)}).")
        if e - s < 60:
            errors.append("A working day must be at least one hour long.")
    days = sorted({d for d in (to_int(x) for x in f.getlist("work_days")) if d is not None and 0 <= d <= 6})
    if not days:
        errors.append("Select at least one working day.")
    return errors, start, end, days


def stylist_orphans(stylist_id, start, end, days):
    """Upcoming active bookings that would fall outside new working hours / days."""
    s_min, e_min = to_minutes(start), to_minutes(end)
    bad = []
    for a in appointments.for_stylist(stylist_id):
        if a.status in ("Cancelled", "Completed") or a.date < date.today().isoformat():
            continue
        d = parse_date(a.date)
        a_s = to_minutes(a.time)
        a_e = a_s + scheduler.service_slots(a.service_id) * SLOT_MINUTES
        if d.weekday() not in days or a_s < s_min or a_e > e_min:
            bad.append(a)
    return bad


@app.route("/admin/stylists", methods=["GET", "POST"])
@role_required("admin")
def admin_stylists():
    if request.method == "POST":
        f = request.form
        name, email, phone = f.get("name", "").strip(), f.get("email", "").strip(), f.get("phone", "").strip()
        password = f.get("password", "") or "stylist123"
        errors = profile_errors(name, email, phone) + password_errors(password)
        if email and users.find_by_email(email):
            errors.append("An account with this email already exists.")
        w_errors, start, end, days = parse_work_hours(f)
        errors += w_errors
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            st = Stylist(name, email, password, phone,
                         f.get("specialty", "").strip()[:50] or "Hair Styling", f.get("bio", "").strip()[:200])
            st.work_start, st.work_end, st.work_days = start, end, days
            users.add(st)
            flash("Stylist added to the team.", "success")
        return redirect(url_for("admin_stylists"))
    return render_template("admin_stylists.html", stylists=users.by_role("stylist"), active_page="stylists")


@app.route("/admin/stylists/<int:stylist_id>/edit", methods=["POST"])
@role_required("admin")
def admin_edit_stylist(stylist_id):
    st = users.get(stylist_id)
    if not st or st.role != "stylist":
        flash("Stylist not found.", "error")
        return redirect(url_for("admin_stylists"))
    f = request.form
    name, email, phone = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("phone", "").strip()
    errors = profile_errors(name, email, phone)
    if email and users.find_by_email(email, exclude_id=st.id):
        errors.append("Another account already uses that email.")
    w_errors, start, end, days = parse_work_hours(f)
    errors += w_errors
    if not w_errors:
        bad = stylist_orphans(st.id, start, end, days)
        if bad:
            a = bad[0]
            errors.append(f"{len(bad)} upcoming booking(s) fall outside these hours/days "
                          f"(e.g. {dlabel_filter(a.date)} at {fmt12(a.time)}). Move or cancel them first.")
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        users.update(st.id, name=name, email=email, phone=phone,
                     specialty=f.get("specialty", "").strip()[:50] or "Hair Styling",
                     bio=f.get("bio", "").strip()[:200],
                     available=f.get("available") == "on", active=f.get("active") == "on",
                     work_start=start, work_end=end, work_days=days)
        flash("Stylist updated.", "success")
    return redirect(url_for("admin_stylists"))


@app.route("/admin/stylists/<int:stylist_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_stylist(stylist_id):
    st = users.get(stylist_id)
    if not st or st.role != "stylist":
        flash("Stylist not found.", "error")
    else:
        removed = appointments.remove_where(stylist_id=st.id)
        time_offs.remove_where(stylist_id=st.id)
        users.delete(st.id)
        flash(f"Stylist removed along with {removed} booking(s).", "success")
    return redirect(url_for("admin_stylists"))


# =========================================================
#  ADMIN — CLIENTS
# =========================================================

@app.route("/admin/customers", methods=["GET", "POST"])
@role_required("admin")
def admin_customers():
    if request.method == "POST":
        f = request.form
        name, email, phone = f.get("name", "").strip(), f.get("email", "").strip(), f.get("phone", "").strip()
        password = f.get("password", "") or "client123"
        errors = profile_errors(name, email, phone) + password_errors(password)
        if email and users.find_by_email(email):
            errors.append("An account with this email already exists.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            users.add(Customer(name, email, password, phone))
            flash("Client added.", "success")
        return redirect(url_for("admin_customers"))

    data = []
    for c in users.by_role("customer"):
        c_appts = appointments.for_customer(c.id)
        data.append({"obj": c, "total_visits": len(c_appts),
                     "completed": len([a for a in c_appts if a.status == "Completed"])})
    return render_template("admin_customers.html", customers=data, active_page="customers")


@app.route("/admin/customers/<int:customer_id>/edit", methods=["POST"])
@role_required("admin")
def admin_edit_customer(customer_id):
    c = users.get(customer_id)
    if not c or c.role != "customer":
        flash("Client not found.", "error")
        return redirect(url_for("admin_customers"))
    f = request.form
    name, email, phone = f.get("name", "").strip(), f.get("email", "").strip().lower(), f.get("phone", "").strip()
    errors = profile_errors(name, email, phone)
    if email and users.find_by_email(email, exclude_id=c.id):
        errors.append("Another account already uses that email.")
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        users.update(c.id, name=name, email=email, phone=phone, active=f.get("active") == "on")
        flash("Client updated.", "success")
    return redirect(url_for("admin_customers"))


@app.route("/admin/customers/<int:customer_id>/delete", methods=["POST"])
@role_required("admin")
def admin_delete_customer(customer_id):
    c = users.get(customer_id)
    if not c or c.role != "customer":
        flash("Client not found.", "error")
    else:
        removed = appointments.remove_where(customer_id=c.id)
        users.delete(c.id)
        flash(f"Client removed along with {removed} booking(s).", "success")
    return redirect(url_for("admin_customers"))


# =========================================================
#  ADMIN — ACCOUNTS & PASSWORDS
# =========================================================

@app.route("/admin/accounts")
@role_required("admin")
def admin_accounts():
    order = {"admin": 0, "stylist": 1, "customer": 2}
    accounts = sorted(users.all(), key=lambda u: (order.get(u.role, 9), u.name.lower()))
    return render_template("admin_accounts.html", accounts=accounts, active_page="accounts")


@app.route("/admin/accounts/new", methods=["POST"])
@role_required("admin")
def admin_account_new():
    f = request.form
    role = f.get("role")
    name, email, phone = f.get("name", "").strip(), f.get("email", "").strip(), f.get("phone", "").strip()
    password = f.get("password", "")
    errors = profile_errors(name, email, phone) + password_errors(password)
    if role not in ("admin", "stylist", "customer"):
        errors.append("Please choose a role.")
    if email and users.find_by_email(email):
        errors.append("An account with this email already exists.")
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        cls = {"admin": Admin, "stylist": Stylist, "customer": Customer}[role]
        users.add(cls(name, email, password, phone))
        flash(f"{cls.__name__} account created.", "success")
    return redirect(url_for("admin_accounts"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@role_required("admin")
def admin_reset_password(user_id):
    target = users.get(user_id)
    new = request.form.get("new_password", "")
    errors = password_errors(new)
    if not target:
        flash("Account not found.", "error")
    elif errors:
        for e in errors:
            flash(e, "error")
    else:
        target.set_password(new)
        flash(f"Password reset for {target.name}. Share the new password with them securely.", "success")
    return redirect(safe_next("admin_accounts"))


@app.route("/admin/users/<int:user_id>/toggle-active", methods=["POST"])
@role_required("admin")
def admin_toggle_active(user_id):
    target = users.get(user_id)
    if not target:
        flash("Account not found.", "error")
    elif target.id == current_user().id:
        flash("You can't deactivate your own account.", "error")
    else:
        users.update(target.id, active=not target.active)
        flash(f"{target.name} is now {'active' if target.active else 'deactivated'}.", "success")
    return redirect(safe_next("admin_accounts"))


# =========================================================
#  ERROR HANDLING
# =========================================================

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="This page has stepped out of the salon."), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, message="Something went wrong on our end."), 500


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5001")))
