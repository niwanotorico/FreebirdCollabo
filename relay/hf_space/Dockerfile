# Freebird Collab relay — Hugging Face Spaces (Docker SDK) / any Docker host
FROM python:3.12-slim
RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app
COPY --chown=user app.py .
ENV PORT=7860
EXPOSE 7860
CMD ["python3", "app.py"]
