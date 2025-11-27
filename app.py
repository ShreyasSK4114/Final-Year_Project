from flask import Flask, request, jsonify, render_template_string, send_from_directory
import os
from datetime import datetime
from dotenv import load_dotenv
import requests
import json
import re
import threading
import mysql.connector
from mysql.connector import Error
import google.generativeai as genai
from pathlib import Path
import time

load_dotenv()
app = Flask(__name__)

# API Keys
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)

# MySQL configuration
MYSQL_CONFIG = {
    'host': os.getenv('MYSQL_HOST', 'localhost'),
    'user': os.getenv('MYSQL_USER', 'root'),
    'password': os.getenv('MYSQL_PASSWORD', ''),
    'database': os.getenv('MYSQL_DATABASE', 'smart_environment'),
    'port': os.getenv('MYSQL_PORT', 3306)
}

# Store pending requests and sensor data
pending_requests = {}
pending_lock = threading.Lock()
current_sensor_data = {"temperature": 0, "humidity": 0, "light": 0}
esp8266_commands = {}

# Smart scanning control
last_sensor_scan = 0
SCAN_COOLDOWN = 10  # seconds between scans

# Create static directory for favicon
static_dir = Path('static')
static_dir.mkdir(exist_ok=True)

def get_db_connection():
    """Create and return MySQL database connection"""
    try:
        connection = mysql.connector.connect(**MYSQL_CONFIG)
        return connection
    except Error as e:
        print(f"❌ Error connecting to MySQL: {e}")
        return None

def init_database():
    """Initialize database tables"""
    try:
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor()
            
            # Create conversations table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    session_id VARCHAR(255) NOT NULL,
                    message_type ENUM('user', 'assistant', 'system') NOT NULL,
                    content TEXT NOT NULL,
                    metadata JSON,
                    sensor_data JSON,
                    request_id VARCHAR(255),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_session_id (session_id),
                    INDEX idx_created_at (created_at),
                    INDEX idx_request_id (request_id)
                )
            """)
            
            # Create environment_changes table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS environment_changes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    session_id VARCHAR(255) NOT NULL,
                    request_id VARCHAR(255) NOT NULL,
                    factor ENUM('temperature', 'humidity', 'light', 'fan_speed', 'rgb_color') NOT NULL,
                    previous_value VARCHAR(100),
                    new_value VARCHAR(100),
                    reasoning TEXT,
                    activity_context VARCHAR(100),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_session_id (session_id),
                    INDEX idx_request_id (request_id),
                    INDEX idx_activity (activity_context),
                    INDEX idx_created_at (created_at)
                )
            """)
            
            connection.commit()
            cursor.close()
            connection.close()
            print("✅ Database tables initialized successfully")
            
    except Error as e:
        print(f"❌ Error initializing database: {e}")

def extract_json_from_response(text):
    """Extract JSON from response text"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        json_match = re.search(r'(?:json)?\s*(.*?)\s*', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass
        brace_match = re.search(r'\{.*\}', text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass
    return None

def get_predefined_queries(session_id):
    """Return ONLY the two predefined SQL queries - NO MODEL GENERATED SQL"""
    print(f"📊 Generating predefined queries for session: {session_id}")
    
    predefined_queries = [
        {
            "purpose": "Get recent conversation history for context",
            "query": """SELECT 
                message_type,
                content,
                metadata,
                sensor_data,
                created_at
            FROM conversations
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT 20""",
            "parameters": [session_id]
        },
        {
            "purpose": "Get recent environment changes for context",
            "query": """SELECT 
                factor,
                previous_value,
                new_value,
                reasoning,
                activity_context,
                created_at
            FROM environment_changes
            WHERE session_id = %s
            ORDER BY created_at DESC
            LIMIT 15""",
            "parameters": [session_id]
        }
    ]
    
    print(f"🛡  PREDEFINED QUERIES: Using fixed queries only, no model-generated SQL")
    return predefined_queries

# Smart Scanning Functions
def can_scan_now():
    """Check if we can scan now (respect cooldown)"""
    global last_sensor_scan
    current_time = time.time()
    return (current_time - last_sensor_scan) >= SCAN_COOLDOWN

def update_scan_time():
    """Update last scan time"""
    global last_sensor_scan
    last_sensor_scan = time.time()

def needs_explicit_scan(user_message):
    """Check if user message requires explicit sensor scan"""
    scan_keywords = [
        'scan', 'check environment', 'current conditions', 'real-time',
        'right now', 'live data', 'update sensors'
    ]
    
    user_lower = user_message.lower()
    return any(keyword in user_lower for keyword in scan_keywords)

# LLM2 - Gemini Flash Classifier - STRICTLY NO SQL GENERATION
def classify_and_generate_queries(user_message, session_id):
    """Classify if user message needs sensor data or prehistoric data - NO SQL GENERATION"""
    print(f"🎯 CLASSIFIER: Starting classification for message: '{user_message}'")
    
    # Check if this is an explicit scan request
    if needs_explicit_scan(user_message):
        print("🔍 EXPLICIT SCAN REQUEST DETECTED")
        if can_scan_now():
            return {
                "needs_sensor_data": True,
                "message_type": "real_time_scan",
                "reasoning": "Explicit scan request detected",
                "sql_queries": []
            }
        else:
            return {
                "needs_sensor_data": False,
                "message_type": "cached_data_response",
                "reasoning": "Explicit scan requested but in cooldown, using cached data",
                "sql_queries": get_predefined_queries(session_id)
            }
    
    system_msg = (
        "You are a strict JSON-only classifier. Your ONLY purpose is to classify if the user message "
        "requires CURRENT sensor data or can be answered using HISTORICAL data. "
        "NEVER generate SQL queries, code, or any database fragments. "
        "Output ONLY a single JSON object with keys: needs_sensor_data (true/false), "
        "message_type (one of: real_time_optimization/past_data_query/contextual_adjustment/explanation_request), "
        "reasoning (short string explaining your classification). "
        "No extra text, no SQL, no queries."
    )

    user_prompt = f"""
USER QUESTION: "{user_message}"

Return ONLY this JSON format:
{{
  "needs_sensor_data": true/false,
  "message_type": "real_time_optimization/past_data_query/contextual_adjustment/explanation_request",
  "reasoning": "brief explanation for classification"
}}
"""

    try:
        print(f"🔍 CLASSIFIER: Sending request to Gemini for classification...")
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "google/gemini-2.5-flash",
                "messages": [{"role": "system", "content": system_msg}, {"role": "user", "content": user_prompt}],
                "max_tokens": 120,
                "temperature": 0.0,
                "stop": ["\nSELECT", "\nINSERT", "```sql", "SQL", "query"]
            },
            timeout=20
        )
        resp.raise_for_status()
        result = resp.json()

        # Safe default - assume sensor data needed
        classification = {
            "needs_sensor_data": True,
            "message_type": "real_time_optimization",
            "reasoning": "Fallback: classification failed or unparsable model output",
            "sql_queries": []
        }

        if "choices" in result and len(result["choices"]) > 0:
            content = result["choices"][0].get("message", {}).get("content", "").strip()
            print(f"📨 CLASSIFIER: Raw response from Gemini: {content}")
            
            parsed = extract_json_from_response(content) if content else None

            if isinstance(parsed, dict):
                needs = parsed.get("needs_sensor_data", True)
                needs_bool = needs.strip().lower() in ("true","1","yes") if isinstance(needs, str) else bool(needs)
                classification["needs_sensor_data"] = needs_bool
                classification["message_type"] = parsed.get("message_type", classification["message_type"])
                classification["reasoning"] = parsed.get("reasoning", classification["reasoning"])
                print(f"✅ CLASSIFIER: Successfully parsed classification from Gemini")

        # CRITICAL: ALWAYS use predefined queries for prehistoric data - NO MODEL SQL
        if not classification["needs_sensor_data"]:
            classification["sql_queries"] = get_predefined_queries(session_id)
            print(f"📊 CLASSIFIER: Prehistoric data needed - Using PREDEFINED queries only")
        else:
            classification["sql_queries"] = []
            print(f"📊 CLASSIFIER: Sensor data needed - No SQL queries required")

        print(f"🎯 CLASSIFIER FINAL RESULT: needs_sensor_data={classification['needs_sensor_data']}, type={classification['message_type']}")
        return classification

    except Exception as e:
        print(f"❌ CLASSIFIER ERROR: Gemini classification failed: {e}")
        return {"needs_sensor_data": True, "message_type": "real_time_optimization", "sql_queries": [], "reasoning": "Fallback due to classifier error"}


def execute_sql_queries(sql_queries):
    """Execute ONLY predefined SQL queries with safety checks"""
    print(f"🛠  SQL EXECUTION: Starting execution of {len(sql_queries)} predefined queries")
    
    results = {}

    # Allowed target tables - only our predefined tables
    allowed_tables = {"conversations", "environment_changes"}

    connection = get_db_connection()
    if not connection:
        print("❌ SQL EXECUTION: Database connection failed")
        return {"error": "Database connection failed"}

    try:
        cursor = connection.cursor(dictionary=True)

        for i, query_info in enumerate(sql_queries):
            try:
                query = query_info.get('query', '')
                params = query_info.get('parameters', []) or []

                if not isinstance(query, str) or not query.strip():
                    raise Error("Empty or invalid query provided")

                q_lower = query.lower()

                # Safety: reject '?' placeholders which belong to other DB adapters
                if '?' in query:
                    raise Error("Unsafe placeholder '?' detected — use %s placeholders for mysql.connector")

                # Count %s placeholders and ensure count matches params length
                placeholder_count = query.count('%s')
                if placeholder_count != len(params):
                    raise Error(f"Parameter count mismatch: query expects {placeholder_count} placeholders but got {len(params)} parameters")

                # Safety: only allow queries that reference the allowed tables
                if not any(f" {tbl} " in f" {q_lower} " or f"from {tbl}" in q_lower or f"join {tbl}" in q_lower for tbl in allowed_tables):
                    raise Error(f"Query references disallowed table(s). Allowed tables: {', '.join(allowed_tables)}")

                # Safety: reject obvious multi-statement injections or SQL comments
                if q_lower.count(';') > 1 or '--' in q_lower or '/' in q_lower or '/' in q_lower:
                    raise Error("Query contains suspicious characters (multiple statements or comments)")

                print(f"🛠  SQL EXECUTION: Executing predefined query {i+1}: {query[:100]}...")
                print(f"🛠  SQL EXECUTION: With parameters: {params}")

                cursor.execute(query, params)
                data = cursor.fetchall()

                results[f'query_{i+1}'] = {
                    'purpose': query_info.get('purpose', f'query_{i+1}'),
                    'data': data,
                    'row_count': len(data)
                }
                print(f"✅ SQL EXECUTION: Query {i+1} returned {len(data)} rows")

            except Error as e:
                print(f"❌ SQL EXECUTION ERROR (query_{i+1}): {e}")
                results[f'query_{i+1}'] = {
                    'purpose': query_info.get('purpose', f'query_{i+1}'),
                    'error': str(e),
                    'data': []
                }

        cursor.close()
        connection.close()
        print("✅ SQL EXECUTION: All queries completed successfully")

    except Error as e:
        print(f"❌ DATABASE ERROR: {e}")
        results = {"error": f"Database error: {str(e)}"}

    return results


# Database Operations
def store_conversation(session_id, message_type, content, metadata=None, sensor_data=None, request_id=None):
    """Store conversation in database"""
    print(f"💾 STORING CONVERSATION: session={session_id}, type={message_type}, request_id={request_id}")
    try:
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor()
            
            cursor.execute(
                """INSERT INTO conversations 
                (session_id, message_type, content, metadata, sensor_data, request_id) 
                VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    session_id,
                    message_type,
                    content,
                    json.dumps(metadata) if metadata else None,
                    json.dumps(sensor_data) if sensor_data else None,
                    request_id
                )
            )
            
            connection.commit()
            cursor.close()
            connection.close()
            print(f"✅ CONVERSATION STORED: {message_type} message for session {session_id}")
            
    except Error as e:
        print(f"❌ ERROR STORING CONVERSATION: {e}")

def store_environment_change(session_id, request_id, factor, previous_value, new_value, reasoning, activity_context):
    """Store environment change in database"""
    print(f"💾 STORING ENVIRONMENT CHANGE: session={session_id}, factor={factor}, {previous_value}→{new_value}")
    try:
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor()
            
            cursor.execute(
                """INSERT INTO environment_changes 
                (session_id, request_id, factor, previous_value, new_value, reasoning, activity_context) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (
                    session_id,
                    request_id,
                    factor,
                    str(previous_value),
                    str(new_value),
                    reasoning,
                    activity_context
                )
            )
            
            connection.commit()
            cursor.close()
            connection.close()
            print(f"✅ ENVIRONMENT CHANGE STORED: {factor}: {previous_value}→{new_value}")
            
    except Error as e:
        print(f"❌ ERROR STORING ENVIRONMENT CHANGE: {e}")

# LLM1 - GPT-4o mini Response Generator
def get_llm1_response(prompt_text):
    """Call OpenRouter API (GPT-4o mini)"""
    print(f"🧠 LLM1: Generating response with GPT-4o mini...")
    if not OPENROUTER_API_KEY:
        raise Exception("No API key configured")
    
    try:
        response = requests.post(
            url="https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5003",
                "X-Title": "Smart Environment Assistant"
            },
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt_text}],
                "max_tokens": 1000,
                "temperature": 0.7
            },
            timeout=30
        )
        
        response.raise_for_status()
        result = response.json()
        
        if "choices" in result and len(result["choices"]) > 0:
            message = result["choices"][0].get("message", {})
            content = message.get("content", "").strip()
            
            if content:
                print(f"✅ LLM1: Response generated successfully")
                return content
        
        raise Exception(f"Unexpected response format: {result}")
        
    except Exception as e:
        print(f"❌ LLM1 API ERROR: {repr(e)}")
        raise e

def build_context_from_query_results(user_message, query_results, classification):
    """Build context for LLM1 from SQL query results"""
    print(f"📝 BUILDING CONTEXT: Creating context for LLM1 from query results")
    
    context = f"USER QUESTION: {user_message}\n\n"
    context += f"QUESTION TYPE: {classification['message_type']}\n"
    context += f"REASONING: {classification['reasoning']}\n\n"
    
    if "error" in query_results:
        context += f"DATABASE ERROR: {query_results['error']}\n\n"
        context += "Please acknowledge the data limitation but try to answer based on general knowledge.\n\n"
    else:
        context += "RECENT CONVERSATION HISTORY AND ENVIRONMENT DATA:\n"
        context += "==================================================\n\n"
        
        for key, result in query_results.items():
            context += f"\n--- {result['purpose']} ---\n"
            context += f"Rows returned: {result.get('row_count', 0)}\n"
            
            if 'error' in result:
                context += f"Query Error: {result['error']}\n"
            elif result['data']:
                # Format the data nicely
                for i, row in enumerate(result['data']):
                    context += f"\nRecord {i+1}:\n"
                    for field, value in row.items():
                        if value and str(value).strip():
                            context += f"  {field}: {value}\n"
            else:
                context += "No historical data found\n"
    
    context += f"\nINSTRUCTIONS:\n"
    context += "1. Answer the user's question conversationally using the available historical data\n"
    context += "2. If specific data isn't available, acknowledge this but provide helpful insights\n"
    context += "3. Look for patterns in the conversation history and environment changes\n"
    context += "4. For questions about past activities, reference specific events from the data\n"
    context += "5. Keep your response clear and focused on what the data shows\n\n"
    context += "RESPONSE:"
    
    print(f"✅ CONTEXT BUILT: Context prepared for LLM1")
    return context

def extract_activity_context(user_message):
    """Extract activity context from user message"""
    print(f"🔍 EXTRACTING ACTIVITY: Analyzing user message for activity context")
    activity_keywords = {
        'study': ['study', 'learn', 'exam', 'homework', 'concentrate'],
        'sleep': ['sleep', 'bed', 'tired', 'nap', 'rest'],
        'yoga': ['yoga', 'exercise', 'workout', 'meditate'],
        'read': ['read', 'book', 'novel', 'article'],
        'work': ['work', 'focus', 'project', 'deadline'],
        'relax': ['relax', 'chill', 'unwind', 'tv', 'movie']
    }
    
    user_lower = user_message.lower()
    for activity, keywords in activity_keywords.items():
        if any(keyword in user_lower for keyword in keywords):
            print(f"✅ ACTIVITY DETECTED: {activity}")
            return activity
    print(f"🔍 ACTIVITY: No specific activity detected, using 'general'")
    return 'general'

# Device Control Functions
def control_rgb_color(color):
    """Control RGB LED color"""
    print(f"🎨 CONTROLLING RGB: Setting color to {color}")
    esp8266_commands['rgb_color'] = color
    return f"RGB light set to {color}"

def control_buzzer(duration=None, action=None):
    """Control buzzer with duration or action"""
    print(f"🔊 CONTROLLING BUZZER: Duration={duration}, Action={action}")
    
    if duration:
        esp8266_commands['buzzer_duration'] = duration
        esp8266_commands['buzzer_action'] = 'beep'
        return f"Buzzer set for {duration} seconds"
    elif action:
        esp8266_commands['buzzer_action'] = action
        return f"Buzzer {action}"
    else:
        return "No buzzer action specified"

def set_alarm(duration, alarm_type='standard'):
    """Set alarm with duration and type"""
    print(f"🚨 SETTING ALARM: Duration={duration}s, Type={alarm_type}")
    esp8266_commands['alarm'] = True
    esp8266_commands['alarm_duration'] = duration
    esp8266_commands['alarm_type'] = alarm_type
    return f"Alarm set for {duration} seconds ({alarm_type})"

def set_oled_display(text):
    """Set OLED display text"""
    print(f"📟 SETTING OLED: {text}")
    esp8266_commands['oled_text'] = text
    return f"OLED display set to: {text}"

def parse_device_commands(llm_response, user_message):
    """Parse device commands from LLM response and user message"""
    print(f"🔍 PARSING DEVICE COMMANDS from LLM response and user message")
    
    commands = []
    combined_text = f"{user_message} {llm_response}".lower()
    
    # Parse RGB color commands
    if any(color in combined_text for color in ['red', 'blue', 'green', 'yellow', 'purple', 'cyan', 'white']):
        if 'red' in combined_text:
            commands.append(control_rgb_color('red'))
        elif 'blue' in combined_text:
            commands.append(control_rgb_color('blue'))
        elif 'green' in combined_text:
            commands.append(control_rgb_color('green'))
        elif 'yellow' in combined_text:
            commands.append(control_rgb_color('yellow'))
        elif 'purple' in combined_text or 'violet' in combined_text:
            commands.append(control_rgb_color('purple'))
        elif 'cyan' in combined_text:
            commands.append(control_rgb_color('cyan'))
        elif 'white' in combined_text:
            commands.append(control_rgb_color('white'))
    
    # Parse buzzer commands
    if 'buzzer' in combined_text or 'beep' in combined_text:
        # Extract duration if mentioned
        duration_match = re.search(r'(\d+)\s*sec', combined_text)
        duration = int(duration_match.group(1)) if duration_match else 2
        commands.append(control_buzzer(duration=duration))
    
    # Parse alarm commands
    if 'alarm' in combined_text:
        duration_match = re.search(r'(\d+)\s*sec', combined_text)
        duration = int(duration_match.group(1)) if duration_match else 10
        
        alarm_type = 'standard'
        if 'urgent' in combined_text or 'emergency' in combined_text:
            alarm_type = 'urgent'
        elif 'reminder' in combined_text:
            alarm_type = 'reminder'
            
        commands.append(set_alarm(duration, alarm_type))
    
    # Parse OLED display commands
    if any(word in combined_text for word in ['display', 'show', 'oled', 'screen']):
        # Extract the activity context for display
        activity_context = extract_activity_context(user_message)
        display_text = f"Activity: {activity_context.upper()}"
        commands.append(set_oled_display(display_text))
    
    print(f"✅ PARSED {len(commands)} device commands")
    return commands

# Flask Routes
@app.route('/')
def chat_interface():
    """Serve the chat interface"""
    print("🌐 ROUTE: Serving chat interface")
    return render_template_string(CHAT_HTML)

@app.route('/favicon.ico')
def favicon():
    """Serve favicon to prevent 404 errors"""
    try:
        return send_from_directory(static_dir, 'favicon.ico')
    except:
        # Return empty 204 No Content if favicon doesn't exist
        return '', 204

@app.route('/.well-known/appspecific/com.chrome.devtools.json')
def chrome_devtools():
    """Handle Chrome DevTools request to prevent 404"""
    return jsonify({}), 200

@app.route('/debug')
def debug_page():
    """Debug page to test JavaScript"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Debug Test</title>
    </head>
    <body>
        <h1>Debug Test Page</h1>
        <input type="text" id="testInput" placeholder="Test message">
        <button onclick="testSend()">Test Send</button>
        <div id="result"></div>
        
        <script>
            async function testSend() {
                const message = document.getElementById('testInput').value;
                console.log('Sending:', message);
                
                try {
                    const response = await fetch('/chat', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({
                            user_activity: message,
                            session_id: 'debug_test'
                        })
                    });
                    const data = await response.json();
                    document.getElementById('result').innerHTML = 'Response: ' + JSON.stringify(data);
                    console.log('Success:', data);
                } catch (error) {
                    document.getElementById('result').innerHTML = 'Error: ' + error;
                    console.error('Error:', error);
                }
            }
        </script>
    </body>
    </html>
    """

@app.route('/chat', methods=['POST'])
def handle_chat():
    """Main chat endpoint with intelligent routing and smart scanning"""
    print("\n" + "="*50)
    print("🚀 CHAT ENDPOINT: New request received")
    print("="*50)
    
    try:
        data = request.get_json()
        user_message = data.get('user_activity', '').strip()
        session_id = data.get('session_id')
        
        if not user_message:
            print("❌ CHAT ERROR: Empty user message")
            return jsonify({"error": "Please provide a message", "status": "error"}), 400

        print(f"💬 USER MESSAGE: '{user_message}'")
        print(f"🆔 SESSION ID: {session_id}")
        
        # Step 1: Store user message
        print("💾 STEP 1: Storing user message in database")
        store_conversation(session_id, 'user', user_message)
        
        # Step 2: Classify and generate queries with Gemini (LLM2) - NO SQL GENERATION
        print("🎯 STEP 2: Classifying message with Gemini (STRICTLY NO SQL GENERATION)")
        try:
            classification = classify_and_generate_queries(user_message, session_id)
        except Exception as e:
            print(f"❌ CLASSIFICATION FAILED: {e}, using fallback")
            classification = {
                "needs_sensor_data": True,  # Safe fallback
                "message_type": "real_time_optimization",
                "sql_queries": [],
                "reasoning": "Fallback due to classification error"
            }
        
        if classification['needs_sensor_data']:
            # Action request - need sensor data
            print("🔧 ROUTING: Action request - needs sensor data")
            return handle_action_request(user_message, session_id, classification)
        else:
            # Information request - use historical data with PREDEFINED queries only
            print("🔧 ROUTING: Information request - using prehistoric data with PREDEFINED queries")
            return handle_info_request(user_message, session_id, classification)
            
    except Exception as e:
        print(f"❌ CHAT HANDLING ERROR: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500
    

def handle_action_request(user_message, session_id, classification):
    """Handle requests that need fresh sensor data"""
    print("🎯 ACTION REQUEST: Handling action request requiring sensor data")
    
    request_id = f"req_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hash(user_message) % 10000:04d}"
    
    # Store pending request
    with pending_lock:
        pending_requests[request_id] = {
            "user_message": user_message,
            "session_id": session_id,
            "classification": classification,
            "status": "waiting_for_sensors",
            "created_at": datetime.now().isoformat(),
            "sensor_data": None,
            "result": None
        }
    
    print(f"📝 ACTION REQUEST: Created pending request {request_id}")
    
    return jsonify({
        "status": "waiting_for_sensors",
        "request_id": request_id,
        "user_message": user_message,
        "message": "🔄 Scanning sensors for current environment data...",
        "needs_sensor_data": True
    })

def handle_info_request(user_message, session_id, classification):
    """Handle requests that can use historical data with PREDEFINED queries only"""
    print("📊 INFO REQUEST: Handling information request using prehistoric data")
    print("🛡  INFO REQUEST: Using ONLY predefined SQL queries - NO MODEL GENERATED SQL")
    
    try:
        # ALWAYS use predefined queries for prehistoric data - NO EXCEPTIONS
        sql_queries = get_predefined_queries(session_id)
        print(f"✅ INFO REQUEST: Using {len(sql_queries)} predefined queries for session {session_id}")

        # Step 1: Execute the predefined SQL queries (safe, validated)
        print("🛠  STEP 1: Executing predefined SQL queries")
        query_results = execute_sql_queries(sql_queries)

        # Log any database errors but proceed
        if isinstance(query_results, dict) and 'error' in query_results:
            print(f"❗ DATABASE ERROR: {query_results.get('error')}")
        else:
            print(f"✅ QUERY EXECUTION: Completed successfully")

        # Step 2: Build context from query results and classifier info
        print("📝 STEP 2: Building context for LLM1")
        context = build_context_from_query_results(user_message, query_results, classification)

        # Step 3: Call LLM1 (GPT via OpenRouter) to get the assistant response
        print("🧠 STEP 3: Calling LLM1 (GPT-4o mini) for response generation")
        try:
            llm_response = get_llm1_response(context)
        except Exception as e:
            print(f"❌ LLM1 ERROR: {e}")
            llm_response = (
                "Sorry — I'm having trouble generating a full answer right now. "
                "I can still try to help based on what I remember: "
                "please ask again or check the conversation history."
            )

        # Step 4: Parse and execute device commands
        print("🎮 STEP 4: Parsing device commands")
        device_commands = parse_device_commands(llm_response, user_message)
        
        # Combine LLM response with device commands
        final_response = llm_response
        if device_commands:
            final_response += "\n\n" + "🔧 Device Actions:\n" + "\n".join([f"• {cmd}" for cmd in device_commands])

        # Step 5: Store assistant response in DB
        print("💾 STEP 5: Storing assistant response in database")
        try:
            store_conversation(
                session_id,
                'assistant',
                final_response,
                metadata={"type": "info_response", "classification": classification, "device_commands": device_commands}
            )
        except Exception as e:
            print(f"❌ STORAGE ERROR: Failed to store assistant response: {e}")

        print(f"✅ INFO REQUEST: Completed successfully for session {session_id}")

        # Step 6: Return structured JSON for the HTTP response
        return jsonify({
            "status": "completed",
            "response": final_response,
            "needs_sensor_data": False,
            "message_type": classification.get('message_type', 'past_data_query'),
            "device_commands": device_commands
        })

    except Exception as e:
        print(f"❌ INFO REQUEST ERROR: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500

@app.route('/provide_sensor_data/<request_id>', methods=['POST'])
def provide_sensor_data(request_id):
    """Provide sensor data for pending action requests"""
    print(f"\n📊 SENSOR DATA ENDPOINT: Received sensor data for request {request_id}")
    
    try:
        data = request.get_json()
        sensor_data = data.get('sensor_data', {})
        
        print(f"📊 SENSOR DATA: {sensor_data}")
        
        with pending_lock:
            if request_id not in pending_requests:
                print(f"❌ SENSOR DATA ERROR: Invalid request ID {request_id}")
                return jsonify({"error": "Invalid request ID", "status": "error"}), 404
            
            request_info = pending_requests[request_id]
            
            # Update global sensor data
            global current_sensor_data
            current_sensor_data.update(sensor_data)
            
            # Store sensor data with the original user message
            print("💾 Storing sensor data with user message")
            store_conversation(
                request_info["session_id"], 
                'user', 
                request_info["user_message"],
                sensor_data=sensor_data,
                request_id=request_id
            )
            
            # Build optimization prompt
            activity_context = extract_activity_context(request_info["user_message"])
            optimization_prompt = f"""
You are a smart environment assistant that analyzes current room conditions and optimizes them for different activities.

USER ACTIVITY: {request_info["user_message"]}
ACTIVITY CONTEXT: {activity_context}

CURRENT SENSOR READINGS:
- Temperature: {sensor_data.get('temperature', 'N/A')}°C
- Humidity: {sensor_data.get('humidity', 'N/A')}%
- Light Level: {sensor_data.get('light', 'N/A')}

Please analyze the current conditions and provide optimization recommendations. Focus on:
1. What's problematic about current conditions for this activity
2. Specific adjustments needed for temperature, light, etc.
3. Clear reasoning for each change

Provide your response in a helpful, conversational tone with specific recommendations.
"""
            
            # Get optimization response
            print("🧠 Generating optimization response with LLM1")
            llm_response = get_llm1_response(optimization_prompt)
            
            # Parse and execute device commands
            print("🎮 Parsing device commands for action request")
            device_commands = parse_device_commands(llm_response, request_info["user_message"])
            
            # Combine LLM response with device commands
            final_response = llm_response
            if device_commands:
                final_response += "\n\n" + "🔧 Device Actions:\n" + "\n".join([f"• {cmd}" for cmd in device_commands])
            
            # Parse and store environment changes
            print("💾 Storing environment changes")
            environment_changes = parse_environment_changes(llm_response, sensor_data, activity_context)
            for change in environment_changes:
                store_environment_change(
                    request_info["session_id"],
                    request_id,
                    change['factor'],
                    change['previous_value'],
                    change['new_value'],
                    change['reasoning'],
                    activity_context
                )
            
            # Store assistant response
            store_conversation(
                request_info["session_id"],
                'assistant',
                final_response,
                metadata={"device_commands": device_commands},
                request_id=request_id
            )
            
            request_info["result"] = final_response
            request_info["status"] = "completed"
            request_info["completed_at"] = datetime.now().isoformat()
        
        # Update scan time to prevent bombardment
        update_scan_time()
        
        print(f"✅ SENSOR DATA PROCESSING: Completed for request {request_id}")
        
        return jsonify({
            "status": "completed",
            "request_id": request_id,
            "response": final_response,
            "device_commands": device_commands,
            "message": "Scan completed successfully"
        })
        
    except Exception as e:
        print(f"❌ SENSOR DATA PROCESSING ERROR: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500

def parse_environment_changes(llm_response, sensor_data, activity_context):
    """Parse environment changes from LLM response"""
    print(f"🔍 PARSING ENVIRONMENT CHANGES from LLM response")
    changes = []
    
    # Extract temperature changes
    if 'temperature' in llm_response.lower():
        changes.append({
            'factor': 'temperature',
            'previous_value': sensor_data.get('temperature', 'unknown'),
            'new_value': '22',  # Simplified - in real implementation, extract from response
            'reasoning': 'Optimal temperature for comfort and focus'
        })
    
    # Extract light changes
    if 'light' in llm_response.lower() or 'bright' in llm_response.lower():
        changes.append({
            'factor': 'light',
            'previous_value': sensor_data.get('light', 'unknown'),
            'new_value': '2500',
            'reasoning': 'Improved lighting for the activity'
        })
    
    # Extract fan speed changes
    if 'fan' in llm_response.lower() or 'airflow' in llm_response.lower():
        changes.append({
            'factor': 'fan_speed',
            'previous_value': 'off',
            'new_value': 'medium',
            'reasoning': 'Better air circulation'
        })
    
    print(f"✅ PARSED {len(changes)} environment changes")
    return changes

@app.route('/check_status/<request_id>', methods=['GET'])
def check_status(request_id):
    """Check status of pending requests"""
    print(f"🔍 CHECK STATUS: Checking status for request {request_id}")
    
    with pending_lock:
        if request_id not in pending_requests:
            print(f"❌ CHECK STATUS: Invalid request ID {request_id}")
            return jsonify({"error": "Invalid request ID", "status": "error"}), 404
        
        request_info = pending_requests[request_id]
        
        if request_info["status"] == "completed":
            print(f"✅ CHECK STATUS: Request {request_id} completed")
            return jsonify({
                "status": "completed",
                "response": request_info["result"],
                "user_message": request_info["user_message"]
            })
        else:
            print(f"⏳ CHECK STATUS: Request {request_id} still waiting for sensors")
            return jsonify({
                "status": "waiting_for_sensors",
                "message": "Waiting for sensor data...",
                "user_message": request_info["user_message"]
            })

@app.route('/conversation_history/<session_id>', methods=['GET'])
def get_conversation_history(session_id):
    """Get complete conversation history for a session"""
    print(f"📜 CONVERSATION HISTORY: Retrieving history for session {session_id}")
    
    try:
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor(dictionary=True)
            
            cursor.execute(
                """SELECT message_type, content, metadata, sensor_data, request_id, created_at 
                FROM conversations 
                WHERE session_id = %s 
                ORDER BY created_at ASC""",
                (session_id,)
            )
            
            conversations = cursor.fetchall()
            cursor.close()
            connection.close()
            
            print(f"✅ CONVERSATION HISTORY: Retrieved {len(conversations)} messages")
            return jsonify({
                "session_id": session_id,
                "conversations": conversations,
                "status": "success"
            })
        else:
            print("❌ CONVERSATION HISTORY: Database connection failed")
            return jsonify({"error": "Database connection failed", "status": "error"}), 500
            
    except Exception as e:
        print(f"❌ CONVERSATION HISTORY ERROR: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500

@app.route('/environment_history/<session_id>', methods=['GET'])
def get_environment_history_endpoint(session_id):
    """Get environment change history for a session"""
    print(f"🌡  ENVIRONMENT HISTORY: Retrieving changes for session {session_id}")
    
    try:
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor(dictionary=True)
            
            cursor.execute(
                """SELECT factor, previous_value, new_value, reasoning, activity_context, created_at 
                FROM environment_changes 
                WHERE session_id = %s 
                ORDER BY created_at DESC""",
                (session_id,)
            )
            
            changes = cursor.fetchall()
            cursor.close()
            connection.close()
            
            print(f"✅ ENVIRONMENT HISTORY: Retrieved {len(changes)} changes")
            return jsonify({
                "session_id": session_id,
                "environment_changes": changes,
                "status": "success"
            })
        else:
            print("❌ ENVIRONMENT HISTORY: Database connection failed")
            return jsonify({"error": "Database connection failed", "status": "error"}), 500
            
    except Exception as e:
        print(f"❌ ENVIRONMENT HISTORY ERROR: {e}")
        return jsonify({"error": str(e), "status": "error"}), 500

# Device Command Endpoints
@app.route('/get_commands/esp32')
def get_commands_esp32():
    """Get commands for ESP32 (OLED display)"""
    commands = {}
    if 'oled_text' in esp8266_commands:
        commands['oled_text'] = esp8266_commands['oled_text']
    print(f"📡 Sending commands to ESP32: {commands}")
    return jsonify(commands)

@app.route('/get_commands/esp8266')
def get_commands_esp8266():
    """Get commands for ESP8266 (RGB + Buzzer)"""
    commands = {}
    
    # RGB commands
    if 'rgb_color' in esp8266_commands:
        commands['rgb_color'] = esp8266_commands['rgb_color']
    
    # Buzzer commands
    if 'buzzer_action' in esp8266_commands:
        commands['buzzer_action'] = esp8266_commands['buzzer_action']
    
    if 'buzzer_duration' in esp8266_commands:
        commands['buzzer_duration'] = esp8266_commands['buzzer_duration']
    
    # Alarm commands
    if 'alarm' in esp8266_commands:
        commands['alarm'] = esp8266_commands['alarm']
        if 'alarm_duration' in esp8266_commands:
            commands['alarm_duration'] = esp8266_commands['alarm_duration']
        if 'alarm_type' in esp8266_commands:
            commands['alarm_type'] = esp8266_commands['alarm_type']
    
    print(f"📡 Sending commands to ESP8266: {commands}")
    return jsonify(commands)

@app.route('/clear_commands/esp8266', methods=['POST'])
def clear_esp8266_commands():
    """Clear ESP8266 commands after they've been read"""
    global esp8266_commands
    
    # Only clear buzzer and RGB commands, keep OLED for ESP32
    keys_to_clear = ['rgb_color', 'buzzer_action', 'buzzer_duration', 'alarm', 'alarm_duration', 'alarm_type']
    for key in keys_to_clear:
        if key in esp8266_commands:
            del esp8266_commands[key]
    
    print("✅ ESP8266 commands cleared")
    return jsonify({"status": "success", "message": "ESP8266 commands cleared"})

@app.route('/clear_commands/esp32', methods=['POST'])
def clear_esp32_commands():
    """Clear ESP32 commands after they've been read"""
    global esp8266_commands
    
    if 'oled_text' in esp8266_commands:
        del esp8266_commands['oled_text']
    
    print("✅ ESP32 commands cleared")
    return jsonify({"status": "success", "message": "ESP32 commands cleared"})

# Smart Scanning Endpoints
@app.route('/sensor_data', methods=['POST'])
def receive_sensor_data():
    """Receive sensor data from ESP32"""
    global current_sensor_data
    data = request.get_json()
    
    if data:
        current_sensor_data.update(data)
        print(f"📊 SENSOR DATA RECEIVED: Temp={data.get('temperature')}°C, Light={data.get('light')}")
    
    return jsonify({"status": "success"})

@app.route('/get_pending_request')
def get_pending_request():
    """Get pending scan requests for ESP32"""
    with pending_lock:
        for req_id, req_info in pending_requests.items():
            if req_info["status"] == "waiting_for_sensors":
                return jsonify({
                    "request_id": req_id,
                    "user_message": req_info["user_message"]
                })
    
    return jsonify({"request_id": ""})

@app.route('/control_rgb', methods=['POST'])
def control_rgb():
    """Control RGB from web interface"""
    color = request.form.get('color')
    result = control_rgb_color(color)
    return jsonify({"status": "success", "message": result})

@app.route('/control_buzzer', methods=['POST'])
def control_buzzer_web():
    """Control buzzer from web interface"""
    action = request.form.get('action')
    duration = request.form.get('duration')
    
    if duration:
        result = control_buzzer(duration=int(duration))
    else:
        result = control_buzzer(action=action)
    
    return jsonify({"status": "success", "message": result})

@app.route('/set_alarm', methods=['POST'])
def set_alarm_endpoint():
    """Set alarm with duration and type"""
    data = request.get_json()
    duration = data.get('duration', 10)  # seconds
    alarm_type = data.get('type', 'standard')  # standard, urgent, reminder
    
    result = set_alarm(duration, alarm_type)
    
    return jsonify({
        "status": "success", 
        "message": result
    })

@app.route('/stop_alarm', methods=['POST'])
def stop_alarm():
    """Stop the alarm"""
    esp8266_commands['alarm'] = False
    esp8266_commands['buzzer_action'] = 'stop'
    
    return jsonify({"status": "success", "message": "Alarm stopped"})

@app.route('/set_oled', methods=['POST'])
def set_oled():
    """Set OLED display text"""
    data = request.get_json()
    text = data.get('text', 'Hello World')
    
    result = set_oled_display(text)
    return jsonify({"status": "success", "message": result})

@app.route('/current_sensor_data', methods=['GET'])
def get_current_sensor_data():
    """Get current sensor data for dashboard"""
    return jsonify({
        "status": "success",
        "sensor_data": current_sensor_data,
        "timestamp": datetime.now().isoformat()
    })

@app.route('/force_scan', methods=['POST'])
def force_scan():
    """Force a sensor scan (override cooldown)"""
    global last_sensor_scan
    last_sensor_scan = 0  # Reset cooldown
    print("🔄 Force scan initiated")
    return jsonify({"status": "success", "message": "Scan cooldown reset"})

@app.route('/scan_status', methods=['GET'])
def scan_status():
    """Get current scan status"""
    current_time = time.time()
    scan_ready = can_scan_now()
    time_since_last_scan = current_time - last_sensor_scan if last_sensor_scan > 0 else SCAN_COOLDOWN
    
    return jsonify({
        "scan_ready": scan_ready,
        "cooldown_seconds": SCAN_COOLDOWN,
        "seconds_since_last_scan": int(time_since_last_scan),
        "seconds_until_next_scan": max(0, int(SCAN_COOLDOWN - time_since_last_scan)),
        "last_scan_time": last_sensor_scan
    })

# Health check
@app.route('/health', methods=['GET'])
def health_check():
    print("🔍 HEALTH CHECK: Checking system health")
    db_status = "connected" if get_db_connection() else "disconnected"
    return jsonify({
        "status": "healthy", 
        "database": db_status,
        "llm1_configured": bool(OPENROUTER_API_KEY),
        "llm2_configured": bool(GEMINI_API_KEY),
        "scan_cooldown": SCAN_COOLDOWN,
        "can_scan_now": can_scan_now(),
        "timestamp": datetime.now().isoformat()
    })

# Error handlers
@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors gracefully"""
    return jsonify({"error": "Endpoint not found", "status": "error"}), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors gracefully"""
    return jsonify({"error": "Internal server error", "status": "error"}), 500

# Your exact frontend HTML code with fixed image URLs
CHAT_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Super Smart Bros</title>
    <link href="https://fonts.googleapis.com/css2?family=Press+Start+2P&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        
        /* RETRO SCROLLBAR */
        ::-webkit-scrollbar { width: 12px; background: #000; }
        ::-webkit-scrollbar-thumb { background: #e70012; border: 2px solid #000; }
        
        body { 
            font-family: 'Press Start 2P', cursive; 
            background-color: #202028;
            background-image: 
                linear-gradient(#111 50%, transparent 50%),
                linear-gradient(90deg, rgba(255,0,0,.06), rgba(0,255,0,.02), rgba(0,0,255,.06));
            background-size: 100% 4px, 6px 100%;
            min-height: 100vh; 
            padding: 20px;
            color: #fff;
            line-height: 1.6;
            font-size: 12px;
        }
        
        .chat-container { 
            max-width: 900px; 
            margin: 0 auto; 
            background: #000000; 
            border: 4px solid #000000;
            box-shadow: 10px 10px 0px rgba(0,0,0,0);
            position: relative;
            overflow: hidden; 
        }

        /* --- HEADER IS NOW THE GAME BACKGROUND --- */
        .chat-header { 
            background: #e70012; /* Mario Red */
            color: rgba(5, 5, 5, 0.973); 
            padding: 25px; 
            text-align: center; 
            border-bottom: 4px solid #fff;
            position: relative;
            height: 150px; 
            overflow: hidden; 
            cursor: pointer; 
            user-select: none;
        }
        
        .header-content {
            position: relative;
            z-index: 10; 
            pointer-events: none; 
            /* Added padding to push text up slightly so it doesn't overlap game area too much */
            padding-bottom: 10px;
        }

        .chat-header h1 { 
            font-size: 16px; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 1px;
            text-shadow: 3px 3px 0 #000; 
            color: #fff;
        }
        
        /* --- STATIC TEXT --- */
        .chat-header p { 
            font-size: 12px; 
            color: #ffff00; 
            text-shadow: 4px 4px 0 #000000; 
            letter-spacing: 1px; 
            margin-top: 5px; 
            line-height: 1.5;
            display: block; 
        }

        /* --- SCORE BOARD --- */
        #score-board {
            position: absolute;
            top: 35px;
            right: 60px; 
            font-size: 12px;
            color: #fff;
            text-shadow: 2px 2px 0 #000;
            z-index: 20;
            letter-spacing: 1px;
        }

        /* --- GAME OVER MESSAGE --- */
        #game-over-msg {
            display: none; 
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: rgba(0,0,0,0.85);
            color: #fff;
            padding: 20px;
            border: 4px solid #fff;
            z-index: 50;
            text-align: center;
        }

        /* --- FULL WIDTH GAME ELEMENTS --- */
        .game-floor {
            position: absolute;
            bottom: 10px;
            left: 0;
            width: 100%;
            height: 4px;
            background: #fff;
            z-index: 1;
        }

        /* --- CSS CODED DINOSAUR --- */
        #dino {
            width: 44px;
            height: 47px;
            background-color: #fff;
            position: absolute;
            bottom: 14px; 
            left: 50px;   
            z-index: 2;
            clip-path: polygon(
                40% 0%, 60% 0%, 60% 10%, 100% 10%, 100% 40%, 60% 40%, 60% 50%, 
                80% 50%, 80% 60%, 20% 60%, 20% 30%, 0% 30%, 0% 50%, 20% 50%, 
                20% 80%, 0% 80%, 0% 100%, 25% 100%, 25% 70%, 50% 70%, 50% 100%, 
                75% 100%, 75% 80%, 40% 80%
            );
            transition: height 0.1s;
        }

        #dino.ducking {
            height: 30px; 
            width: 55px;  
            clip-path: polygon(
                50% 20%, 100% 20%, 100% 50%, 60% 50%, 60% 60%, 
                80% 60%, 80% 70%, 20% 70%, 20% 40%, 0% 40%, 0% 60%, 20% 60%, 
                20% 80%, 0% 80%, 0% 100%, 30% 100%, 30% 80%, 50% 80%, 50% 100%, 
                80% 100%, 80% 80%, 50% 80%
            );
        }

        .animate-jump {
            animation: jump 0.6s ease-out;
        }

        @keyframes jump {
            0% { bottom: 14px; }
            40% { bottom: 90px; } 
            60% { bottom: 90px; }
            100% { bottom: 14px; }
        }

        #cactus {
            width: 25px;
            height: 35px;
            background-color: #000; 
            position: absolute;
            bottom: 14px;
            right: -30px; 
            z-index: 2;
            animation: cactusMove 2s infinite linear;
            clip-path: polygon(20% 0%, 80% 0%, 80% 100%, 20% 100%, 20% 60%, 0% 60%, 0% 30%, 20% 30%, 80% 30%, 100% 30%, 100% 60%, 80% 60%);
        }

        @keyframes cactusMove {
            0% { right: -30px; }
            100% { right: 100%; } 
        }

        .coin {
            width: 24px; height: 32px;
            background-image: url('https://raw.githubusercontent.com/jmflhs/Mario-Bros-Assets/master/assets/coin_1.png');
            background-size: contain; background-repeat: no-repeat;
            position: absolute; top: 25px; right: 25px;
            image-rendering: pixelated;
            z-index: 15; 
        }

        .chat-messages { 
            height: 500px; 
            overflow-y: auto; 
            padding: 25px; 
            background: #101317; 
            position: relative;
            z-index: 5; 
        }
        
        .message { margin-bottom: 30px; display: flex; align-items: flex-end; position: relative; z-index: 7; }
        .user-message { flex-direction: row-reverse; }
        .bot-message { flex-direction: row; }

        .character-sprite {
            width: 48px; height: 48px; margin: 0 12px; image-rendering: pixelated; 
            filter: drop-shadow(4px 4px 0px rgba(0,0,0,0.5));
        }
        .user-message .character-sprite { animation: runInRight 0.5s ease-out, bounce 2s infinite ease-in-out 0.5s; }
        .bot-message .character-sprite { animation: runInLeft 0.5s ease-out, bounce 2s infinite ease-in-out 0.5s; }
        @keyframes runInRight { 0% { transform: translateX(100px); opacity: 0; } 100% { transform: translateX(0); opacity: 1; } }
        @keyframes runInLeft { 0% { transform: translateX(-100px); opacity: 0; } 100% { transform: translateX(0); opacity: 1; } }
        @keyframes bounce { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
        
        .message-bubble { 
            max-width: 70%; padding: 15px; position: relative; font-size: 10px;
            box-shadow: -4px 0 0 0 black, 4px 0 0 0 black, 0 -4px 0 0 black, 0 4px 0 0 black;
            margin-bottom: 5px;
        }
        .user-bubble { background: #e70012; color: #fff; border: 4px solid #ffcccc; margin-right: 5px; }
        .bot-bubble { background: #000; color: #fff; border: 4px solid #00aa00; margin-left: 5px; }

        .system-header { color: #00ff00; text-align: center; margin-bottom: 10px; }
        .mission-list { color: #ffffff; line-height: 1.8; }
        .sensor-info { background: #000; border: 2px solid #ffff00; padding: 10px; margin: 10px 0; color: #ffff00; }
        
        .chat-input { padding: 20px; background: #222; border-top: 4px solid #fff; position: relative; z-index: 20; }
        .input-group { display: flex; gap: 15px; }
        
        #userInput { flex: 1; padding: 15px; background: #000; color: #fff; border: 4px solid #fff; outline: none; font-family: 'Press Start 2P', cursive; font-size: 10px; text-transform: uppercase; }
        #userInput:focus { border-color: #00aa00; }
        
        /* SHARED BUTTON STYLES */
        button {
            padding: 15px 25px; 
            color: white; 
            border: 4px solid #fff; 
            cursor: pointer; 
            font-family: 'Press Start 2P', cursive; 
            font-size: 10px; 
            text-transform: uppercase; 
            box-shadow: 4px 4px 0px #000;
        }
        button:active { transform: translate(2px, 2px); box-shadow: 2px 2px 0px #000; }

        #sendButton { background: #00aa00; }
        
        /* --- NEW VOICE BUTTON STYLES --- */
        #voiceButton { 
            background: #0088aa; /* Blue for Ice/Luigi */
            font-size: 16px; /* Larger icon */
            padding: 15px; /* Square shape */
            min-width: 60px;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        /* Pulse animation when listening */
        #voiceButton.listening {
            background: #e70012; /* Turns red when recording */
            animation: pulse-red 1s infinite;
            border-color: #ffff00;
        }

        @keyframes pulse-red {
            0% { box-shadow: 0 0 0 0 rgba(231, 0, 18, 0.7), 4px 4px 0px #000; }
            70% { box-shadow: 0 0 0 10px rgba(231, 0, 18, 0), 4px 4px 0px #000; }
            100% { box-shadow: 0 0 0 0 rgba(231, 0, 18, 0), 4px 4px 0px #000; }
        }

        strong { color: #fff; text-shadow: 2px 2px 0 #000; }

        /* --- ALARM CONTROLS --- */
        .alarm-controls {
            background: #000;
            border: 4px solid #ffff00;
            padding: 15px;
            margin: 15px 0;
            text-align: center;
        }

        .alarm-controls h3 {
            color: #ffff00;
            margin-bottom: 10px;
            font-size: 12px;
        }

        .alarm-buttons {
            display: flex;
            gap: 10px;
            justify-content: center;
            flex-wrap: wrap;
        }

        .alarm-btn {
            background: #e70012;
            padding: 10px 15px;
            font-size: 9px;
        }

        .alarm-btn.stop {
            background: #00aa00;
        }

        /* --- SENSOR DASHBOARD --- */
        .sensor-dashboard {
            background: #000;
            border: 4px solid #00aa00;
            padding: 15px;
            margin: 15px 0;
        }

        .sensor-dashboard h3 {
            color: #00ff00;
            margin-bottom: 10px;
            font-size: 12px;
            text-align: center;
        }

        .sensor-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 10px;
        }

        .sensor-item {
            background: #111;
            border: 2px solid #333;
            padding: 10px;
            text-align: center;
        }

        .sensor-value {
            font-size: 14px;
            color: #ffff00;
            margin: 5px 0;
        }

        .sensor-label {
            font-size: 9px;
            color: #ccc;
        }

        /* --- RGB COLOR CONTROLS --- */
        .color-controls {
            background: #000;
            border: 4px solid #ff00ff;
            padding: 15px;
            margin: 15px 0;
            text-align: center;
        }

        .color-controls h3 {
            color: #ff00ff;
            margin-bottom: 10px;
            font-size: 12px;
        }

        .color-buttons {
            display: flex;
            gap: 10px;
            justify-content: center;
            flex-wrap: wrap;
        }

        .color-btn {
            padding: 10px 15px;
            font-size: 9px;
            min-width: 80px;
        }

        .color-red { background: #e70012; }
        .color-blue { background: #0088aa; }
        .color-green { background: #00aa00; }
        .color-yellow { background: #ffff00; color: #000; }
        .color-purple { background: #aa00aa; }
        .color-cyan { background: #00aaaa; }
        .color-white { background: #ffffff; color: #000; }
    </style>
</head>
<body>
    <div class="chat-container">
        
        <!-- HEADER IS GAME AREA -->
        <div class="chat-header" onclick="handleHeaderClick()">
            <!-- SCORE BOARD -->
            <div id="score-board">SCORE: 00000</div>
            <div class="coin"></div>
            
            <div class="header-content">
                <h1>SUPER SMART BROS.</h1>
                <p>INSERT COIN (Just kidding, type below)</p>
            </div>

            <!-- GAME ELEMENTS -->
            <div id="dino"></div>
            <div id="cactus"></div>
            <div class="game-floor"></div>
            
            <!-- GAME OVER MESSAGE -->
            <div id="game-over-msg">
                GAME OVER<br><br>
                <span style="font-size:10px; color:#ffff00; animation: blink 1s infinite;">CLICK TO RESTART</span>
            </div>
        </div>
        
        <div class="chat-messages" id="chatMessages">
            <!-- SENSOR DASHBOARD -->
            <div class="sensor-dashboard">
                <h3>🌡️ LIVE SENSOR DATA</h3>
                <div class="sensor-grid">
                    <div class="sensor-item">
                        <div class="sensor-label">TEMPERATURE</div>
                        <div class="sensor-value" id="tempValue">-- °C</div>
                    </div>
                    <div class="sensor-item">
                        <div class="sensor-label">HUMIDITY</div>
                        <div class="sensor-value" id="humidityValue">-- %</div>
                    </div>
                    <div class="sensor-item">
                        <div class="sensor-label">LIGHT LEVEL</div>
                        <div class="sensor-value" id="lightValue">--</div>
                    </div>
                </div>
            </div>

            <!-- RGB COLOR CONTROLS -->
            <div class="color-controls">
                <h3>🎨 RGB COLOR CONTROLS</h3>
                <div class="color-buttons">
                    <button class="color-btn color-red" onclick="setRGBColor('red')">RED</button>
                    <button class="color-btn color-blue" onclick="setRGBColor('blue')">BLUE</button>
                    <button class="color-btn color-green" onclick="setRGBColor('green')">GREEN</button>
                    <button class="color-btn color-yellow" onclick="setRGBColor('yellow')">YELLOW</button>
                    <button class="color-btn color-purple" onclick="setRGBColor('purple')">PURPLE</button>
                    <button class="color-btn color-cyan" onclick="setRGBColor('cyan')">CYAN</button>
                    <button class="color-btn color-white" onclick="setRGBColor('white')">WHITE</button>
                </div>
            </div>

            <!-- ALARM CONTROLS -->
            <div class="alarm-controls">
                <h3>🚨 ALARM CONTROLS</h3>
                <div class="alarm-buttons">
                    <button class="alarm-btn" onclick="setAlarm(10, 'standard')">SET ALARM (10s)</button>
                    <button class="alarm-btn" onclick="setAlarm(30, 'urgent')">URGENT ALARM (30s)</button>
                    <button class="alarm-btn stop" onclick="stopAlarm()">STOP ALARM</button>
                    <button class="alarm-btn" onclick="testBuzzer('beep')">TEST BEEP</button>
                </div>
            </div>

            <div class="message bot-message">
                <img src="https://i.ibb.co/Sw5Z1cRf/bot.jpg" class="character-sprite" alt="Bot">
                <div class="message-bubble bot-bubble">
                    <div class="system-header">★ SYSTEM READY ★</div>
                    <strong>MISSION:</strong>
                    <br><br>
                    <div class="mission-list">
                        1. INPUT ACTIVITY<br>
                        2. SCAN SENSORS<br>
                        3. OPTIMIZE ROOM
                    </div>
                    <br>
                    <div class="sensor-info">SENSORS: ONLINE [OK]</div>
                </div>
            </div>

            <div class="message user-message">
                <img src="https://i.ibb.co/9jSstgJ/mari.jpg" class="character-sprite" alt="Mario">
                <div class="message-bubble user-bubble">
                    I need to focus on coding late at night!
                </div>
            </div>
        </div>
        
        <!-- INPUT AREA WITH NEW BUTTON -->
        <div class="chat-input">
            <div class="input-group">
                <input type="text" id="userInput" placeholder="WHAT ARE WE DOING?" autocomplete="off">
                
                <!-- NEW VOICE BUTTON -->
                <button id="voiceButton" title="Voice Input">🎤</button>
                
                <button id="sendButton">START</button>
            </div>
        </div>
    </div>

    <script>
        // --- GAME LOGIC ---
        const dino = document.getElementById("dino");
        const cactus = document.getElementById("cactus");
        const gameOverMsg = document.getElementById("game-over-msg");
        const scoreBoard = document.getElementById("score-board");
        let isGameOver = false;
        
        // --- SCORE LOGIC ---
        let score = 0;
        let scoreInterval;

        function startScore() {
            clearInterval(scoreInterval); 
            scoreInterval = setInterval(() => {
                if (!isGameOver) {
                    score++;
                    scoreBoard.innerText = "SCORE: " + score.toString().padStart(5, '0');
                }
            }, 100);
        }

        function jump() {
            if (isGameOver) return;
            if (dino.classList.contains("ducking")) return;

            if (dino.classList != "animate-jump") {
                dino.classList.add("animate-jump");
                setTimeout(function() {
                    dino.classList.remove("animate-jump");
                }, 600);
            }
        }

        function duck(isDucking) {
            if (isGameOver) return;
            if (dino.classList.contains("animate-jump")) return;

            if (isDucking) {
                dino.classList.add("ducking");
            } else {
                dino.classList.remove("ducking");
            }
        }
        
        // COLLISION DETECTION LOOP
        let checkDeadInterval = setInterval(function() {
            if (isGameOver) return;
            let dinoRect = dino.getBoundingClientRect();
            let cactusRect = cactus.getBoundingClientRect();

            if (
                dinoRect.right > cactusRect.left + 15 && 
                dinoRect.left < cactusRect.right - 15 && 
                dinoRect.bottom > cactusRect.top + 10
            ) {
                endGame();
            }
        }, 10);

        function endGame() {
            isGameOver = true;
            clearInterval(scoreInterval);
            cactus.style.animationPlayState = "paused";
            dino.style.animationPlayState = "paused";
            gameOverMsg.style.display = "block";
        }

        function resetGame() {
            isGameOver = false;
            score = 0;
            scoreBoard.innerText = "SCORE: 00000";
            startScore();
            gameOverMsg.style.display = "none";
            cactus.style.animation = 'none';
            cactus.offsetHeight; 
            cactus.style.animation = 'cactusMove 2s infinite linear';
            dino.style.animationPlayState = "running";
            dino.classList.remove("animate-jump");
            dino.classList.remove("ducking");
        }

        function handleHeaderClick() {
            if (isGameOver) {
                resetGame();
            } else {
                jump();
            }
        }

        document.addEventListener('keydown', function(e) {
            if(["Space", "ArrowUp", "ArrowDown"].indexOf(e.code) > -1) {
                e.preventDefault();
            }
            if ((e.key === " " || e.code === "Space" || e.key === "ArrowUp")) {
                if (isGameOver) resetGame();
                else jump();
            }
            if (e.key === "ArrowDown") {
                duck(true);
            }
        });

        document.addEventListener('keyup', function(e) {
            if (e.key === "ArrowDown") {
                duck(false);
            }
        });
        
        startScore();

        // --- CHAT LOGIC ---
        // FIXED SPRITES (No randomization)
        const HERO_SPRITE = "https://i.ibb.co/9jSstgJ/mari.jpg";
        const BOT_SPRITE = "https://i.ibb.co/Sw5Z1cRf/bot.jpg";

        function addMessage(text, isUser) {
            const container = document.getElementById('chatMessages');
            const msgDiv = document.createElement('div');
            msgDiv.className = isUser ? 'message user-message' : 'message bot-message';
            
            // USE FIXED SPRITE FOR USER AND BOT
            const img = document.createElement('img');
            img.src = isUser ? HERO_SPRITE : BOT_SPRITE;
            img.className = 'character-sprite';
            
            const bubble = document.createElement('div');
            bubble.className = isUser ? 'message-bubble user-bubble' : 'message-bubble bot-bubble';
            bubble.innerHTML = text;
            
            msgDiv.appendChild(img);
            msgDiv.appendChild(bubble);
            
            container.appendChild(msgDiv);
            container.scrollTop = container.scrollHeight;
        }

        function sendMessage() {
            const input = document.getElementById('userInput');
            const val = input.value.trim();
            if(val) {
                addMessage(val, true);
                input.value = '';
                
                // Send to backend
                fetch('/chat', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        user_activity: val,
                        session_id: 'super_smart_bros'
                    })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'completed') {
                        addMessage(data.response, false);
                    } else if (data.status === 'waiting_for_sensors') {
                        addMessage("🔄 SCANNING SENSORS...", false);
                        // Poll for completion
                        checkRequestStatus(data.request_id);
                    }
                })
                .catch(error => {
                    console.error('Error:', error);
                    addMessage("❌ ERROR: Could not connect to server", false);
                });
            }
        }

        function checkRequestStatus(requestId) {
            const checkInterval = setInterval(() => {
                fetch(`/check_status/${requestId}`)
                    .then(response => response.json())
                    .then(data => {
                        if (data.status === 'completed') {
                            clearInterval(checkInterval);
                            // Remove waiting message and add actual response
                            const messages = document.getElementById('chatMessages');
                            if (messages.lastChild) {
                                messages.removeChild(messages.lastChild);
                            }
                            addMessage(data.response, false);
                        }
                    })
                    .catch(error => {
                        console.error('Error checking status:', error);
                        clearInterval(checkInterval);
                    });
            }, 2000);
        }

        // Event listeners for send button and enter key
        document.getElementById('sendButton').addEventListener('click', sendMessage);
        
        document.getElementById('userInput').addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
            }
            // Shift+Enter will create a new line
        });

        // --- VOICE INPUT LOGIC (NEW) ---
        const voiceButton = document.getElementById('voiceButton');
        const userInput = document.getElementById('userInput');
        
        // Check for browser support
        if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            const recognition = new SpeechRecognition();
            
            recognition.continuous = false;
            recognition.lang = 'en-US';
            recognition.interimResults = false;

            voiceButton.addEventListener('click', () => {
                try {
                    recognition.start();
                } catch(e) {
                    // Usually means it's already started, so we stop it
                    recognition.stop();
                }
            });

            // Start Listening UI
            recognition.onstart = () => {
                voiceButton.classList.add('listening');
                userInput.placeholder = "LISTENING...";
            };

            // Stop Listening UI
            recognition.onend = () => {
                voiceButton.classList.remove('listening');
                userInput.placeholder = "WHAT ARE WE DOING?";
            };

            // Handle Result
            recognition.onresult = (event) => {
                const transcript = event.results[0][0].transcript;
                userInput.value = transcript.toUpperCase(); // Retro vibe
                userInput.focus();
            };

            recognition.onerror = (event) => {
                console.error("Speech recognition error", event.error);
                voiceButton.classList.remove('listening');
                userInput.placeholder = "ERROR. TRY TYPING.";
            };

        } else {
            // Fallback for unsupported browsers
            voiceButton.style.display = 'none';
            console.log("Web Speech API not supported in this browser.");
        }

        // --- SENSOR DASHBOARD UPDATE ---
        function updateSensorDashboard() {
            fetch('/current_sensor_data')
                .then(response => response.json())
                .then(data => {
                    if (data.status === 'success') {
                        const sensorData = data.sensor_data;
                        document.getElementById('tempValue').textContent = `${sensorData.temperature || '--'} °C`;
                        document.getElementById('humidityValue').textContent = `${sensorData.humidity || '--'} %`;
                        document.getElementById('lightValue').textContent = sensorData.light || '--';
                    }
                })
                .catch(error => {
                    console.error('Error updating sensor dashboard:', error);
                });
        }

        // Update sensor data every 3 seconds
        setInterval(updateSensorDashboard, 3000);
        updateSensorDashboard(); // Initial update

        // --- RGB COLOR CONTROL ---
        function setRGBColor(color) {
            fetch('/control_rgb', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                body: `color=${color}`
            })
            .then(response => response.json())
            .then(data => {
                addMessage(`🎨 ${data.message}`, false);
            })
            .catch(error => {
                console.error('Error setting RGB color:', error);
                addMessage("❌ ERROR: Could not set RGB color", false);
            });
        }

        // --- ALARM FUNCTIONS ---
        function setAlarm(duration, type) {
            fetch('/set_alarm', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    duration: duration,
                    type: type
                })
            })
            .then(response => response.json())
            .then(data => {
                addMessage(`🚨 ${data.message}`, false);
            })
            .catch(error => {
                console.error('Error setting alarm:', error);
                addMessage("❌ ERROR: Could not set alarm", false);
            });
        }

        function stopAlarm() {
            fetch('/stop_alarm', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            })
            .then(response => response.json())
            .then(data => {
                addMessage("✅ ALARM STOPPED", false);
            })
            .catch(error => {
                console.error('Error stopping alarm:', error);
                addMessage("❌ ERROR: Could not stop alarm", false);
            });
        }

        function testBuzzer(action) {
            fetch('/control_buzzer', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded',
                },
                body: `action=${action}`
            })
            .then(response => response.json())
            .then(data => {
                addMessage(`🔊 BUZZER: ${action.toUpperCase()}`, false);
            })
            .catch(error => {
                console.error('Error controlling buzzer:', error);
                addMessage("❌ ERROR: Could not control buzzer", false);
            });
        }

        // --- OLED DISPLAY CONTROL ---
        function setOLEDDisplay(text) {
            fetch('/set_oled', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    text: text
                })
            })
            .then(response => response.json())
            .then(data => {
                addMessage(`📟 ${data.message}`, false);
            })
            .catch(error => {
                console.error('Error setting OLED display:', error);
                addMessage("❌ ERROR: Could not set OLED display", false);
            });
        }
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    # Initialize database
    init_database()
    
    print("🚀 Starting Super Smart Bros Assistant...")
    print(f"🔑 LLM1 (GPT-4o) configured: {'Yes' if OPENROUTER_API_KEY else 'No'}")
    print(f"🔑 LLM2 (Gemini Flash) configured: {'Yes' if GEMINI_API_KEY else 'No'}")
    print(f"🗄  Database connected: {'Yes' if get_db_connection() else 'No'}")
    print(f"📊 Smart Scanning: Enabled with {SCAN_COOLDOWN}-second cooldown")
    print("\n🌐 Available endpoints:")
    print("  GET  http://localhost:5003/ - Super Smart Bros Interface")
    print("  POST http://localhost:5003/chat - Main chat endpoint")
    print("  POST http://localhost:5003/provide_sensor_data/<request_id> - Provide sensor data")
    print("  GET  http://localhost:5003/current_sensor_data - Get current sensor data for dashboard")
    print("  POST http://localhost:5003/set_alarm - Set alarm on ESP8266")
    print("  POST http://localhost:5003/stop_alarm - Stop alarm")
    print("  POST http://localhost:5003/control_rgb - Control RGB LED color")
    print("  POST http://localhost:5003/set_oled - Set OLED display text")
    print("\n🎮 Features:")
    print("  • Retro game interface with Dino game")
    print("  • Live sensor dashboard (Temp, Humidity, Light)")
    print("  • RGB color controls (Red, Blue, Green, Yellow, Purple, Cyan, White)")
    print("  • Alarm controls for ESP8266 buzzer")
    print("  • OLED display control")
    print("  • Voice input support")
    print("  • Smart scanning with cooldown")
    
    app.run(host='0.0.0.0', port=5003, debug=True)