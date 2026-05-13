FROM mirror.gcr.io/library/python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Let GID 0 write under /app — required by OpenShift's restricted-v2 SCC
# which runs containers as a random non-root UID. Harmless elsewhere.
RUN chgrp -R 0 /app && chmod -R g+rwX /app

CMD ["python3", "main.py"]
