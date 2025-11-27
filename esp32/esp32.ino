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
const char* serverURL = "http://192.168.1.38:5003";

// Initialize components
DHT dht(DHT_PIN, DHT_TYPE);
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

WiFiClient client;

struct SensorData {
  float temperature;
  float humidity;
  int light;
};

String currentSuggestion = "Ready for AI commands";
String currentActivity = "No active task";
String oledDisplayText = "Smart Environment"; // Default OLED text
unsigned long lastSuggestionUpdate = 0;
const unsigned long SUGGESTION_INTERVAL = 30000;
String pendingRequestId = "";

// Command polling
unsigned long lastCommandCheck = 0;
const unsigned long COMMAND_CHECK_INTERVAL = 2000; // Check every 2 seconds

void setup() {
  Serial.begin(115200);
  
  // Initialize sensors
  dht.begin();
  pinMode(LDR_PIN, INPUT);
  
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
  
  // Set initial OLED display
  updateOLEDDisplay("System Ready", "Waiting for AI...");
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
  
  // Check for pending requests that need sensor data
  checkForPendingRequests(data);
  
  // Check for new commands from server (OLED display)
  if (millis() - lastCommandCheck > COMMAND_CHECK_INTERVAL) {
    checkForCommands();
    lastCommandCheck = millis();
  }
  
  // Get new AI suggestion every 30 seconds
  if (millis() - lastSuggestionUpdate > SUGGESTION_INTERVAL) {
    getAISuggestion(data);
    lastSuggestionUpdate = millis();
  }
  
  updateOLED(data);
  delay(2000);
}

SensorData readSensors() {
  SensorData data;
  data.temperature = dht.readTemperature();
  data.humidity = dht.readHumidity();
  data.light = analogRead(LDR_PIN);  // LDR sensor reading
  
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
    doc["light"] = data.light;  // LDR value
    
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

void checkForPendingRequests(SensorData data) {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    
    String fullURL = String(serverURL) + "/get_pending_request";
    http.begin(client, fullURL);
    int httpCode = http.GET();
    
    if (httpCode == 200) {
      String response = http.getString();
      DynamicJsonDocument doc(512);
      DeserializationError error = deserializeJson(doc, response);
      
      if (!error && doc.containsKey("request_id") && doc["request_id"] != "") {
        String newRequestId = doc["request_id"].as<String>();
        if (newRequestId != pendingRequestId) {
          pendingRequestId = newRequestId;
          Serial.println("📥 Received NEW pending request: " + pendingRequestId);
          provideSensorDataForRequest(pendingRequestId, data);
        }
      }
    }
    
    http.end();
  }
}

void provideSensorDataForRequest(String requestId, SensorData data) {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    
    StaticJsonDocument<300> doc;
    doc["sensor_data"]["temperature"] = data.temperature;
    doc["sensor_data"]["humidity"] = data.humidity;
    doc["sensor_data"]["light"] = data.light;
    
    String jsonStr;
    serializeJson(doc, jsonStr);
    
    String fullURL = String(serverURL) + "/provide_sensor_data/" + requestId;
    http.begin(client, fullURL);
    http.addHeader("Content-Type", "application/json");
    int httpCode = http.POST(jsonStr);
    
    if (httpCode == 200) {
      Serial.println("✅ Sensor data provided for request: " + requestId);
      String response = http.getString();
      
      // Parse the response to get AI suggestions and commands
      DynamicJsonDocument respDoc(1024);
      DeserializationError error = deserializeJson(respDoc, response);
      
      if (!error) {
        String aiResponse = respDoc["response"];
        if (aiResponse.length() > 0) {
          currentSuggestion = extractShortSuggestion(aiResponse);
          currentActivity = extractActivity(aiResponse);
          Serial.println("AI Activity: " + currentActivity);
          Serial.println("AI Suggestion: " + currentSuggestion);
          
          // Update OLED with activity
          oledDisplayText = "Activity: " + currentActivity;
        }
      }
    } else {
      Serial.println("❌ Failed to provide sensor data: " + String(httpCode));
    }
    
    http.end();
  }
}

void checkForCommands() {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    
    String fullURL = String(serverURL) + "/get_commands/esp32";
    http.begin(client, fullURL);
    int httpCode = http.GET();
    
    if (httpCode == 200) {
      String response = http.getString();
      DynamicJsonDocument doc(1024);
      DeserializationError error = deserializeJson(doc, response);
      
      if (!error) {
        // Check for OLED display command
        if (doc.containsKey("oled_text")) {
          String newOledText = doc["oled_text"].as<String>();
          if (newOledText != oledDisplayText) {
            oledDisplayText = newOledText;
            Serial.println("📟 New OLED Text: " + oledDisplayText);
            // Update OLED immediately with just the text
            updateOLEDDisplay("AI Command:", oledDisplayText);
            
            // Clear the command after processing
            clearCommandsOnServer();
          }
        }
      }
    }
    
    http.end();
  }
}

void clearCommandsOnServer() {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    
    String fullURL = String(serverURL) + "/clear_commands/esp32";
    http.begin(client, fullURL);
    http.addHeader("Content-Type", "application/json");
    http.POST("{}");
    http.end();
    
    Serial.println("✅ ESP32 commands cleared on server");
  }
}

void getAISuggestion(SensorData data) {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    
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

String extractShortSuggestion(String fullResponse) {
  // Extract a short suggestion from the full AI response for OLED display
  if (fullResponse.length() < 60) {
    return fullResponse;
  }
  
  // Find the first actionable suggestion
  int periodPos = fullResponse.indexOf('.');
  if (periodPos > 0 && periodPos < 50) {
    return fullResponse.substring(0, periodPos + 1);
  }
  
  // Fallback: take first 50 characters
  return fullResponse.substring(0, 50) + "...";
}

String extractActivity(String fullResponse) {
  // Create a lowercase copy of the response
  String lowerResponse = fullResponse;
  lowerResponse.toLowerCase();
  
  // Extract the main activity from AI response
  if (lowerResponse.indexOf("study") >= 0 || lowerResponse.indexOf("learn") >= 0) {
    return "Studying";
  } else if (lowerResponse.indexOf("sleep") >= 0 || lowerResponse.indexOf("rest") >= 0) {
    return "Sleeping";
  } else if (lowerResponse.indexOf("work") >= 0 || lowerResponse.indexOf("focus") >= 0) {
    return "Working";
  } else if (lowerResponse.indexOf("read") >= 0) {
    return "Reading";
  } else if (lowerResponse.indexOf("relax") >= 0 || lowerResponse.indexOf("chill") >= 0) {
    return "Relaxing";
  } else if (lowerResponse.indexOf("yoga") >= 0 || lowerResponse.indexOf("meditate") >= 0) {
    return "Meditation";
  }
  
  return "General Activity";
}

void updateOLED(SensorData data) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0,0);
  
  // Display current activity or command
  if (oledDisplayText.length() > 0) {
    display.println(oledDisplayText);
  } else {
    display.println("ACTIVITY: " + currentActivity);
  }
  
  display.println("---------------");
  
  // Display sensor data
  display.print("Temp: "); display.print(data.temperature); display.println(" C");
  display.print("Hum:  "); display.print(data.humidity); display.println(" %");
  display.print("LDR:  "); display.println(data.light);
  display.println("---------------");
  
  // Display AI suggestion
  display.println("AI SUGGESTION:");
  display.setTextSize(1);
  
  String lines[3];
  splitString(currentSuggestion, lines, 3, 20);
  
  for (int i = 0; i < 3; i++) {
    if (lines[i].length() > 0) {
      display.println(lines[i]);
    }
  }
  
  display.display();
}

void updateOLEDDisplay(String title, String message) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0,0);
  
  // Display title
  display.println(title);
  display.println("---------------");
  
  // Display message with word wrap
  String lines[4];
  splitString(message, lines, 4, 20);
  
  for (int i = 0; i < 4; i++) {
    if (lines[i].length() > 0) {
      display.println(lines[i]);
    }
  }
  
  display.display();
  
  // Keep this display for 5 seconds then return to normal
  delay(5000);
}

void splitString(String input, String output[], int maxLines, int maxChars) {
  for (int i = 0; i < maxLines; i++) {
    output[i] = "";
  }
  
  int currentLine = 0;
  
  while (input.length() > 0 && currentLine < maxLines) {
    if (input.length() <= maxChars) {
      output[currentLine] = input;
      break;
    }
    
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