# Same image as changedetection: its HTTP client is the one BookMyShow already answers
# for the existing watches (a plain python:slim client got 403 on the show-list API).
FROM ghcr.io/dgtlmoon/changedetection.io:latest
WORKDIR /bms
COPY app.py dashboard.py dashboard.html ./
USER 1000:1000
ENTRYPOINT []
CMD ["python3", "-u", "/bms/app.py"]
