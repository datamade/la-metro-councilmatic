FROM python:3.6-slim-stretch
LABEL maintainer "DataMade <info@datamade.us>"

ENV PYTHONUNBUFFERED=1

RUN mkdir /app
WORKDIR /app

COPY ./requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
