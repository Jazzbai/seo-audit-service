# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set environment variables to ensure good container practices
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set the working directory inside the container
WORKDIR /app

# Copy only the requirements file first to leverage Docker's build cache.
# This step is only re-run if requirements.txt changes.
COPY requirements.txt .

# Install the dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application's source code into the container
COPY . .

# The commands to run the web server and worker will be specified in docker-compose.yml
# This makes the Dockerfile more reusable. 