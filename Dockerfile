FROM python:3.9

ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8

WORKDIR /plan_etl

COPY requirements.txt .

RUN pip3 install -r requirements.txt

COPY . .

EXPOSE 8077

# Use multiple workers in production; --reload is dev-only and should NOT be in the image
CMD ["uvicorn", "extraction_api:app", "--host", "0.0.0.0", "--port", "8077", "--workers", "4"]
