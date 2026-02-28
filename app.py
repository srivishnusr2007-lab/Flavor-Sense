from dotenv import load_dotenv
load_dotenv("email.env")
import os
import csv
import threading
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, redirect, session, url_for
from werkzeug.security import generate_password_hash, check_password_hash
import smtplib
from email.message import EmailMessage

# ─────────────────────────────────────────
#  App Setup
# ─────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("FLAVORSENSE_SECRET", "change-me-in-production")

# ─────────────────────────────────────────
#  Global State
# ─────────────────────────────────────────
menu = {
    "breakfast": "Idli, Sambar",
    "lunch":     "Rice, Dal, Curry",
    "dinner":    "Chapathi, Paneer"
}

# In-memory ratings: { "YYYY-MM-DD": { "Idli": [5, 4, 3], ... } }
RATINGS = {}
ratings_lock = threading.Lock()

# ─────────────────────────────────────────
#  File Paths
# ─────────────────────────────────────────
STUDENTS_CSV = os.environ.get("STUDENTS_CSV", "students.csv")
REVIEWS_CSV  = os.environ.get("REVIEWS_CSV",  "reviews.csv")
csv_lock = threading.Lock()

# ─────────────────────────────────────────
#  Staff Credentials (set via env vars!)
# ─────────────────────────────────────────
STAFF_USER = os.environ.get("STAFF_USER", "staff")
STAFF_PASS = os.environ.get("STAFF_PASS", "changeme123")   # plain-text env var

# ─────────────────────────────────────────
#  CSV Helpers
# ─────────────────────────────────────────
def ensure_csv_files():
    """Create CSV files with headers if they don't exist."""
    if not os.path.exists(STUDENTS_CSV):
        with open(STUDENTS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "email", "password_hash"])
            writer.writeheader()

    if not os.path.exists(REVIEWS_CSV):
        with open(REVIEWS_CSV, "w", newline="", encoding="utf-8") as f:
            fields = ["email", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()


def load_students():
    ensure_csv_files()
    with csv_lock:
        with open(STUDENTS_CSV, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


def student_exists(email):
    return any(s["email"].lower() == email.lower() for s in load_students())


def save_student(name, email, password_hash):
    ensure_csv_files()
    with csv_lock:
        with open(STUDENTS_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["name", "email", "password_hash"])
            writer.writerow({"name": name, "email": email, "password_hash": password_hash})


def load_reviews():
    ensure_csv_files()
    with csv_lock:
        with open(REVIEWS_CSV, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


def create_reviews_row(email):
    ensure_csv_files()
    fields = ["email", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    with csv_lock:
        with open(REVIEWS_CSV, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writerow({
                "email": email,
                "Mon": "no", "Tue": "no", "Wed": "no", "Thu": "no",
                "Fri": "no", "Sat": "no", "Sun": "no"
            })


def update_review_for_today(email):
    """Mark today's weekday column as 'yes' for the given student."""
    ensure_csv_files()
    weekday = datetime.now(timezone.utc).strftime("%a")  # Mon, Tue, ...
    fields  = ["email", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    with csv_lock:
        rows = []
        with open(REVIEWS_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

        for row in rows:
            if row["email"].lower() == email.lower():
                row[weekday] = "yes"

        with open(REVIEWS_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


# ─────────────────────────────────────────
#  Email Helper
# ─────────────────────────────────────────
def send_email(to_email, subject, body):
    host     = os.environ.get("EMAIL_HOST", "smtp.gmail.com")
    port     = int(os.environ.get("EMAIL_PORT", 587))
    user     = os.environ.get("EMAIL_USER")
    password = os.environ.get("EMAIL_PASS")

    if not user or not password:
        print(f"[EMAIL] Skipping — EMAIL_USER or EMAIL_PASS not set. Would send to: {to_email}")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = user
    msg["To"]      = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        print(f"[EMAIL] Sent to {to_email}")
        return True
    except Exception as e:
        print(f"[EMAIL] Failed to send to {to_email}: {e}")
        return False


# ─────────────────────────────────────────
#  Auth Helpers
# ─────────────────────────────────────────
def student_required(f):
    """Decorator: redirect to register if student not logged in."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("student_email"):
            return redirect(url_for("register"))
        return f(*args, **kwargs)
    return decorated


def staff_required(f):
    """Decorator: redirect to staff login if not authenticated."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("staff_logged_in"):
            return redirect(url_for("staff_login"))
        return f(*args, **kwargs)
    return decorated


# ─────────────────────────────────────────
#  Routes: Root
# ─────────────────────────────────────────
@app.route("/")
def index():
    if session.get("student_email"):
        return redirect(url_for("student_review"))
    return redirect(url_for("register"))


# ─────────────────────────────────────────
#  Routes: Student Register / Login / Logout
# ─────────────────────────────────────────
@app.route("/register", methods=["GET", "POST"])
def register():
    # Already logged in? Go to student page
    if session.get("student_email"):
        return redirect(url_for("student_review"))

    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        # Validation
        if not name or not email or not password:
            return render_template("register_login.html",
                                   error="Please fill in all fields.", show="register")
        if len(password) < 6:
            return render_template("register_login.html",
                                   error="Password must be at least 6 characters.", show="register")
        if student_exists(email):
            return render_template("register_login.html",
                                   error="This email is already registered. Please login.", show="register")

        pw_hash = generate_password_hash(password)
        save_student(name, email, pw_hash)
        create_reviews_row(email)

        session["student_email"] = email
        session["student_name"]  = name
        return redirect(url_for("student_review"))

    return render_template("register_login.html", show="register")


@app.route("/student-login", methods=["POST"])
def student_login():
    email    = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")

    if not email or not password:
        return render_template("register_login.html",
                               error="Please enter your email and password.", show="login")

    for student in load_students():
        if student["email"].lower() == email:
            if check_password_hash(student["password_hash"], password):
                session["student_email"] = email
                session["student_name"]  = student["name"]
                return redirect(url_for("student_review"))
            else:
                return render_template("register_login.html",
                                       error="Incorrect password. Please try again.", show="login")

    return render_template("register_login.html",
                           error="No account found with that email. Please register.", show="login")


@app.route("/logout")
def logout():
    session.pop("student_email", None)
    session.pop("student_name", None)
    return redirect(url_for("register"))


# ─────────────────────────────────────────
#  Routes: Student Review Page
# ─────────────────────────────────────────
@app.route("/student")
@student_required
def student_review():
    return render_template(
        "student.html",
        menu=menu,
        student_email=session.get("student_email"),
        student_name=session.get("student_name")
    )


# ─────────────────────────────────────────
#  Routes: Rating API
# ─────────────────────────────────────────
@app.route("/rate", methods=["POST"])
@student_required
def rate():
    data   = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    item   = data.get("item", "").strip()
    rating = data.get("rating")
    date   = data.get("date", "").strip()

    # Validate
    if not item or not date:
        return jsonify({"error": "Missing item or date"}), 400
    try:
        rating = int(rating)
        if rating < 1 or rating > 5:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Rating must be an integer between 1 and 5"}), 400

    # Save to in-memory store (thread-safe)
    with ratings_lock:
        if date not in RATINGS:
            RATINGS[date] = {}
        RATINGS[date].setdefault(item, []).append(rating)

    # Mark today as reviewed in CSV
    update_review_for_today(session["student_email"])

    return jsonify({"message": "Rating saved", "item": item, "rating": rating})


@app.route("/ratings/<date>")
def get_ratings(date):
    """Return ratings for a specific date."""
    with ratings_lock:
        return jsonify(RATINGS.get(date, {}))


# ─────────────────────────────────────────
#  Routes: Staff Login / Logout
# ─────────────────────────────────────────
@app.route("/staff-login", methods=["GET", "POST"])
def staff_login():
    if session.get("staff_logged_in"):
        return redirect(url_for("staff_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == STAFF_USER and password == STAFF_PASS:
            session["staff_logged_in"] = True
            return redirect(url_for("staff_dashboard"))
        else:
            return render_template("staff_login.html", error="Invalid username or password.")

    return render_template("staff_login.html")


@app.route("/staff-logout")
def staff_logout():
    session.pop("staff_logged_in", None)
    return redirect(url_for("staff_login"))


# ─────────────────────────────────────────
#  Routes: Staff Dashboard
# ─────────────────────────────────────────
@app.route("/staff-dashboard")
@staff_required
def staff_dashboard():
    reviews = load_reviews()
    return render_template("staff_dashboard.html", menu=menu, reviews=reviews, message=None)


@app.route("/update-menu", methods=["POST"])
@staff_required
def update_menu():
    breakfast = request.form.get("breakfast", "").strip()
    lunch     = request.form.get("lunch", "").strip()
    dinner    = request.form.get("dinner", "").strip()

    if breakfast: menu["breakfast"] = breakfast
    if lunch:     menu["lunch"]     = lunch
    if dinner:    menu["dinner"]    = dinner

    reviews = load_reviews()
    return render_template("staff_dashboard.html",
                           menu=menu,
                           reviews=reviews,
                           message=None,
                           menu_updated=True)


@app.route("/send-reminders", methods=["POST"])
@staff_required
def send_reminders():
    weekday = datetime.now(timezone.utc).strftime("%a")  # Mon, Tue, ...
    rows    = load_reviews()
    sent    = 0
    skipped = 0

    for row in rows:
        if row.get(weekday, "no").lower() == "no":
            to_email = row["email"]
            subject  = "📢 Reminder: Rate today's mess on Flavorsense"
            body     = (
                f"Hello,\n\n"
                f"We noticed you haven't rated today's mess on Flavorsense yet.\n\n"
                f"Today's Menu:\n"
                f"  Breakfast : {menu['breakfast']}\n"
                f"  Lunch     : {menu['lunch']}\n"
                f"  Dinner    : {menu['dinner']}\n\n"
                f"Please visit the portal and share your feedback — it helps us improve!\n\n"
                f"Thanks,\nFlavorsense Team"
            )
            if send_email(to_email, subject, body):
                sent += 1
            else:
                skipped += 1

    reviews = load_reviews()
    msg = f"Reminders sent: {sent}"
    if skipped:
        msg += f" | Skipped (email not configured): {skipped}"

    return render_template("staff_dashboard.html",
                           menu=menu,
                           reviews=reviews,
                           message=msg)


# ─────────────────────────────────────────
#  Error Handlers
# ─────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_template("register_login.html",
                           error="Page not found (404).", show="register"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("register_login.html",
                           error="Something went wrong on our end (500). Please try again.", show="register"), 500


# ─────────────────────────────────────────
#  Run
# ─────────────────────────────────────────
if __name__ == "__main__":
    ensure_csv_files()
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    port       = int(os.environ.get("PORT", 5000))
    print(f"[FLAVORSENSE] Starting on http://localhost:{port}  |  debug={debug_mode}")
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
