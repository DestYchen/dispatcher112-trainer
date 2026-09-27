FROM postgres:15
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
ENV PYTHONPATH=/scripts
COPY infra/backup/backup.sh infra/backup/entrypoint.sh infra/backup/full_backup.py /scripts/
COPY backend/app/operations/snapshot.py backend/app/operations/program_files.py backend/app/operations/backup_protocol.py backend/app/operations/backup_worker.py backend/app/operations/file_integrity.py /scripts/app/operations/
RUN chmod 755 /scripts/*.sh
ENTRYPOINT ["/scripts/entrypoint.sh"]
