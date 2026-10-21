# Library Service API

A Django REST Framework project for managing a library catalog, book
borrowings, rental payments, and Telegram notifications.

Customers borrow books and pay through Stripe Checkout. Employees manage the
catalog and register returns. Administrators manage accounts and roles.
Background tasks reconcile payments, send overdue reminders, and retry pending
notifications after delivery failures.

## Stack

- Django, Django REST Framework
- SQLite
- Simple JWT for authentication
- Stripe Checkout for payments in USD
- Celery and Redis for background tasks
- Telegram Bot API for notifications
- drf-spectacular for OpenAPI and Swagger UI
- Django tests, coverage.py, Black, Flake8, and GitHub Actions

Exact dependency versions are in [requirements.txt](requirements.txt).
This project is configured for local development; deployment and CD are outside
its scope.

## Project structure

```text
library-service/
├── .github/workflows/ci.yml
├── src/
│   ├── library_service/     # Django settings, root URLs, Celery configuration
│   ├── users/               # Accounts, roles, profiles, authentication
│   ├── books/               # Catalog, inventory, filtering
│   ├── borrowings/          # Borrowing and return workflow
│   ├── payments/            # Stripe integration, rental charges, fines
│   └── notifications/       # Telegram delivery and persistent notification queue
├── tests/
│   ├── users/
│   ├── books/
│   ├── borrowings/
│   ├── payments/
│   └── notifications/
├── .env.example
├── manage.py
├── pyproject.toml
└── requirements.txt
```

## Local installation

Install Python 3.14 and Git. For the complete workflow, you also need Redis,
a Stripe test account and the Stripe CLI, and a Telegram bot.

Clone the development branch:

```shell
git clone --branch develop https://github.com/pprykhodko/library-service.git
cd library-service
```

Create and activate a virtual environment on Windows PowerShell:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Or on Linux/macOS:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
```

Install dependencies and the project:

```shell
python -m pip install -r requirements.txt -e .
```

The `-e .` option installs this repository in editable mode so Python can import
the applications inside `src/`. Changes to their source files take effect without
reinstalling the project. Generated `*.egg-info` files are installation metadata
and are ignored by Git.

Copy the environment template on Windows:

```powershell
Copy-Item .env.example .env
```

Or on Linux/macOS:

```bash
cp .env.example .env
```

Edit `.env` in the repository root:

| Variable | Purpose |
| --- | --- |
| `STRIPE_SECRET_KEY` | Stripe test secret key, beginning with `sk_test_` |
| `STRIPE_WEBHOOK_SECRET` | Signing secret printed by the local Stripe listener, beginning with `whsec_` |
| `PAYMENTS_BASE_URL` | Base URL for Checkout return URLs; locally `http://127.0.0.1:8000` |
| `CELERY_BROKER_URL` | Redis connection URL; locally `redis://127.0.0.1:6379/0` |
| `TELEGRAM_BOT_TOKEN` | Bot token issued by BotFather |
| `TELEGRAM_CHAT_ID` | Destination chat or group ID, including its minus sign when present |

Replace the placeholder values with your own credentials. Keep `.env` out of
Git. Restart Django, the Celery worker, and Beat after changing these settings.
Integration setup is described below.

Create the database and an administrator:

```shell
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Useful local URLs:

| Page | URL |
| --- | --- |
| API root | <http://127.0.0.1:8000/> |
| Swagger UI | <http://127.0.0.1:8000/api/docs/> |
| OpenAPI schema | <http://127.0.0.1:8000/api/schema/> |
| Django Admin | <http://127.0.0.1:8000/admin/> |

## Redis and Celery

Use an existing Redis instance or start one with Docker. The repository does not
contain a Docker Compose setup; this container runs Redis only.

```shell
docker run -d --name library-redis -p 127.0.0.1:6379:6379 redis:7-alpine
docker exec library-redis redis-cli ping
```

The second command should return `PONG`. On subsequent runs, start the existing
container with `docker start library-redis`.

Open separate terminals in the project root and activate the virtual environment
in each. Keep Django, one worker, and one Beat scheduler running.

Worker on Linux/macOS:

```shell
python -m celery -A library_service worker --loglevel=info
```

For local development on Windows, use the solo pool:

```powershell
python -m celery -A library_service worker --loglevel=info --pool=solo
```

Windows is not officially supported by Celery; the solo pool is a local
workaround. See the [Celery platform FAQ](https://docs.celeryq.dev/en/stable/faq.html#does-celery-support-windows).

Beat scheduler, in another terminal:

```shell
python -m celery -A library_service beat --loglevel=info
```

| Scheduled task | Schedule |
| --- | --- |
| Retry pending notifications | Every minute |
| Synchronize pending Stripe sessions | Every minute |
| Notify about overdue, unreturned books | Daily at 09:00, `Europe/Vienna` |

Beat schedules work; the worker executes it. Run a single Beat instance for this
project to avoid duplicate scheduling.

## Stripe setup

Use a Stripe sandbox/test environment and put its secret API key in `.env`.
Install the [Stripe CLI](https://docs.stripe.com/stripe-cli), then run:

```shell
stripe login
stripe listen --forward-to http://127.0.0.1:8000/payments/webhook/
```

Copy the listener's `whsec_...` value into `STRIPE_WEBHOOK_SECRET`, restart the
application processes, and leave the listener running. The signing secret must
belong to this listener. See [Stripe's local webhook instructions](https://docs.stripe.com/webhooks#test-your-handler).

To test a successful Checkout payment, use card number `4242 4242 4242 4242`, a
future expiration date, and any three-digit CVC. Use test keys and test cards;
these transactions do not transfer real money. More scenarios are available in
the [Stripe testing documentation](https://docs.stripe.com/testing).

The application verifies Stripe webhook signatures. Visiting the success URL
alone cannot mark a payment as paid: the server checks the Stripe session and
its payment details. Background reconciliation also checks pending sessions,
so confirmation does not depend on the customer returning to the success page.

To run reconciliation manually:

```shell
python manage.py sync_payments
```

This command contacts Stripe directly and does not require a running Celery
worker.

## Telegram setup

1. Create a bot through [BotFather](https://t.me/BotFather) and save its token in
   `TELEGRAM_BOT_TOKEN`.
2. Add the bot to the destination group and allow it to send messages.
3. Send `/start@YourBotUsername` in that group.
4. Open `python manage.py shell` and retrieve the group ID:

```python
import requests
from django.conf import settings

response = requests.get(
    f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/getUpdates",
    timeout=10,
)
response.raise_for_status()
for update in response.json().get("result", []):
    chat = update.get("message", {}).get("chat", {})
    if chat.get("type") in ("group", "supergroup"):
        print(chat["title"], chat["id"])
```

Save the printed ID as `TELEGRAM_CHAT_ID` and restart the application processes.
If there are no updates, send a new command addressed to the bot and try again.
`getUpdates` requires that the bot has no active Telegram webhook; this project
does not register one. See the [Telegram Bot API reference](https://core.telegram.org/bots/api#getupdates).

Notifications go to the configured chat for new borrowings, confirmed payments,
and overdue books. There are no public notification endpoints.

### Delivery and recovery

Notifications are first saved in the database with a unique event key. Borrowing
creation and payment confirmation save their notification in the same database
transaction as the business change. Delivery is queued after commit.

If Redis is unavailable, the notification remains in the database. After Redis,
the worker, and Beat are running again, the periodic retry task queues eligible
unsent records. Temporary Telegram failures are retried; expired processing locks
allow recovery after a worker stops unexpectedly.

Overdue reminders are deduplicated per borrowing and calendar day. Successfully
recorded deliveries are skipped on retries. Delivery is **at least once**:
a duplicate is still possible if Telegram accepts a message but the worker stops
before saving the successful result.

## Roles and permissions

Registration creates a `customer`. An administrator can change a user's role
through the user form in Django Admin. Public registration and profile endpoints
cannot grant roles or staff privileges.

| Operation | Customer | Employee | Administrator |
| --- | --- | --- | --- |
| Read catalog | Active books | All books | All books |
| Create, update, deactivate, delete books | No | Yes | Yes |
| Read own profile | Yes | Yes | Yes |
| Edit own profile | Yes | No | Yes |
| Change own password using current password | Yes | Yes | Yes |
| Read profiles through `/users/{id}/` | No | Customers only | Any user |
| Create borrowing | For self | No | For self |
| Read borrowings and payments | Own records | All records | All records |
| Register a book return | No | Yes | Yes |
| Create or renew a Checkout session | Own payment | No | Any payment |
| Manage user roles in Django Admin | No | No | Yes |

The `customer` and `employee` roles use the `Customers` and `Employees` Django
groups. An administrator is a superuser. An employee is not Django `is_staff`
and does not receive access to the admin site.

Borrowings and payments cannot be manually created, edited, or deleted through
Django Admin. Use the API borrowing/return actions and payment workflow so that
inventory, charges, and notifications remain consistent.

## API reference

Business endpoints have no `/api/` prefix and use trailing slashes.
Protected requests require:

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

In Postman, select **Bearer Token** and paste only the access token into its
token field. Access tokens last one hour. Use the refresh endpoint to obtain
a new access token.

### Users

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/users/` | Register a customer; public |
| POST | `/users/token/` | Obtain access and refresh tokens; public |
| POST | `/users/token/refresh/` | Exchange a refresh token for an access token |
| POST | `/users/token/verify/` | Verify a token |
| GET, PUT, PATCH | `/users/me/` | Read or update own profile, subject to role |
| POST | `/users/me/password/` | Change own password |
| GET | `/users/{id}/` | Read a user's profile, subject to role |

Example registration body:

```json
{
  "email": "anna@example.com",
  "password": "LibraryReader!2026",
  "first_name": "Anna",
  "last_name": "Reader"
}
```

Obtain tokens using `POST /users/token/`:

```json
{
  "email": "anna@example.com",
  "password": "LibraryReader!2026"
}
```

Refresh with `{"refresh": "<refresh_token>"}`; verify with
`{"token": "<access_token>"}`.

Change a password using `POST /users/me/password/`:

```json
{
  "old_password": "LibraryReader!2026",
  "new_password": "AnotherLibraryPassword!2026"
}
```

Success returns `204 No Content`. Password changes through `/users/me/` are
rejected for every role, including administrators. Email addresses are normalized
and checked for case-insensitive uniqueness.

### Books

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/books/` | List and filter books |
| POST | `/books/` | Create a book |
| GET | `/books/{id}/` | Read a book |
| PUT, PATCH | `/books/{id}/` | Update a book |
| DELETE | `/books/{id}/` | Delete a book without borrowing history |

Create a book as an employee or administrator:

```json
{
  "title": "Clean Code",
  "author": "Robert C. Martin",
  "cover": "HARD",
  "inventory": 3,
  "daily_fee": "1.50",
  "is_active": true
}
```

`cover` accepts `HARD` or `SOFT`. Inventory and daily fee cannot be negative.
Books with borrowing history are protected from deletion; deactivate them with
`PATCH` and `{"is_active": false}` instead.

Supported filters:

| Parameter | Meaning |
| --- | --- |
| `title`, `author` | Case-insensitive partial match |
| `cover` | `HARD` or `SOFT` |
| `is_active` | Catalog status: `true` or `false` |
| `available` | `true` means active with inventory greater than zero; `false` means the opposite |

Example: `/books/?author=martin&cover=HARD&available=true`.
Customers always see only active books, even when supplying filters. An active
book with zero inventory remains visible unless filtered out; it cannot be
borrowed. Invalid values for supported filters return `400`.

### Borrowings

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/borrowings/` | List visible borrowings |
| POST | `/borrowings/` | Borrow a book for the authenticated user |
| GET | `/borrowings/{id}/` | Read a visible borrowing |
| POST | `/borrowings/{id}/return/` | Register return; employee or administrator |

Creation accepts `book` (its numeric ID) and `expected_return_date` (an ISO date).
For a body using a date three days from today, run this in `python manage.py shell`
and replace book ID `1` with an existing book:

```python
import json
from datetime import timedelta
from django.utils import timezone

print(json.dumps({
    "book": 1,
    "expected_return_date": (timezone.localdate() + timedelta(days=3)).isoformat(),
}))
```

Send the resulting JSON to `POST /borrowings/`. The server sets the user,
borrowing date, and daily fee snapshot, decreases available inventory by one,
creates the rental payment, and saves a notification. The response includes
related payments; use their IDs for Checkout.

The return action takes no body. It records today's date, restores one copy to
inventory, and creates a fine if applicable. Repeating a return is rejected.
There are no generic borrowing update or delete endpoints.

List filters: `user_id=<positive integer>` and `is_active=true|false`.
An active borrowing has no actual return date. Customer filters never expose
another user's records: filtering for another customer returns an empty list
with `200`, while retrieving their borrowing by ID returns `404`.

### Payments

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/payments/` | List visible payments |
| GET | `/payments/{id}/` | Read a visible payment |
| POST | `/payments/{id}/checkout/` | Create or reuse a Checkout session; no body |
| POST | `/payments/{id}/renew/` | Replace an expired Checkout session; no body |
| GET | `/payments/success/?session_id=...` | Public return URL; verifies session with Stripe |
| GET | `/payments/cancel/` | Public cancel return URL; leaves payment status unchanged |
| POST | `/payments/webhook/` | Receive signed Stripe events; no JWT required |

Payments are created by the server. Clients cannot create arbitrary charges or
edit payment amounts and statuses. Payment types are `PAYMENT` and `FINE`;
statuses are `PENDING`, `PAID`, and `EXPIRED`.

Typical workflow:

1. Create a borrowing and obtain its payment ID from the response.
2. Send `POST /payments/{id}/checkout/` as the owner or administrator.
3. Open the returned `session_url` and complete Stripe Checkout.
4. Fetch the payment to check its status after confirmation.
5. Register the book return as an employee or administrator.
6. If the return generated a fine, pay that payment through the same workflow.

Repeating Checkout reuses an existing open session. For an expired session, use
`renew/`; renewal keeps the payment amount and inventory unchanged. Cancelling on
the Stripe page does not cancel the debt. A customer with a `PENDING` or `EXPIRED`
rental payment or fine cannot create another borrowing.

### Rental and fine calculations

All amounts are in USD. The book's daily fee is saved on the borrowing, so later
catalog price changes do not alter that borrowing's charges.

```text
rental_days = max(expected_return_date - borrow_date, 1 day)
rental_amount = rental_days × saved_daily_fee

overdue_days = max(actual_return_date - expected_return_date, 0 days)
fine = overdue_days × saved_daily_fee × 2
```

The rental is charged for the planned period. For example, three days at $1.50
cost $4.50. An early return does not recalculate or refund this charge.

A fine is created once, at return. A two-day delay at $1.50 per day produces a
$6.00 fine. A positive fine below $0.50 is raised to $0.50; a zero fine is not
created. A positive rental total below $0.50 is rejected instead of rounded up.
Free rentals have a zero-value payment marked `PAID` without Stripe Checkout.

### Pagination

Book, borrowing, and payment lists return `count`, `next`, `previous`, and
`results`. The default page size is 10; `page_size` can increase it to 100.
For example: `/books/?page=2&page_size=20`.

## Tests and quality checks

Run commands from the repository root with the virtual environment activated.
Tests use Django's test database and mock external integrations; live Stripe,
Telegram, and Redis services are not required.

All tests:

```shell
python manage.py test tests --noinput
```

One application, for example notifications:

```shell
python manage.py test tests.notifications --noinput
```

Run the same checks as CI:

```shell
python -m black --check .
python -m flake8 .
python manage.py check
python manage.py makemigrations --check --dry-run
python -m coverage run manage.py test tests --noinput
python -m coverage report
```

For a browsable coverage report, run `python -m coverage html` and open
`htmlcov/index.html`. The enforced coverage threshold is 60%, configured in
[pyproject.toml](pyproject.toml).

[GitHub Actions CI](.github/workflows/ci.yml) runs formatting, linting, Django and
migration checks, and tests with coverage on pushes to `develop`/`main` and pull
requests targeting either branch. There is no deployment job.
