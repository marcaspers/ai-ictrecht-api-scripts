# Eén image voor de command line (testregister.py) en het beheerscherm (Streamlit).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
RUN mkdir -p data output logs

EXPOSE 8501
ENTRYPOINT ["python", "testregister.py"]
CMD ["--help"]
