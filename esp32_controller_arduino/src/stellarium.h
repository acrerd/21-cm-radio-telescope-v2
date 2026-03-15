// stellarium.h - Stellarium Telescope Protocol server

#ifndef STELLARIUM_H
#define STELLARIUM_H

#include <Arduino.h>
#include <WiFi.h>

// Initialize Stellarium server
void setupStellariumServer();

// Handle Stellarium clients (call from loop)
void handleStellariumServer();

#endif // STELLARIUM_H
