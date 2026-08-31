sudo docker compose down
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs --tail=100 web worker

ormal mode — recommended

Starts everything in background:

```bash
sudo docker compose up -d --build
```

Then watch live errors/logs in another terminal:

```bash
sudo docker compose logs -f --tail=100 web worker
```

This shows live Django/API logs and logs from both `worker-1` and `worker-2`.

For only one service:

```bash
sudo docker compose logs -f web
sudo docker compose logs -f worker
sudo docker compose logs -f db
```

No need to run `manage.py runserver` in this mode.

### Debug mode — when editing Django code live

Use it only after stopping the normal web container, otherwise port `8000` conflicts:

```bash
sudo docker compose stop web

sudo docker compose run --rm --service-ports --volume "$PWD:/app" web python manage.py runserver 0.0.0.0:8000
```

It gives you live Django output and auto-reload for code mounted from your folder. When finished, press `Ctrl+C`, then restore normal mode:

```bash
sudo docker compose up -d --build web
```

For live worker debugging, normally use logs. If you specifically edit worker/task code and want the Celery process in the terminal:

```bash
sudo docker compose stop worker

sudo docker compose run --rm --volume "$PWD:/app" worker celery -A config worker --loglevel=info --concurrency=2
```

`docker compose run` is for temporary/manual debugging. `docker compose up -d` is your normal server command.

“Build” means Docker creates a new image using the `Dockerfile`, installs packages from `requirements.txt`, and copies current backend code inside it. Run `--build` after changing Python code, `requirements.txt`, Dockerfile, or Compose setup. The `CACHED` lines in your output mean Docker reused unchanged layers, so that build was fast.

The Bake/buildx warning is harmless; your build completed successfully.
