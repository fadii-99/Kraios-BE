# Kraios Backend Workflow API

All REST URLs start with `/api/v1`. Authentication uses the existing secure
cookies. Unsafe requests must include the CSRF cookie and `X-CSRFToken` header.

## REST, background jobs, and WebSockets

Use REST for quick operations: profile reads/edits, project creation, history,
selection, messages, and manual BOQ edits. Generation endpoints and
`download-all` return HTTP `202 Accepted` with a job in one of these states:
`QUEUED`, `PROCESSING`, `COMPLETED`, or `FAILED`.

Celery executes these jobs outside the web request. Redis is the Celery broker
and Django Channels layer. Subscribe to `wss://API_HOST/ws/jobs/{job_id}/` for
live status. Also support `GET /projects/jobs/{job_id}/` as a reconnect/polling
fallback. Once complete, fetch the step history to get the output version.

During the temporary Vercel-to-ngrok setup, use REST polling if that proxy does
not forward WebSocket upgrades. WebSockets are configured for the final Nginx
deployment through `/ws/`.

## Authentication and email endpoints

All URLs below are relative to `/api/v1`. Before any public `POST`, call
`GET /auth/csrf/` with `credentials: "include"`, then send the returned
`csrfToken` as `X-CSRFToken`. Continue using `credentials: "include"` on every
request.

| Method | URL                               | Use                                      |
| ------ | --------------------------------- | ---------------------------------------- |
| `POST` | `/auth/signup-request/`           | Create the meeting/signup request        |
| `POST` | `/auth/forgot-password/request/`  | Email a six-digit password-reset OTP     |
| `POST` | `/auth/forgot-password/confirm/`  | Verify OTP and set the new password      |

Signup accepts `name`, `firm`, `email`, `country`, `date`, and `time`. A
successful response includes `confirmation_email_sent`. The email confirms the
requested meeting date and time.

Forgot-password request body:

```json
{
  "email": "architect@example.com"
}
```

The request always returns HTTP `202` with a generic message and a
`verification_id`, whether or not an active account exists. This prevents the
UI from revealing registered email addresses. Confirmation body:

```json
{
  "email": "architect@example.com",
  "verification_id": "UUID_FROM_REQUEST",
  "otp": "123456",
  "new_password": "A-strong-new-password"
}
```

On success, password reset revokes existing refresh tokens, clears auth
cookies, and requires a fresh login.

Email templates are versioned in the application and identified by the
`X-Kraios-Template-ID` message header:

| Email                              | Template ID                         |
| ---------------------------------- | ----------------------------------- |
| Signup/meeting confirmation        | `signup_meeting_confirmation_v1`    |
| Forgot-password OTP                | `forgot_password_otp_v1`            |
| Password-change OTP                | `change_password_otp_v1`            |
| Account-deletion OTP               | `delete_account_otp_v1`             |

Gmail SMTP does not provide hosted template IDs. These IDs version the local
Django text/HTML templates in `accounts/templates/emails/`.

## Profile endpoints

| Method    | URL                                   | Use                                             |
| --------- | ------------------------------------- | ----------------------------------------------- |
| `GET`   | `/profile/`                         | View the signed-in user's profile               |
| `PATCH` | `/profile/`                         | Edit profile fields; email is immutable         |
| `POST`  | `/profile/password-change/request/` | Validate passwords and email an OTP             |
| `POST`  | `/profile/password-change/confirm/` | Confirm`verification_id` + `otp`            |
| `POST`  | `/profile/delete-account/request/`  | Validate password and email an OTP              |
| `POST`  | `/profile/delete-account/confirm/`  | Confirm OTP and permanently delete account/data |

OTP codes expire after 10 minutes, are stored only as hashes, are single-use,
and allow five incorrect attempts. A new request invalidates the old request.
Changing the password revokes refresh tokens, clears auth cookies, and requires
the user to log in again.
With the console email backend, email is printed in web-container logs. With
the SMTP backend, it is delivered through the configured SMTP account.

## Project endpoints

| Method     | URL                 | Use                                  |
| ---------- | ------------------- | ------------------------------------ |
| `GET`    | `/projects/`      | List the current user's projects     |
| `POST`   | `/projects/`      | Create with `name`; `workflow` is optional |
| `GET`    | `/projects/{id}/` | Details and selected versions        |
| `PATCH`  | `/projects/{id}/` | Rename project                       |
| `DELETE` | `/projects/{id}/` | Delete project                       |

If `workflow` is omitted, the backend selects `COMPLETE`, enabling Steps 1, 2,
3, and 4. Future clients may explicitly send `COMPLETE`, `STEP_1_ONLY`,
`STEP_2_ONLY`, or `STEP_3_ONLY`. Workflow is immutable after creation. No
subscription model or subscription check is present.

## Step 1: 2D floor plans

| Method   | URL                                                     | Body/use                                           |
| -------- | ------------------------------------------------------- | -------------------------------------------------- |
| `GET`  | `/projects/{id}/step-1/history/`                      | All uploaded/generated/edited versions             |
| `POST` | `/projects/{id}/step-1/upload/`                       | Multipart`file`; completes/selects Step 1        |
| `POST` | `/projects/{id}/step-1/generate/`                     | JSON`prompt`; queue generation                   |
| `POST` | `/projects/{id}/step-1/generate/`                     | `prompt` + `parent_version_id`; queue revision |
| `POST` | `/projects/{id}/step-1/versions/{version_id}/select/` | Select any completed version                       |

Each version stores prompt, parent, job, output, owner, status, and timestamps.
If none is explicitly selected, Step 2 persists and uses the latest completed
version.

## Step 2: 3D generation and editing

For `STEP_2_ONLY`, upload its 2D input using multipart `file` at
`POST /projects/{id}/step-2/input/`. Complete workflows use Step 1 output.

| Method   | URL                                                     | Body/use                                                    |
| -------- | ------------------------------------------------------- | ----------------------------------------------------------- |
| `GET`  | `/projects/{id}/step-2/conversation/`                 | Ordered chat and attachments                                |
| `GET`  | `/projects/{id}/step-2/history/`                      | All generated/edited 3D versions                            |
| `POST` | `/projects/{id}/step-2/generate/`                     | JSON`prompt`; queue generation                            |
| `POST` | `/projects/{id}/step-2/edit/`                         | Multipart`original_version_id`, `mask`, `instruction` |
| `POST` | `/projects/{id}/step-2/versions/{version_id}/select/` | Select completed 3D version                                 |

An edit stores its original version/image, mask, instruction, job, result, and
timestamps. If none is selected, Step 3 persists and uses the latest completed
3D version.

## Step 3: BOQ conversation and structured versions

| Method   | URL                                                     | Body/use                                            |
| -------- | ------------------------------------------------------- | --------------------------------------------------- |
| `GET`  | `/projects/{id}/step-3/conversation/`                 | Ordered messages/files                              |
| `POST` | `/projects/{id}/step-3/conversation/`                 | `content`, optional repeated `files`            |
| `POST` | `/projects/{id}/step-3/generate/`                     | `prompt`, optional repeated `files`; queue job  |
| `GET`  | `/projects/{id}/step-3/versions/`                     | Generated and manual versions                       |
| `POST` | `/projects/{id}/step-3/versions/manual/`              | `structured_data`, optional `parent_version_id` |
| `POST` | `/projects/{id}/step-3/versions/{version_id}/select/` | Select completed BOQ                                |

`structured_data` is JSON separate from chat messages. Manual editing creates a
new immutable version instead of overwriting history. Expected shape:

```json
{
  "columns": ["Item", "Description", "Quantity", "Unit", "Rate", "Amount"],
  "rows": [{"Item": "1", "Description": "Concrete", "Quantity": 10}]
}
```

## Step 4: downloads

| Method   | URL                                            | Use                           |
| -------- | ---------------------------------------------- | ----------------------------- |
| `GET`  | `/projects/{id}/assets/`                     | List assets and download URLs |
| `GET`  | `/projects/{id}/assets/{asset_id}/download/` | Individual download           |
| `POST` | `/projects/{id}/download-all/`               | Queue archive creation        |

The archive contains `2D/`, `3D/`, `BOQ/`, `Masks/`, and `Uploads/`. When its
job completes, `output_asset` contains the downloadable archive asset ID.

## Replacing AI placeholders

Celery dispatches in `projects/tasks.py`. The placeholder waits for
`AI_PLACEHOLDER_DELAY_SECONDS`, publishes progress, and produces a tiny test
image/table. Replace the internals of `complete_floor_plan_job`,
`complete_three_d_job`, and `complete_boq_job` with real agents while preserving
their database completion behavior. AI work should remain in Celery, never in
the API view.
