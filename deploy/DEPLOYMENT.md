# Kraios Backend: Docker and VPS Setup

This project uses the same application stack locally and on the VPS:

- `web`: Django served by Daphne (HTTP and WebSockets)
- `db`: PostgreSQL 18
- `redis`: Celery broker and Channels message layer
- `worker`: Celery background worker for generation and archives

The database is stored in the named Docker volume `postgres_data`. The web port is bound to `127.0.0.1:8000`, so PostgreSQL is never exposed and the VPS can publish the API through Nginx.

## 1. Local Docker setup

### Requirements

Install Docker Engine or Docker Desktop with the Compose plugin. Confirm both commands work:

```bash
docker --version
docker compose version
```

If Docker reports permission denied for `/var/run/docker.sock`, add your user to the Docker group:

```bash
sudo usermod -aG docker "$USER"
```

Log out of Linux completely and log back in, then verify access:

```bash
docker info
```

Do not make the Docker socket world-writable with `chmod 666`.

### Configure the environment

From the project root:

```bash
cp .env.example .env
```

Edit `.env`. Local development can use:

```dotenv
DJANGO_SECRET_KEY=a-local-development-secret
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8000
DJANGO_SECURE_SSL_REDIRECT=False

POSTGRES_DB=kraios
POSTGRES_USER=kraios_user
POSTGRES_PASSWORD=a-local-database-password
POSTGRES_HOST=db
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
AI_PLACEHOLDER_DELAY_SECONDS=5

# Use real SMTP for signup confirmation, password, and account-deletion emails.
DJANGO_DEFAULT_FROM_EMAIL="Kraios <noreply@example.com>"
DJANGO_EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
DJANGO_EMAIL_HOST=smtp.example.com
DJANGO_EMAIL_PORT=587
DJANGO_EMAIL_HOST_USER=SMTP_USERNAME
DJANGO_EMAIL_HOST_PASSWORD=SMTP_PASSWORD
DJANGO_EMAIL_USE_TLS=True
DJANGO_EMAIL_USE_SSL=False
KRAIOS_SUPPORT_EMAIL=support@example.com
```

Never commit `.env`. It is already listed in `.gitignore` and `.dockerignore`.

### Build and start

```bash
docker compose config
docker compose up -d --build
docker compose ps
```

If port `8000` is already in use, stop the existing application using it before starting this stack. Do not change the public binding on a VPS to `0.0.0.0` unless you intentionally want to bypass Nginx.

The `web` container waits for PostgreSQL and Redis, runs migrations, collects static files, and starts Daphne. The worker handles long-running jobs.

View logs:

```bash
docker compose logs -f web
docker compose logs -f worker
```

Press `Ctrl+C` to leave the logs. The containers keep running.

### Create the first administrator

```bash
docker compose exec web python manage.py createsuperuser
```

Open:

- Admin: `http://localhost:8000/admin/`
- Signup API: `http://localhost:8000/api/v1/auth/signup-request/`
- Forgot-password request: `http://localhost:8000/api/v1/auth/forgot-password/request/`
- Forgot-password confirm: `http://localhost:8000/api/v1/auth/forgot-password/confirm/`
- Login API: `http://localhost:8000/api/v1/auth/login/`
- Current user API: `http://localhost:8000/api/v1/auth/me/`

### Verify the API

Create a signup request:

```bash
curl -X POST http://localhost:8000/api/v1/auth/signup-request/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Test Architect",
    "firm": "Test Firm",
    "email": "architect@example.com",
    "country": "Pakistan",
    "date": "2026-09-15",
    "time": "10:00 AM - 11:00 AM"
  }'
```

In the admin panel:

1. Open **Signup requests** and update the request status.
2. Open **Users** and select the applicant.
3. Use the password-change link to set the initial password.
4. Enable `is_active` and save.

Then log in:

```bash
curl -X POST http://localhost:8000/api/v1/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{
    "email": "architect@example.com",
    "password": "the-password-set-by-admin"
  }'
```

### Run tests inside Docker

```bash
docker compose exec web python manage.py test
```

### Stop or restart

```bash
docker compose stop
docker compose start
docker compose restart web
```

Remove containers while keeping database data:

```bash
docker compose down
```

Do not run `docker compose down -v` unless you intentionally want to delete the PostgreSQL volume and all database data.

## 2. Deploy on an Ubuntu VPS

These steps assume:

- Ubuntu 22.04 or 24.04
- a domain such as `api.example.com`
- the domain's DNS `A` record points to the VPS IP
- SSH access with a sudo-enabled user

### Install the server packages

SSH into the VPS and install Docker, Compose, Nginx, and Certbot. You may install Docker Engine from Docker's official Ubuntu repository. If you use Ubuntu's packages:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 nginx certbot python3-certbot-nginx
sudo systemctl enable --now docker
sudo systemctl enable --now nginx
sudo usermod -aG docker "$USER"
```

Log out and reconnect after adding your user to the Docker group, then verify:

```bash
docker --version
docker compose version
```

### Upload the project

The recommended location is `/opt/kraios-backend`.

If the project is in Git:

```bash
sudo mkdir -p /opt/kraios-backend
sudo chown "$USER":"$USER" /opt/kraios-backend
git clone YOUR_REPOSITORY_URL /opt/kraios-backend
cd /opt/kraios-backend
```

If Git is not being used, upload the project folder with `rsync` or SFTP and then enter that directory.

### Create production secrets

Generate two different random values:

```bash
openssl rand -base64 48
openssl rand -base64 48
```

Use one for `DJANGO_SECRET_KEY` and one for `POSTGRES_PASSWORD`.

```bash
cp .env.example .env
nano .env
```

Production `.env` example:

```dotenv
DJANGO_SECRET_KEY=PASTE_THE_FIRST_RANDOM_VALUE
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=api.example.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://api.example.com
DJANGO_SECURE_SSL_REDIRECT=False

POSTGRES_DB=kraios
POSTGRES_USER=kraios_user
POSTGRES_PASSWORD=PASTE_THE_SECOND_RANDOM_VALUE
POSTGRES_HOST=db
POSTGRES_PORT=5432
REDIS_URL=redis://redis:6379/0
AI_PLACEHOLDER_DELAY_SECONDS=5
```

Keep `DJANGO_SECURE_SSL_REDIRECT=False` until HTTPS has been issued successfully.

### Start the production containers

```bash
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 web
```

Create the administrator:

```bash
docker compose exec web python manage.py createsuperuser
```

Run Django's deployment checks:

```bash
docker compose exec web python manage.py check --deploy
```

Review every warning before going live. Some HTTPS-related warnings are expected until the next step is complete.

### Configure Nginx

Copy the supplied configuration and replace the example domain:

```bash
sudo cp deploy/nginx/kraios-backend.conf /etc/nginx/sites-available/kraios-backend
sudo nano /etc/nginx/sites-available/kraios-backend
```

Change this line:

```nginx
server_name api.example.com;
```

Enable the site:

```bash
sudo ln -s /etc/nginx/sites-available/kraios-backend /etc/nginx/sites-enabled/kraios-backend
sudo nginx -t
sudo systemctl reload nginx
```

If the symlink already exists, do not create it again.

At this point `http://api.example.com/admin/` should reach Django.

### Enable HTTPS

Make sure DNS already points to the VPS, then run:

```bash
sudo certbot --nginx -d api.example.com
```

After HTTPS works, edit `.env`:

```dotenv
DJANGO_SECURE_SSL_REDIRECT=True
```

Restart Django so it reads the updated environment:

```bash
docker compose up -d --force-recreate web
docker compose exec web python manage.py check --deploy
```

The API and admin should now be used only through `https://api.example.com`.

### Configure the firewall

Allow SSH before enabling the firewall so you do not lock yourself out:

```bash
sudo ufw allow OpenSSH
sudo ufw allow "Nginx Full"
sudo ufw enable
sudo ufw status
```

Port `8000` does not need a public firewall rule because Compose binds it only to `127.0.0.1`.

Nginx must forward WebSocket upgrades for `/ws/`. The supplied configuration already includes the required upgrade headers.

## 3. Common VPS operations

### View status and logs

```bash
docker compose ps
docker compose logs -f web
docker compose logs -f db
```

### Deploy an update

```bash
cd /opt/kraios-backend
git pull
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 web
```

Migrations and static-file collection run automatically when the web container is recreated.

### Database backup

Create a backup directory and dump PostgreSQL:

```bash
mkdir -p backups
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backups/kraios.sql
```

Copy backups to another machine or storage provider. A backup stored only on the same VPS does not protect against VPS loss.

Restore into an empty or intentionally replaceable database only after confirming the target:

```bash
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"' < backups/kraios.sql
```

### Important database-password rule

The official PostgreSQL image uses `POSTGRES_PASSWORD` only when initializing an empty data volume. Changing the value in `.env` later does not automatically change the password inside an existing database. Rotate an existing database password through PostgreSQL itself, then update `.env` to match.

## 4. Existing SQLite data

Docker starts a fresh PostgreSQL database. The existing local `db.sqlite3` file is not automatically imported. If it contains data you need, export and import that data deliberately before using PostgreSQL in production.
