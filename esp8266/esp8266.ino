#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>
#include <ArduinoJson.h>

// WiFi credentials
const char* ssid = "WiFi_SSID";
const char* password = "WiFi_Password";

// Server details
const char* serverURL = "Server_IP_Address"; // e.g., "http://

// RGB LED pins
#define RED_PIN 12    // GPIO12
#define GREEN_PIN 13  // GPIO13  
#define BLUE_PIN 15   // GPIO15

// Buzzer pin
#define BUZZER_PIN 14 // GPIO14

WiFiClient client;
HTTPClient http;

void setup() {
  Serial.begin(115200);
  
  // Initialize pins
  pinMode(RED_PIN, OUTPUT);
  pinMode(GREEN_PIN, OUTPUT);
  pinMode(BLUE_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  
  // Turn off initially
  digitalWrite(RED_PIN, LOW);
  digitalWrite(GREEN_PIN, LOW);
  digitalWrite(BLUE_PIN, LOW);
  digitalWrite(BUZZER_PIN, LOW);
  
  connectToWiFi();
  
  Serial.println("ESP8266 RGB+Buzzer Started");
  Serial.println("Pins: R=GPIO12, G=GPIO13, B=GPIO15, Buzzer=GPIO14");
}

void connectToWiFi() {
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(1000);
    Serial.print(".");
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nConnected to WiFi!");
    Serial.print("IP Address: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nFailed to connect to WiFi!");
  }
}

void loop() {
  if (WiFi.status() == WL_CONNECTED) {
    checkCommands();
  } else {
    Serial.println("WiFi disconnected! Reconnecting...");
    connectToWiFi();
  }
  
  delay(2000);
}

void checkCommands() {
  // FIX: Use WiFiClient with HTTPClient
  String fullURL = String(serverURL) + "/get_commands/esp8266";
  
  // Updated HTTPClient usage
  http.begin(client, fullURL);
  
  int httpCode = http.GET();
  
  if (httpCode > 0) {
    Serial.println("Commands check: " + String(httpCode));
    
    if (httpCode == 200) {
      String payload = http.getString();
      Serial.println("Received: " + payload);
      
      DynamicJsonDocument doc(512);
      DeserializationError error = deserializeJson(doc, payload);
      
      if (!error) {
        if (doc.containsKey("rgb_color")) {
          String color = doc["rgb_color"];
          setRGBColor(color);
        }
        
        if (doc.containsKey("buzzer_action")) {
          String action = doc["buzzer_action"];
          handleBuzzer(action);
        }
      } else {
        Serial.println("JSON parsing error");
      }
    }
  } else {
    Serial.println("HTTP error: " + String(httpCode));
  }
  
  http.end();
}

void setRGBColor(String color) {
  // Turn off all
  digitalWrite(RED_PIN, LOW);
  digitalWrite(GREEN_PIN, LOW);
  digitalWrite(BLUE_PIN, LOW);
  
  Serial.println("Setting RGB: " + color);
  
  if (color == "red") {
    digitalWrite(RED_PIN, HIGH);
  } else if (color == "green") {
    digitalWrite(GREEN_PIN, HIGH);
  } else if (color == "blue") {
    digitalWrite(BLUE_PIN, HIGH);
  } else if (color == "yellow") {
    digitalWrite(RED_PIN, HIGH);
    digitalWrite(GREEN_PIN, HIGH);
  } else if (color == "purple") {
    digitalWrite(RED_PIN, HIGH);
    digitalWrite(BLUE_PIN, HIGH);
  } else if (color == "cyan") {
    digitalWrite(GREEN_PIN, HIGH);
    digitalWrite(BLUE_PIN, HIGH);
  } else if (color == "white") {
    digitalWrite(RED_PIN, HIGH);
    digitalWrite(GREEN_PIN, HIGH);
    digitalWrite(BLUE_PIN, HIGH);
  } else if (color == "off") {
    // Already off
  } else {
    Serial.println("Unknown color: " + color);
  }
}

void handleBuzzer(String action) {
  Serial.println("Buzzer action: " + action);
  
  if (action == "beep") {
    digitalWrite(BUZZER_PIN, HIGH);
    delay(200);
    digitalWrite(BUZZER_PIN, LOW);
  } else if (action == "double_beep") {
    for(int i = 0; i < 2; i++) {
      digitalWrite(BUZZER_PIN, HIGH);
      delay(100);
      digitalWrite(BUZZER_PIN, LOW);
      delay(100);
    }
  } else if (action == "alarm") {
    for(int i = 0; i < 5; i++) {
      digitalWrite(BUZZER_PIN, HIGH);
      delay(500);
      digitalWrite(BUZZER_PIN, LOW);
      delay(500);
    }
  } else if (action == "off") {
    digitalWrite(BUZZER_PIN, LOW);
  } else {
    Serial.println("Unknown buzzer action: " + action);
    digitalWrite(BUZZER_PIN, LOW);
  }
}