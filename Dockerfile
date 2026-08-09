FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN mkdir -p server/data
EXPOSE 9090
CMD ["python3", "server/eryu.py"]
