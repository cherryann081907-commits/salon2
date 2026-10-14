# Lumora — Salon Management System

A multi-user web-based salon management system built with **Python, Flask, and OOP** —
no database, temporary storage only (Python lists in memory).

## Run it

```bash
python3 -m pip install flask
python3 app.py
```

Then open **http://localhost:5001**. Set the `PORT` environment variable to use a different port.

> Note: data resets whenever the app restarts, since everything lives in memory (as required — no database).

## Demo accounts

| Role       | Email                  | Password    |
|------------|-------------------------|-------------|
| Admin      | admin@lumora.com        | admin123    |
| Stylist    | maya@lumora.com         | stylist123  |
| Client     | sophie@example.com      | client123   |

New clients can also self-register from the **Create account** page.

## What's new in this version

- **3D, realistic interface**: layered depth shadows, bevelled buttons, tactile "key" time slots, a perspective hero scene and mouse-tilt cards (respects reduce-motion).
- **Time Selection dropdown**: pick service, stylist and date, then choose from a dropdown and a slot grid. Every time is labelled Available or Booked (or Unavailable / Not enough time).
- **No double booking**: bookings are validated and saved inside a lock, are duration-aware (a 2.5-hour colour blocks every 30-minute slot it covers), and a client can't hold two overlapping appointments.
- **Real-time status**: the browser polls `/api/slots` every 4 s. A booked slot turns Booked and locked for everyone, and a slot you picked that someone else grabs is cleared with a warning.
- **Admin full control**: add / edit / delete bookings, schedules (time off, working hours and days), stylists, clients and accounts; change status; reset any password; activate / deactivate logins.
- **Live schedule board** (`/schedule`): day grid of every stylist, auto-refreshing. Stylists manage their own time off.
- **Change password** for clients, stylists and admin (My Account) with a strength meter. Passwords are stored as salted hashes.
- **Error prevention**: CSRF tokens, server-side validation, no past or out-of-hours bookings, and guard rails (can't delete a service used by bookings, shrink hours over existing bookings, block time over a booking, or restore a cancelled booking whose slot was taken).

## The three user roles

- **Admin**: everything (bookings, schedule, services, stylists, clients, accounts, passwords).
- **Stylist**: own bookings and status, own time off, availability toggle, change password.
- **Client**: books with live time selection, cancels, edits profile, changes password.

Passwords must be 8+ characters with a letter and a number. The demo passwords meet this.

## OOP design

- `User` (base) → `Admin`, `Stylist`, `Customer` — **inheritance** + **polymorphism** (each overrides `role`, `dashboard_endpoint()`), password is a **private/encapsulated** attribute.
- `Repository` (generic CRUD) → `UserRepository`, `ServiceRepository`, `AppointmentRepository` — inheritance + overridden validation (e.g. unique email).
- All data lives in plain Python lists inside each repository — no database, per the brief.

## Deploying

This is a standard Flask app — deploy to Render, Railway, PythonAnywhere, or any host that
runs `python3 app.py` (or `gunicorn app:app`). Remember to set a real `app.secret_key` in production.
