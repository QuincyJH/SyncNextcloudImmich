#!/bin/bash

function main() {
    echo "Starting the application using Docker Compose..."
    docker compose up --build --watch || handle_exception
}
function handle_exception() {
    echo "An error occurred while starting. Exiting..."
    exit 1
}

main