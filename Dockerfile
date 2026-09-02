FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# --system users get home=/nonexistent by default on Debian, which is not
# writable. The BOQ agent's FileSessionManager resolves its default session
# directory from the user's home, so give django a real, writable one.
RUN addgroup --system django \
    && adduser --system --ingroup django --home /home/django django \
    && mkdir -p /home/django \
    && chown django:django /home/django
ENV HOME=/home/django

COPY requirements.txt .
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

COPY --chown=django:django . .

RUN mkdir -p /app/media /app/ai_state \
    && chown django:django /app/media /app/ai_state

USER django

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
