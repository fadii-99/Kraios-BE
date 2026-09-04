# KRAIOS Admin Console API

Everything the admin console talks to. Mounted at **`/api/v1/admin/`** — the
bare `/admin/` prefix belongs to Django's own admin site.

The Django app lives in `backend/admin/`. Its app **label** is `kraios_admin`
(Django will not allow two apps labelled `admin`), so migrations, table names
and `manage.py test admin` all refer to it that way.

---

## 1. Getting a console running

```bash
# once, per environment - there is no seeded admin account anywhere in source
python manage.py migrate
python manage.py create_kraios_admin --email ops@yourdomain.com --role SUPER_ADMIN

# reminders need the scheduler; compose.yaml has a `beat` service for it
celery -A config beat --loglevel=info
```

`--role` is one of `SUPER_ADMIN`, `ADMIN`, `SUPPORT_ADMIN`. Only `SUPER_ADMIN`
can read the audit trail.

The console origin must appear in **both** `DJANGO_CORS_ALLOWED_ORIGINS` and
`DJANGO_CSRF_TRUSTED_ORIGINS`, or sign-in cannot work at all — the session is a
cookie, so the browser will not send it to an origin the server has not
approved.

---

## 2. How a request is authenticated

Same shape as the customer API, with a separate credential:

1. `GET /api/v1/admin/auth/csrf/` once, to receive the `csrftoken` cookie.
2. Send `X-CSRFToken: <that value>` on **every** POST/PATCH/PUT/DELETE.
3. Send `credentials: 'include'` on every request — the session lives in
   `HttpOnly` cookies named `kraios_admin_access` / `kraios_admin_refresh`.
4. On `401`, call `POST /api/v1/admin/auth/refresh/` once and retry. A second
   `401` means the session is genuinely gone: show the login form.

The admin cookies are **separate from the customer ones** and the tokens carry
an admin scope claim, so a customer session can never act as an administrator
even on a shared hostname.

Two things kill every open admin session immediately, not at the next expiry:
signing out, and an administrator's own password change. Deactivating an
administrator (`AdminProfile.is_active = False`) does the same.

---

## 3. Endpoints

`(P)` = privileged, Super Admin only. Everything else needs any active admin.

### Session

| Method | Path                      | Notes                                                    |
| ------ | ------------------------- | -------------------------------------------------------- |
| GET    | `auth/csrf/`            | public; sets the CSRF cookie                             |
| POST   | `auth/login/`           | public;`{email, password}` → `{admin}` + cookies    |
| POST   | `auth/refresh/`         | public; rotates the pair,`204`                         |
| POST   | `auth/logout/`          | public; always clears cookies,`204`                    |
| GET    | `auth/me/`              | `{admin}`                                              |
| POST   | `auth/change-password/` | `{current_password, new_password}`; ends every session |

`login` answers `429` with a `Retry-After` header once a caller has spent its
attempts. Five misses from one `(email, IP)` pair inside 15 minutes locks that
pair; twenty from one IP locks the host.

### Dashboard

| Method | Path           | Query                    |
| ------ | -------------- | ------------------------ |
| GET    | `dashboard/` | `range=7d\|30d\|90d\|12m` |

### Users

| Method | Path                              | Body / query                                                                                                                              |
| ------ | --------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| GET    | `users/`                        | `search`, `account_status`, `subscription`, `meeting_status`, `account_setup`, `sort`, `direction`, `page`, `page_size` |
| GET    | `users/<id>/`                   | full account: usage breakdown, meeting, plan                                                                                              |
| PATCH  | `users/<id>/`                   | `{name, firm, email, country, jobTitle, phone, accountStatus, subscription}` — all optional                                            |
| POST   | `users/<id>/status/`            | `{status: "Active"\|"Inactive"}`                                                                                                         |
| POST   | `users/<id>/generate-password/` | no body — issues credentials, activates, emails                                                                                          |
| POST   | `users/<id>/subscription/`      | `{planId \| plan, billingCycle, durationDays}`                                                                                           |
| DELETE | `users/<id>/subscription/`      | takes the account off every plan                                                                                                          |

`generate-password` returns the updated account plus
`credentialsEmailSent: true|false`. **The password is never in the response** —
the email is the only carrier. If `credentialsEmailSent` is `false`, call it
again; each call issues a fresh password and invalidates the previous one.

Administrators are not customers: they do not appear in `users/`, and asking
for one by id returns `404`.

### Meetings

| Method | Path                            | Body / query                                                                                 |
| ------ | ------------------------------- | -------------------------------------------------------------------------------------------- |
| GET    | `meetings/`                   | `search`, `status`, `firm`, `date=today\|week\|month`, `sort`, `direction`, paging |
| GET    | `meetings/slots/`             | `date=YYYY-MM-DD` → `[{time, available}]`                                               |
| GET    | `meetings/<uuid>/`            |                                                                                              |
| PATCH  | `meetings/<uuid>/`            | `{notes}`                                                                                  |
| POST   | `meetings/<uuid>/status/`     | `{status: "Completed"\|"Cancelled"\|"No Show", outcome?, notes?}`                            |
| POST   | `meetings/<uuid>/reschedule/` | `{date, time, notes?}`                                                                     |

`outcome` is one of `CONTINUING`, `NOT_CONTINUING`, `FOLLOW_UP`, `PENDING`. A
`Completed` meeting with `CONTINUING` approves the signup request; with
`NOT_CONTINUING` it rejects it. That is the gate before **Generate Password**.

`Scheduled` is deliberately not settable through `status/` — giving a meeting a
slot is `reschedule/`, so a meeting can never be "scheduled" without a time.

### Availability

| Method | Path                             | Body                                                                                     |
| ------ | -------------------------------- | ---------------------------------------------------------------------------------------- |
| GET    | `availability/`                | `{rules, blackouts}`                                                                   |
| PUT    | `availability/`                | `{rules: [{weekday, startTime, endTime, slotMinutes, isActive}]}` — replaces the week |
| POST   | `availability/blackouts/`      | `{date, reason?}`                                                                      |
| DELETE | `availability/blackouts/<id>/` |                                                                                          |

`weekday` is `0` = Monday. Times are UTC. Migrations seed Mon–Fri 09:00–17:00
in half-hour slots so the slot endpoint answers with something on a fresh
install.

**This screen is not console-only.** The PUBLIC signup form draws its calendar
and its slot list from the same schedule, through two unauthenticated reads
outside this API:

| Method | Path                                          | Answer                          |
| ------ | --------------------------------------------- | ------------------------------- |
| GET    | `/api/v1/auth/booking/days/?month=YYYY-MM`  | `{month, min_date, max_date, days: [ISO date]}` — the OPEN dates only |
| GET    | `/api/v1/auth/booking/slots/?date=YYYY-MM-DD` | `{date, slots: [{time, label, available}]}` |

Both call the same `services_meetings` functions this console's
`meetings/slots/` does, so the two can never offer different weeks, and
`accounts.serializers.SignupRequestSerializer` refuses any slot the schedule
does not hold open. Closing a weekday or adding a blackout here therefore takes
a date off the public form immediately — and emptying the week takes ALL of
them off, which is worth knowing before saving a week with every day
unchecked.

### Plans · Usage · Support

| Method           | Path                   |                                                                              |
| ---------------- | ---------------------- | ---------------------------------------------------------------------------- |
| GET/POST         | `plans/`             | list / create                                                                |
| GET/PATCH/DELETE | `plans/<id>/`        | delete returns`409` while accounts are on it                               |
| POST             | `plans/<id>/status/` | `{status: "Active"\|"Inactive"}`                                            |
| GET              | `usage/`             | `search`, `subscription`, `firm`, `status=near\|normal`, sort, paging |
| GET              | `usage/firms/`       | the firm filter's options                                                    |
| GET              | `support/`           | `search`, `status`, `priority`, `topic`, sort, paging          |
| GET/PATCH        | `support/<id>/`      | PATCH takes any of`{status, priority, assignee}`                           |
| GET              | `audit-logs/`        | **(P)** `action`, `target_type`, `admin_email`, paging           |

**Plan bodies.** `POST plans/` and `PATCH plans/<id>/` both take the WHOLE
plan — the PATCH is a replacement, not a merge — and require `name`,
`description`, `price`, `billingCycle`, `status` and every limit in
`PLAN_LIMIT_FIELDS` except the ones in `OPTIONAL_PLAN_LIMITS`.

`apiLimit` is the one optional limit. Nothing meters API requests
(`apiRequests` is 0 for every account), the console has no field for it, and
requiring it made every plan the console sent a 400. Omitted, it keeps the
value the plan already had, or starts at 0 on a new plan — which
`metric_usage` reads as uncapped. Sent, it is validated like any other cap.

**Who else reads the catalogue.** A signed-in CUSTOMER reads the same plan
rows through a different door, with a different field set:

| Method | Path                            | Answer                                      |
| ------ | ------------------------------- | ------------------------------------------- |
| GET    | `/api/v1/billing/plans/`        | `{plans: [...]}` — ACTIVE plans only, cheapest first |
| GET    | `/api/v1/billing/subscription/` | `{subscription: {...} \| null}` — the caller's own |

Read-only: there is no gateway, so a plan is granted by an administrator
through `users/<id>/subscription/` and there is nothing for a customer to POST.

`serialize_customer_plan` withholds `status` (an administrative switch),
`subscribers` (how many accounts are on the plan) and `apiLimit` (a cap on
something nothing meters), and Inactive plans are absent entirely.
`serialize_customer_subscription` withholds `assignedBy`, `assignedAt`,
`durationDays` and `isExpired` — the first would hand a customer an employee's
email address. `status` is the resolved one, so an activation past its end date
reads `Past Due` on both sides.

**Where support requests come from.** The queue has ONE writer that is not an
administrator: the public contact form, mounted outside this API so the path a
visitor's browser calls does not say "admin".

| Method | Path                      | Body                                                              |
| ------ | ------------------------- | ----------------------------------------------------------------- |
| POST   | `/api/v1/support/contact/` | `{name, email, firm, country, topic, subject, message}` |

It answers `201 {message, request_id}` and never the stored row. `topic` must
be one of `dummy_data.SUPPORT_TOPICS`; `id`, `submittedAt`, `status`,
`priority` and `assignee` are NOT accepted and are set server-side —
`status` starts at `New` and `priority` is derived from the topic
(`_TOPIC_PRIORITY`, Technical issue and Billing start High, everything else
Medium). A sender who could name their own priority would name Urgent.
Throttled at `public_contact` (10/hour per IP), failing open.

A request filed this way is an ordinary queue record: it lists, filters, sorts
and PATCHes exactly like any other, and there is no second kind of row.

---

## 4. Response shapes

Every list endpoint returns the same envelope:

```json
{
  "items": [ ... ],
  "pagination": {
    "page": 1, "page_size": 50, "total_items": 42,
    "total_pages": 1, "has_next": false, "has_previous": false
  }
}
```

`page_size` defaults to 50 and is capped at 200.

Errors are always one shape:

```json
{ "detail": "A sentence for the user.", "errors": { "field": "message" } }
```

`errors` is present only when the console should mark fields.

---

## 5. Wiring the existing console services

`admin_frontend/src/lib/services/*.js` were written as the seam for exactly
this. Replace each function body; no page, hook or component changes.

```js
// src/lib/api/client.js  (new)
const BASE = import.meta.env.VITE_ADMIN_API_URL || '/api/v1/admin'

export async function api(path, { method = 'GET', body, params } = {}) {
  const url = new URL(`${BASE}${path}`, window.location.origin)
  Object.entries(params ?? {}).forEach(([k, v]) => {
    if (v != null && v !== 'all' && v !== '') url.searchParams.set(k, v)
  })

  const response = await fetch(url, {
    method,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(method === 'GET' ? {} : { 'X-CSRFToken': readCookie('csrftoken') }),
    },
    body: body ? JSON.stringify(body) : undefined,
  })

  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    throw Object.assign(new Error(payload.detail || 'Request failed'), {
      status: response.status,
      errors: payload.errors,
    })
  }

  return response.status === 204 ? null : response.json()
}
```

Then, for example:

```js
// userService.js
export async function listUsers(controls = {}) {
  const { items } = await api('/users/', { params: {
    search: controls.query,
    account_status: controls.filters?.accountStatus,
    subscription: controls.filters?.subscription,
    meeting_status: controls.filters?.meetingStatus,
    sort: controls.sort?.key,
    direction: controls.sort?.direction,
    page: controls.page,
    page_size: 200,
  }})
  return items
}

export const getUser = (id) => api(`/users/${id}/`)
export const updateUser = (id, changes) => api(`/users/${id}/`, { method: 'PATCH', body: changes })
export const setUserStatus = (id, status) => api(`/users/${id}/status/`, { method: 'POST', body: { status } })
export const changeSubscription = (id, body) => api(`/users/${id}/subscription/`, { method: 'POST', body })

// replaces sendPasswordSetup - this is the real operation now
export const generatePassword = (id) => api(`/users/${id}/generate-password/`, { method: 'POST' })
```

Three console-side changes are unavoidable because the product changed:

1. **`sendPasswordSetup` becomes `generatePassword`.** It issues the password
   rather than inviting the customer to choose one, and the response carries
   `credentialsEmailSent`, which the toast should reflect.
2. **`changeSubscription` gains `durationDays`.** Default 30; the dialog should
   offer a custom period for a negotiated deal.
3. **Meeting status gains `outcome`.** The Meetings page needs a control for
   "successful / not continuing / follow-up", because that is what unlocks
   Generate Password.

Passing `page_size: 200` keeps the console's existing client-side filtering and
paging working unchanged. Moving those to the server later is a matter of
passing `controls.page` through and reading `pagination.total_pages`.

`lib/admin/clock.js` should now return the real clock, and `lib/data/` and the
mock body of `AdminAuthContext` can be deleted.

---

## 6. What is real and what is a placeholder

**Real, from the database:** administrators and their sessions, the audit
trail, customers, signup requests, meetings and their whole lifecycle, meeting
reminders, generated passwords and account activation, availability, and every
usage figure except one.

**Placeholder, from `admin/dummy_data.py`:** the plan catalogue, the per-user
subscription assignment, and the support queue. These live in a JSON file at
`KRAIOS_ADMIN_DUMMY_STORE_PATH`, not in Postgres — no migration commits them,
and deleting the file resets them.

They behave like the real thing (validation, uniqueness, the delete guard,
expiry against today), so the console cannot tell the difference. When the plan
model lands, the accessor bodies in that module become ORM calls and no caller
changes.

**Not metered at all:** `apiRequests` reports `0` for every account. Nothing
counts public API calls yet, and inventing a number would be worse than an
honest zero. The dashboard's eighth tile therefore reports AI generation jobs,
which are real.
