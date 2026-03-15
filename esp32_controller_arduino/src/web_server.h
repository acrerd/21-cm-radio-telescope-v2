// web_server.h - Async Web interface for SRT control

#ifndef WEB_SERVER_H
#define WEB_SERVER_H

#include <ESPAsyncWebServer.h>

// Initialize and start web server
void setupWebServer();

// Handle web server clients (no-op for async, kept for compatibility)
void handleWebServer();

// External server instance
extern AsyncWebServer webServer;

#endif // WEB_SERVER_H
