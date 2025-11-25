#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <DHT.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// Sensor pins
#define DHT_PIN 4
#define LDR_PIN 34
#define TOUCH_PIN 27

// OLED settings
#define SCREEN_WIDTH 128
#define SCREEN_HEIGHT 64
#define OLED_RESET -1

// DHT sensor type
#define DHT_TYPE DHT11

// WiFi credentials
const char* ssid = "Sunil BSNL";
const char* password = "9844007710";

// Server details
const char* serverURL = "http://192.168.1.38:5000";

// Initialize components
DHT dht(DHT_PIN, DHT_TYPE);
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

WiFiClient client;

struct SensorData {
  float temperature;
  float humidity;
  int light;
  int touch;
};

String currentSuggestion = "Check dashboard for AI suggestions";
unsigned long lastSuggestionUpdate = 0;
const unsigned long SUGGESTION_INTERVAL = 30000; // 30 seconds

void setup() {
  Serial.begin(115200);
  
  // Initialize sensors
  dht.begin();
  pinMode(LDR_PIN, INPUT);
  pinMode(TOUCH_PIN, INPUT);
  
  // Initialize OLED
  if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    Serial.println("OLED allocation failed");
    for(;;);
  }
  
  display.display();
  delay(2000);
  display.clearDisplay();
  
  // Connect to WiFi
  connectToWiFi();
  
  Serial.println("Smart Environment System Started");
}

void connectToWiFi() {
  WiFi.begin(ssid, password);
  Serial.print("Connecting to WiFi");
  
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0,0);
  display.println("Connecting WiFi");
  display.display();
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 20) {
    delay(1000);
    Serial.print(".");
    display.print(".");
    display.display();
    attempts++;
  }
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nConnected to WiFi!");
    display.clearDisplay();
    display.setCursor(0,0);
    display.println("WiFi Connected!");
    display.println(WiFi.localIP());
    display.display();
    delay(2000);
  } else {
    Serial.println("\nFailed to connect to WiFi!");
  }
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectToWiFi();
    delay(5000);
    return;
  }
  
  SensorData data = readSensors();
  sendSensorData(data);
  
  // Get new AI suggestion every 30 seconds
  if (millis() - lastSuggestionUpdate > SUGGESTION_INTERVAL) {
    getAISuggestion(data);
    lastSuggestionUpdate = millis();
  }
  
  updateOLED(data);
  delay(10000);
}

SensorData readSensors() {
  SensorData data;
  data.temperature = dht.readTemperature();
  data.humidity = dht.readHumidity();
  data.light = analogRead(LDR_PIN);
  data.touch = digitalRead(TOUCH_PIN);
  
  if (isnan(data.temperature) || isnan(data.humidity)) {
    data.temperature = 0;
    data.humidity = 0;
  }
  
  return data;
}

void sendSensorData(SensorData data) {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    
    StaticJsonDocument<200> doc;
    doc["device"] = "esp32_sensors";
    doc["temperature"] = data.temperature;
    doc["humidity"] = data.humidity;
    doc["light"] = data.light;
    
    String jsonStr;
    serializeJson(doc, jsonStr);
    
    String fullURL = String(serverURL) + "/sensor_data";
    http.begin(client, fullURL);
    http.addHeader("Content-Type", "application/json");
    int httpCode = http.POST(jsonStr);
    
    if (httpCode > 0) {
      Serial.println("Sensor data sent: " + String(httpCode));
    } else {
      Serial.println("HTTP error: " + String(httpCode));
    }
    
    http.end();
  }
}

void getAISuggestion(SensorData data) {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    
    // Create request for AI suggestion
    StaticJsonDocument<300> doc;
    doc["temperature"] = data.temperature;
    doc["humidity"] = data.humidity;
    doc["light"] = data.light;
    doc["request_type"] = "oled_suggestion";
    
    String jsonStr;
    serializeJson(doc, jsonStr);
    
    String fullURL = String(serverURL) + "/get_suggestion";
    http.begin(client, fullURL);
    http.addHeader("Content-Type", "application/json");
    int httpCode = http.POST(jsonStr);
    
    if (httpCode == 200) {
      String response = http.getString();
      DynamicJsonDocument respDoc(512);
      DeserializationError error = deserializeJson(respDoc, response);
      
      if (!error) {
        String suggestion = respDoc["suggestion"];
        if (suggestion.length() > 0) {
          currentSuggestion = suggestion;
          Serial.println("New AI suggestion: " + currentSuggestion);
        }
      }
    }
    
    http.end();
  }
}

void updateOLED(SensorData data) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0,0);
  
  // Display sensor data
  display.println("ENVIRONMENT DATA");
  display.println("---------------");
  display.print("Temp: "); display.print(data.temperature); display.println(" C");
  display.print("Hum:  "); display.print(data.humidity); display.println(" %");
  display.print("Light: "); display.println(data.light);
  display.println("---------------");
  
  // Display AI suggestion (scrolling if needed)
  display.println("AI SUGGESTION:");
  display.setTextSize(1);
  
  // Split long suggestions into multiple lines
  String lines[3];
  splitString(currentSuggestion, lines, 3, 20); // 3 lines, 20 chars each
  
  for (int i = 0; i < 3; i++) {
    if (lines[i].length() > 0) {
      display.println(lines[i]);
    }
  }
  
  display.display();
}

// Helper function to split long strings for OLED display
void splitString(String input, String output[], int maxLines, int maxChars) {
  for (int i = 0; i < maxLines; i++) {
    output[i] = "";
  }
  
  int currentLine = 0;
  int currentPos = 0;
  
  while (input.length() > 0 && currentLine < maxLines) {
    if (input.length() <= maxChars) {
      output[currentLine] = input;
      break;
    }
    
    // Find space to break line
    int breakPos = maxChars;
    for (int i = maxChars; i >= 0; i--) {
      if (input.charAt(i) == ' ') {
        breakPos = i;
        break;
      }
    }
    
    output[currentLine] = input.substring(0, breakPos);
    input = input.substring(breakPos + 1);
    currentLine++;
  }
}