FROM python:2.7-alpine3.7

RUN pip install boto3 kubernetes

COPY ./src /app/

WORKDIR /app

ENTRYPOINT ["python", "task.py"]


