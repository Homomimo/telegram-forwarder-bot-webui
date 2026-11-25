# syntax=docker/dockerfile:1
# Includes Telethon, QR dependencies, the receipted TDL build and its corresponding source.
FROM ghcr.io/homomimo/telegram-forwarder-web@sha256:596d0b2fb7648d4827fef791742c262687950684a3571324d80b49ab8fe999d7
USER 0:0
WORKDIR /app
ENV RELAY_MODE=demo RELAY_DATA=/data BIND=0.0.0.0 PORT=8765
ARG APP_VERSION=dev
ENV RELAY_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.title="Telegram Forwarder · Bot + WebUI" org.opencontainers.image.version="${APP_VERSION}"
COPY VERSION channel_input.py accounts.py admin_panel.py clone_receipts.py console_http.py demo_transport.py forward_records.py gen_telethon_session.py launch.py manual_forward.py monitor_clone.py queue_view.py relay_delivery.py task_journal.py tdl-forward.py vps_https.py web_sessions.py ./
COPY static/ ./static/
COPY tools/verify_demo.py ./tools/verify_demo.py
EXPOSE 8765
CMD ["python", "launch.py"]

