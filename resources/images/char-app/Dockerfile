FROM python:3.12-slim

RUN pip install --no-cache-dir pyzmq

COPY char_app.py /char_app.py

CMD ["python", "-u", "/char_app.py"]
