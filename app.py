from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file
from apify_client import ApifyClient
from openai import OpenAI
import os
from dotenv import load_dotenv
from twitter.twitter_client import TwitterClient
import sqlite3
from sqlite3 import Error
from datetime import datetime, timedelta
from io import StringIO, BytesIO
import csv
import json
import random
import logging
from traceback import format_exc
import feedparser
import tweepy
from apscheduler.schedulers.background import BackgroundScheduler
from rss_worker import worker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('app.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'AAAAB3NzaC1yc2EAAAADAQABAAAAgQCWSLIWtUFn2')

# Add Jinja2 filter for JSON parsing
@app.template_filter('fromjson')
def fromjson(value):
    return json.loads(value) if value else {}

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.start()

def get_comments_count(account_id=None):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        today = datetime.now().strftime('%Y-%m-%d')
        
        if account_id:
            # Get today's count
            c.execute('''
                SELECT COUNT(*) FROM posted_comments 
                WHERE account_id = ? AND date(posted_at) = ?
            ''', (account_id, today))
            today_count = c.fetchone()[0]
            
            # Get total count
            c.execute('SELECT COUNT(*) FROM posted_comments WHERE account_id = ?', (account_id,))
            total_count = c.fetchone()[0]
        else:
            # Get today's count for all accounts
            c.execute('''
                SELECT COUNT(*) FROM posted_comments 
                WHERE date(posted_at) = ?
            ''', (today,))
            today_count = c.fetchone()[0]
            
            # Get total count for all accounts
            c.execute('SELECT COUNT(*) FROM posted_comments')
            total_count = c.fetchone()[0]
        
        return {
            'today': today_count,
            'total': total_count
        }
    except Error as e:
        print(f"Database error: {e}")
        return {'today': 0, 'total': 0}
    finally:
        if conn:
            conn.close()

# Make get_comments_count available to templates
app.jinja_env.globals['get_comments_count'] = get_comments_count

# Initialize clients
apify_client = ApifyClient(os.getenv('APIFY_API_TOKEN'))
client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))

# Initialize Twitter V2 client with OAuth 1.0a
twitter_client = TwitterClient(
    consumer_key=os.getenv('TWITTER_CONSUMER_KEY'),
    consumer_secret=os.getenv('TWITTER_CONSUMER_SECRET'),
    access_token=os.getenv('TWITTER_ACCESS_TOKEN'),
    access_token_secret=os.getenv('TWITTER_ACCESS_TOKEN_SECRET')
)

# Load GPT persona from config
def load_persona():
    try:
        with open('config/persona.txt', 'r') as f:
            return f.read().strip()
    except:
        return "You are a helpful assistant who writes engaging Twitter replies."

def migrate_db():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Check if we need to add the persona_id column
        c.execute("PRAGMA table_info(twitter_accounts)")
        columns = [column[1] for column in c.fetchall()]
        
        if 'persona_id' not in columns:
            print("Adding persona_id column...")
            c.execute('ALTER TABLE twitter_accounts ADD COLUMN persona_id INTEGER REFERENCES personas(id)')
            
        if 'automation_persona_id' not in columns:
            print("Adding automation_persona_id column...")
            c.execute('ALTER TABLE twitter_accounts ADD COLUMN automation_persona_id INTEGER REFERENCES personas(id)')
            
        if 'mention_user' not in columns:
            print("Adding mention_user column...")
            c.execute('ALTER TABLE twitter_accounts ADD COLUMN mention_user BOOLEAN DEFAULT 1')
            
        # Check personas table columns
        c.execute("PRAGMA table_info(personas)")
        persona_columns = [column[1] for column in c.fetchall()]
        
        if 'mention_user' not in persona_columns:
            print("Adding mention_user column to personas table...")
            c.execute('ALTER TABLE personas ADD COLUMN mention_user BOOLEAN DEFAULT 1')
            
        # Rename old columns to custom_ prefix if they exist
        old_columns = ['style', 'length', 'speech_settings', 'use_emoji', 'use_hashtags']
        for col in old_columns:
            if col in columns and f'custom_{col}' not in columns:
                print(f"Migrating {col} to custom_{col}...")
                c.execute(f'ALTER TABLE twitter_accounts ADD COLUMN custom_{col} TEXT')
                c.execute(f'UPDATE twitter_accounts SET custom_{col} = {col}')
            
        conn.commit()
        print("Database migration completed successfully")
    except Error as e:
        print(f"Database migration error: {e}")
    finally:
        if conn:
            conn.close()

def init_db():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Add RSS Feeds table
        c.execute('''
        CREATE TABLE IF NOT EXISTS rss_feeds (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            description TEXT
        )
        ''')
        
        # Add RSS Posting Configs table
        c.execute('''
        CREATE TABLE IF NOT EXISTS rss_posting_configs (
            id INTEGER PRIMARY KEY,
            account_id INTEGER,
            feed_id INTEGER,
            persona_id INTEGER,
            is_active BOOLEAN DEFAULT 0,
            schedule_settings TEXT,
            posts_per_day INTEGER DEFAULT 1,
            FOREIGN KEY (account_id) REFERENCES twitter_accounts (id),
            FOREIGN KEY (feed_id) REFERENCES rss_feeds (id),
            FOREIGN KEY (persona_id) REFERENCES personas (id)
        )
        ''')
        
        # Create twitter_accounts table
        c.execute('''
            CREATE TABLE IF NOT EXISTS twitter_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL,
                consumer_key TEXT NOT NULL,
                consumer_secret TEXT NOT NULL,
                access_token TEXT NOT NULL,
                access_token_secret TEXT NOT NULL,
                is_active BOOLEAN DEFAULT 0,
                persona_id INTEGER,
                automation_persona_id INTEGER,
                custom_style TEXT DEFAULT 'neutral',
                custom_length TEXT DEFAULT 'medium',
                custom_speech_settings TEXT DEFAULT 'text',
                custom_use_emoji BOOLEAN DEFAULT 0,
                custom_use_hashtags BOOLEAN DEFAULT 0,
                custom_prompt TEXT,
                automation_mode TEXT DEFAULT 'manual',
                automation_settings TEXT,
                FOREIGN KEY (persona_id) REFERENCES personas(id),
                FOREIGN KEY (automation_persona_id) REFERENCES personas(id)
            )
        ''')
        
        # Create posted_comments table
        c.execute('''
            CREATE TABLE IF NOT EXISTS posted_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                tweet_id TEXT,
                tweet_text TEXT,
                tweet_author TEXT,
                tweet_url TEXT,
                comment_text TEXT,
                comment_url TEXT,
                quality_score REAL,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES twitter_accounts (id)
            )
        ''')
        
        # Create commented_tweets table
        c.execute('''
            CREATE TABLE IF NOT EXISTS commented_tweets (
                tweet_id TEXT PRIMARY KEY,
                account_id INTEGER,
                quality_score REAL,
                commented_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES twitter_accounts (id)
            )
        ''')
        
        # Create personas table
        c.execute('''
            CREATE TABLE IF NOT EXISTS personas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                style TEXT,
                length TEXT,
                speech_settings TEXT,
                use_emoji BOOLEAN DEFAULT 0,
                use_hashtags BOOLEAN DEFAULT 0,
                custom_prompt TEXT,
                mention_user BOOLEAN DEFAULT 0
            )
        ''')
        
        # Create automation_notifications table
        c.execute('''
            CREATE TABLE IF NOT EXISTS automation_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                type TEXT NOT NULL,
                message TEXT NOT NULL,
                retry_time TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                read BOOLEAN DEFAULT 0,
                FOREIGN KEY (account_id) REFERENCES twitter_accounts (id)
            )
        ''')
        
        # Create scheduled_posts table
        c.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER,
                tweet_id TEXT,
                tweet_text TEXT,
                comment_text TEXT,
                scheduled_time TIMESTAMP,
                status TEXT DEFAULT 'pending',
                comment_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES twitter_accounts (id)
            )
        ''')
        
        c.execute('''
        CREATE TABLE IF NOT EXISTS rss_items (
            id INTEGER PRIMARY KEY,
            config_id INTEGER,
            title TEXT,
            link TEXT,
            description TEXT,
            published_at TEXT,
            processed BOOLEAN DEFAULT 0,
            tweet_text TEXT,
            posted BOOLEAN DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (config_id) REFERENCES rss_posting_configs (id)
        )
        ''')
        
        # Add RSS settings to twitter_accounts table
        c.execute('''
        ALTER TABLE twitter_accounts ADD COLUMN IF NOT EXISTS rss_settings TEXT DEFAULT '{
            "posts_per_day": 3,
            "max_posts_per_feed": 10,
            "min_article_age": "1h",
            "max_article_age": "24h",
            "avoid_duplicates": true,
            "custom_rules": ""
        }'
        ''')
        
        conn.commit()
        conn.close()
    except Error as e:
        print(f"Database error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def get_active_account():
    try:
        print("\n[GET_ACTIVE_ACCOUNT] Fetching active account")
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Get active account with explicit column selection
        c.execute('''
            SELECT id, account_name, consumer_key, consumer_secret, 
                   access_token, access_token_secret, persona_id,
                   custom_style, custom_length, custom_speech_settings,
                   custom_use_emoji, custom_use_hashtags, custom_prompt
            FROM twitter_accounts 
            WHERE is_active = 1
        ''')
        account = c.fetchone()
        
        if not account:
            print("[GET_ACTIVE_ACCOUNT] No active account found")
            return None
            
        # Handle persona_id conversion safely
        try:
            persona_id = int(account[6]) if account[6] is not None else None
        except (ValueError, TypeError):
            print(f"[GET_ACTIVE_ACCOUNT] Warning: Invalid persona_id value: {account[6]}, setting to None")
            persona_id = None
        
        # Create base account dict with custom settings
        account_dict = {
            'id': account[0],
            'account_name': account[1],
            'consumer_key': account[2],
            'consumer_secret': account[3],
            'access_token': account[4],
            'access_token_secret': account[5],
            'persona_id': persona_id,
            'custom_style': account[7],
            'custom_length': account[8],
            'custom_speech_settings': account[9],
            'custom_use_emoji': bool(account[10]),
            'custom_use_hashtags': bool(account[11]),
            'custom_prompt': account[12]
        }
        
        print("[GET_ACTIVE_ACCOUNT] Initial account data:")
        for key, value in account_dict.items():
            if not key.startswith(('consumer', 'access')):
                print(f"{key}: {value}")
        
        # If persona is set, its settings completely override custom settings
        if persona_id is not None:
            print(f"[GET_ACTIVE_ACCOUNT] Found persona_id: {persona_id}, fetching persona details")
            persona = get_persona(persona_id)
            if persona:
                print("[GET_ACTIVE_ACCOUNT] Using persona settings")
                account_dict.update({
                    'style': persona['style'],
                    'length': persona['length'],
                    'speech_settings': persona['speech_settings'],
                    'use_emoji': persona['use_emoji'],
                    'use_hashtags': persona['use_hashtags'],
                    'prompt': persona['custom_prompt']
                })
                return account_dict
        
        # No persona or persona not found, use validated custom settings
        print("[GET_ACTIVE_ACCOUNT] Using custom settings")
        valid_styles = {'official', 'casual', 'neutral', 'ironic', 'sarcastic', 'gen-z'}
        valid_lengths = {'short', 'medium', 'long'}
        valid_speech = {'text', 'text with a question'}
        
        style = account_dict['custom_style'].lower() if account_dict['custom_style'] else 'neutral'
        length = account_dict['custom_length'].lower() if account_dict['custom_length'] else 'medium'
        speech_settings = account_dict['custom_speech_settings'].lower() if account_dict['custom_speech_settings'] else 'text'
        
        account_dict.update({
            'style': style if style in valid_styles else 'neutral',
            'length': length if length in valid_lengths else 'medium',
            'speech_settings': speech_settings if speech_settings in valid_speech else 'text',
            'use_emoji': account_dict['custom_use_emoji'],
            'use_hashtags': account_dict['custom_use_hashtags'],
            'prompt': account_dict['custom_prompt'] if isinstance(account_dict['custom_prompt'], str) and not account_dict['custom_prompt'].startswith('20') else None
        })
        
        print("[GET_ACTIVE_ACCOUNT] Final account settings:")
        for key, value in account_dict.items():
            if not key.startswith(('consumer', 'access')):
                print(f"{key}: {value}")
        
        return account_dict
    except Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def get_all_accounts():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('''
            SELECT id, account_name, is_active, persona_id,
                   custom_style, custom_length, custom_speech_settings,
                   custom_use_emoji, custom_use_hashtags, custom_prompt,
                   automation_mode, automation_settings
            FROM twitter_accounts
        ''')
        accounts = c.fetchall()
        result = []
        
        # Default schedule structure
        default_schedule = {
            'monday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'tuesday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'wednesday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'thursday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'friday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'saturday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'sunday': {'enabled': False, 'start': '09:00', 'end': '17:00'}
        }
        
        for a in accounts:
            # Parse automation settings
            try:
                automation_settings = json.loads(a[11]) if a[11] else {}
            except (json.JSONDecodeError, TypeError):
                automation_settings = {}
            
            settings_updated = False
            
            # Ensure working_schedule exists and has correct structure
            if 'working_schedule' not in automation_settings:
                automation_settings['working_schedule'] = default_schedule.copy()
                settings_updated = True
            else:
                # Ensure working_schedule is a dictionary
                if not isinstance(automation_settings['working_schedule'], dict):
                    automation_settings['working_schedule'] = default_schedule.copy()
                    settings_updated = True
                else:
                    # Merge with default schedule to ensure all days exist
                    current_schedule = automation_settings['working_schedule']
                    for day, settings in default_schedule.items():
                        if day not in current_schedule:
                            current_schedule[day] = settings.copy()
                            settings_updated = True
                        else:
                            # Ensure each day has all required fields
                            for field, value in settings.items():
                                if field not in current_schedule[day]:
                                    current_schedule[day][field] = value
                                    settings_updated = True
                            # Convert enabled to boolean if it's not already
                            current_schedule[day]['enabled'] = bool(current_schedule[day].get('enabled', False))
            
            # If settings were updated, save back to database
            if settings_updated:
                c.execute('''
                    UPDATE twitter_accounts 
                    SET automation_settings = ?
                    WHERE id = ?
                ''', (json.dumps(automation_settings), a[0]))
                conn.commit()
            
            account_dict = {
                'id': a[0],
                'account_name': a[1],
                'is_active': bool(a[2]),
                'persona_id': a[3],
                'custom_style': a[4],
                'custom_length': a[5],
                'custom_speech_settings': a[6],
                'custom_use_emoji': bool(a[7]),
                'custom_use_hashtags': bool(a[8]),
                'custom_prompt': a[9],
                'automation_mode': a[10] or 'manual',
                'automation_settings': automation_settings
            }
            
            # If persona is set, get persona details
            if account_dict['persona_id']:
                persona = get_persona(account_dict['persona_id'])
                if persona:
                    account_dict.update({
                        'style': persona['style'],
                        'length': persona['length'],
                        'speech_settings': persona['speech_settings'],
                        'use_emoji': persona['use_emoji'],
                        'use_hashtags': persona['use_hashtags'],
                        'custom_prompt': persona['custom_prompt']
                    })
            else:
                # Use custom settings if no persona
                account_dict.update({
                    'style': account_dict['custom_style'],
                    'length': account_dict['custom_length'],
                    'speech_settings': account_dict['custom_speech_settings'],
                    'use_emoji': account_dict['custom_use_emoji'],
                    'use_hashtags': account_dict['custom_use_hashtags']
                })
            result.append(account_dict)
        return result
    except Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if conn:
            conn.close()

def update_account_settings_directly(account_id, style, length, speech_settings, use_emoji, use_hashtags, custom_prompt):
    try:
        # Validate values against allowed sets
        valid_styles = {'official', 'casual', 'neutral', 'ironic', 'sarcastic', 'gen-z'}
        valid_lengths = {'short', 'medium', 'long'}
        valid_speech = {'text', 'text with a question'}

        # Normalize and validate inputs
        style = style.lower() if style else 'neutral'
        length = length.lower() if length else 'medium'
        speech_settings = speech_settings.lower() if speech_settings else 'text'

        if style not in valid_styles:
            style = 'neutral'
        if length not in valid_lengths:
            length = 'medium'
        if speech_settings not in valid_speech:
            speech_settings = 'text'

        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('''
            UPDATE twitter_accounts 
            SET custom_style = ?, 
                custom_length = ?, 
                custom_speech_settings = ?, 
                custom_use_emoji = ?, 
                custom_use_hashtags = ?, 
                custom_prompt = ?
            WHERE id = ?
        ''', (style, length, speech_settings, 
              1 if use_emoji else 0, 
              1 if use_hashtags else 0, 
              custom_prompt if isinstance(custom_prompt, str) and not custom_prompt.startswith('20') else None, 
              account_id))
        conn.commit()
        return True
    except Error as e:
        print(f"Database error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def generate_persona_prompt(account):
    print("\n[GENERATE_PERSONA_PROMPT] Generating prompt with account data:")
    for key, value in account.items():
        if not key.startswith(('consumer', 'access')):  # Skip sensitive data
            print(f"{key}: {value}")
    
    style_descriptions = {
        'official': "Maintain a professional and formal tone, using business-appropriate language and avoiding colloquialisms.",
        'casual': "Use relaxed, everyday language while staying friendly and approachable.",
        'neutral': "Keep a balanced tone that's neither too formal nor too casual.",
        'ironic': "Employ subtle humor and wit, using understated contradictions to make points.",
        'sarcastic': "Use sharp wit and pointed observations, staying on the border of appropriate bounds.",
        'gen-z': "Incorporate contemporary internet slang and casual language patterns typical of Generation Z."
    }
    
    length_descriptions = {
        'short': "Keep responses concise and to the point, under 50 characters.",
        'medium': "Write moderately detailed responses between 50-120 characters.",
        'long': "Provide comprehensive responses up to 250 characters."
    }
    
    speech_descriptions = {
        'text': "Write declarative statements without questions or exclamations.",
        'text with a question': "Include a relevant question that encourages engagement with the original tweet."
    }
    
    # Get settings based on whether a persona is set
    if account.get('persona_id') is not None:
        print("[GENERATE_PERSONA_PROMPT] Using persona settings")
        persona = get_persona(account['persona_id'])
        if persona:
            style = persona['style']
            length = persona['length']
            speech_setting = persona['speech_settings']
            use_emoji = persona['use_emoji']
            use_hashtags = persona['use_hashtags']
            custom_prompt = persona['custom_prompt']
        else:
            # Fallback to custom settings if persona not found
            style = account.get('custom_style', 'neutral')
            length = account.get('custom_length', 'medium')
            speech_setting = account.get('custom_speech_settings', 'text')
            use_emoji = account.get('custom_use_emoji', False)
            use_hashtags = account.get('custom_use_hashtags', False)
            custom_prompt = account.get('custom_prompt')
    else:
        print("[GENERATE_PERSONA_PROMPT] Using custom settings")
        style = account.get('custom_style', 'neutral')
        length = account.get('custom_length', 'medium')
        speech_setting = account.get('custom_speech_settings', 'text')
        use_emoji = account.get('custom_use_emoji', False)
        use_hashtags = account.get('custom_use_hashtags', False)
        custom_prompt = account.get('custom_prompt')
    
    # Validate and normalize settings
    if style not in style_descriptions:
        print(f"[GENERATE_PERSONA_PROMPT] Warning: Invalid style '{style}', using 'neutral'")
        style = 'neutral'
    
    if length not in length_descriptions:
        print(f"[GENERATE_PERSONA_PROMPT] Warning: Invalid length '{length}', using 'medium'")
        length = 'medium'
    
    if speech_setting not in speech_descriptions:
        print(f"[GENERATE_PERSONA_PROMPT] Warning: Invalid speech setting '{speech_setting}', using 'text'")
        speech_setting = 'text'
    
    # Convert string/numeric booleans to actual booleans
    use_emoji = bool(use_emoji) if isinstance(use_emoji, (bool, int)) else False
    use_hashtags = bool(use_hashtags) if isinstance(use_hashtags, (bool, int)) else False
    
    # Validate custom_prompt is a string and not a timestamp
    if custom_prompt and (not isinstance(custom_prompt, str) or custom_prompt.startswith('20')):
        print("[GENERATE_PERSONA_PROMPT] Warning: Invalid custom prompt, ignoring")
        custom_prompt = None
    
    print(f"[GENERATE_PERSONA_PROMPT] Final settings being used:")
    print(f"style: {style}")
    print(f"length: {length}")
    print(f"speech_setting: {speech_setting}")
    print(f"use_emoji: {use_emoji}")
    print(f"use_hashtags: {use_hashtags}")
    
    # Get exact descriptions
    style_desc = style_descriptions.get(style, style)
    length_desc = length_descriptions.get(length, length)
    speech_desc = speech_descriptions.get(speech_setting, speech_setting)
    
    base_prompt = f"""You are a social media expert crafting responses with these specific characteristics:

1. Style: {style_desc}
2. Length: {length_desc}
3. Format: {speech_desc}
4. Emoji Usage: {"Include relevant emojis to enhance expression" if use_emoji else "Do not use any emojis"}
5. Hashtags: {"Add relevant hashtags to increase visibility" if use_hashtags else "Do not use hashtags"}

Additional guidelines:
- Stay relevant to the original tweet's content and context
- Focus on required style, length, and format
- Keep responses within Twitter's character limit
- If the topic is controversial you can be more aggressive, but not abusive or offensive
- Consider diverse perspectives"""

    if custom_prompt:
        print("[GENERATE_PERSONA_PROMPT] Adding custom prompt")
        base_prompt += f"\n\nAdditional instructions:\n{custom_prompt}"
    
    print("[GENERATE_PERSONA_PROMPT] Generated prompt:")
    print(base_prompt)
    return base_prompt

@app.route('/accounts', methods=['GET', 'POST'])
def manage_accounts():
    if request.method == 'POST':
        account_name = request.form['account_name']
        consumer_key = request.form['consumer_key']
        consumer_secret = request.form['consumer_secret']
        access_token = request.form['access_token']
        access_token_secret = request.form['access_token_secret']
        
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            c.execute('''
                INSERT INTO twitter_accounts 
                (account_name, consumer_key, consumer_secret, access_token, access_token_secret, is_active) 
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (account_name, consumer_key, consumer_secret, access_token, access_token_secret, False))
            
            account_id = c.lastrowid
            conn.commit()
            
            # Initialize automation settings for the new account
            initialize_automation_settings(account_id)
            
            return redirect(url_for('manage_accounts'))
        except Exception as e:
            print(f"Error creating account: {e}")
            return "Error creating account", 500
        finally:
            if conn:
                conn.close()
    
    # GET request handling
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Get all accounts with their settings
        c.execute('''
            SELECT id, account_name, is_active, automation_mode, automation_settings, 
                   persona_id, automation_persona_id,
                   custom_style, custom_length, custom_speech_settings, 
                   custom_use_emoji, custom_use_hashtags, custom_prompt
            FROM twitter_accounts
        ''')
        accounts = []
        for row in c.fetchall():
            account = {
                'id': row[0],
                'account_name': row[1],
                'is_active': bool(row[2]),
                'automation_mode': row[3] or 'manual',
                'automation_settings': json.loads(row[4]) if row[4] else {},
                'persona_id': row[5],
                'automation_persona_id': row[6],
                'custom_style': row[7],
                'custom_length': row[8],
                'custom_speech_settings': row[9],
                'custom_use_emoji': bool(row[10]),
                'custom_use_hashtags': bool(row[11]),
                'custom_prompt': row[12]
            }
            
            # Ensure automation settings have the correct structure
            if not account['automation_settings'] or 'working_schedule' not in account['automation_settings']:
                initialize_automation_settings(account['id'])
                # Reload the account's settings
                c.execute('SELECT automation_settings FROM twitter_accounts WHERE id = ?', (account['id'],))
                settings_row = c.fetchone()
                if settings_row and settings_row[0]:
                    account['automation_settings'] = json.loads(settings_row[0])
            
            accounts.append(account)
        
        # Get all personas
        personas = get_all_personas()
        
        return render_template('accounts.html', accounts=accounts, personas=personas)
        
    except Exception as e:
        print(f"Error loading accounts: {e}")
        return "Error loading accounts", 500
    finally:
        if conn:
            conn.close()

@app.route('/switch_account/<int:account_id>', methods=['POST'])
def switch_account(account_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        # Set all accounts to inactive
        c.execute('UPDATE twitter_accounts SET is_active = 0')
        # Set the selected account to active
        c.execute('UPDATE twitter_accounts SET is_active = 1 WHERE id = ?', (account_id,))
        conn.commit()
        flash('Account switched successfully!', 'success')
    except Error as e:
        flash(f'Error switching account: {str(e)}', 'error')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('index'))

# Initialize the database and run migrations when the app starts
init_db()
migrate_db()

# Update the index route to include active account info
@app.route('/')
def index():
    return render_template('index.html')

# Update the twitter client initialization in relevant routes
def get_twitter_client():
    account = get_active_account()
    if not account:
        raise Exception("No active Twitter account found")
    return TwitterClient(
        consumer_key=account['consumer_key'],
        consumer_secret=account['consumer_secret'],
        access_token=account['access_token'],
        access_token_secret=account['access_token_secret']
    )

# Update the post_comment route to save successful comments
@app.route('/post-comment', methods=['POST'])
def post_comment():
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400
            
        tweet_id = data.get('tweet_id')
        comment = data.get('comment')
        tweet_text = data.get('tweet_text')
        tweet_author = data.get('tweet_author')
        tweet_url = data.get('tweet_url')
        
        if not all([tweet_id, comment, tweet_text, tweet_author, tweet_url]):
            missing = [k for k, v in {'tweet_id': tweet_id, 'comment': comment, 
                                    'tweet_text': tweet_text, 'tweet_author': tweet_author, 
                                    'tweet_url': tweet_url}.items() if not v]
            return jsonify({"success": False, "error": f"Missing required fields: {', '.join(missing)}"}), 400
        
        # Get a fresh client with the current active account
        twitter_client = get_twitter_client()
        response = twitter_client.create_tweet(
            text=comment,
            reply={"in_reply_to_tweet_id": tweet_id}
        )
        
        if not response or not response.get('data', {}).get('id'):
            return jsonify({"success": False, "error": "Failed to post comment to Twitter"}), 500
            
        # Save the comment to our database
        active_account = get_active_account()
        if not active_account:
            return jsonify({"success": False, "error": "No active account found"}), 400
            
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('''
            INSERT INTO posted_comments (
                account_id, tweet_id, tweet_text, tweet_author, 
                tweet_url, comment_text, comment_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            active_account['id'],
            tweet_id,
            tweet_text,
            tweet_author,
            tweet_url,
            comment,
            f"https://twitter.com/i/web/status/{response['data']['id']}"
        ))
        conn.commit()
        conn.close()
            
        return jsonify({
            "success": True, 
            "response": response,
            "message": "Comment posted successfully"
        })
    except Exception as e:
        print(f"Error in post_comment: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 500

def evaluate_tweet_quality(tweet_text):
    try:
        prompt = f"""Please evaluate the following tweet and assign a score from 1 to 5 based on these criteria, be as objective as you can:

1 - Illegal, abusive, advertisements, meaningless, or too short to understand context
2 - Likely AI generated or spam
3 - Legitimate but generic or lacking full context
4 - Good quality, human-written, legal, with clear context
5 - Excellent quality, definitely human, no spam, fully understandable context

Tweet: "{tweet_text}"

Respond with just the score number (1-5) followed by a brief explanation in this format:
SCORE: [number]
REASON: [brief explanation]"""

        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a tweet quality evaluator. Analyze tweets and assign quality scores from 1-5."},
                {"role": "user", "content": prompt}
            ]
        )
        
        result = response.choices[0].message.content.strip()
        score_line = next(line for line in result.split('\n') if line.startswith('SCORE:'))
        reason_line = next(line for line in result.split('\n') if line.startswith('REASON:'))
        
        score = int(score_line.split(':')[1].strip())
        reason = reason_line.split(':')[1].strip()
        
        return {"score": score, "reason": reason}
    except Exception as e:
        print(f"Error evaluating tweet quality: {str(e)}")
        return {"score": 0, "reason": "Error evaluating tweet"}

@app.route('/search', methods=['POST'])
def search_tweets():
    try:
        data = request.json
        if not data or not data.get('searchTerms'):
            return jsonify({"error": "Search terms are required"}), 400

        print(f"Received search request with data: {data}")
        
        # Get active account for filtering commented tweets
        active_account = get_active_account()
        if not active_account:
            return jsonify({"error": "No active account found"}), 400

        # Get list of tweets already commented on by active account
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('SELECT tweet_id FROM posted_comments WHERE account_id = ?', (active_account['id'],))
        commented_tweet_ids = {row[0] for row in c.fetchall()}
        conn.close()

        # Prepare search criteria for Kaito API
        search_input = {
            "searchTerms": [term for term in data.get('searchTerms', [])],
            "maxItems": data.get('maxItems', 50),
            "queryType": "Latest",
            "lang": "en"
        }
        
        # Add minimum engagement metrics to search terms
        min_retweets = data.get('minimumRetweets', 0)
        min_likes = data.get('minimumLikes', 0)
        min_replies = data.get('minimumReplies', 0)
        
        if min_retweets > 0:
            search_input["searchTerms"] = [f"{term} min_retweets:{min_retweets}" for term in search_input["searchTerms"]]
        if min_likes > 0:
            search_input["searchTerms"] = [f"{term} min_faves:{min_likes}" for term in search_input["searchTerms"]]
        if min_replies > 0:
            search_input["searchTerms"] = [f"{term} min_replies:{min_replies}" for term in search_input["searchTerms"]]
        
        # Add optional parameters if they have non-empty values
        if data.get('start'):
            search_input["since"] = f"{data.get('start')}_00:00:00_UTC"
        if data.get('end'):
            search_input["until"] = f"{data.get('end')}_23:59:59_UTC"
        if data.get('geotaggedNear'):
            search_input["near"] = data.get('geotaggedNear')
        
        print(f"Calling Kaito API with input: {search_input}")
        
        try:
            run = apify_client.actor("kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest").call(run_input=search_input)
            if not run:
                return jsonify({"error": "Failed to start Kaito API actor"}), 500
                
            print(f"Kaito API run started with ID: {run.get('id')}")
            
            # Fetch and process results
            dataset_items = list(apify_client.dataset(run["defaultDatasetId"]).iterate_items())
            print(f"Retrieved {len(dataset_items)} tweets from Kaito API")
            
            # Process and validate tweets
            processed_tweets = []
            for tweet in dataset_items:
                try:
                    # Skip already commented tweets
                    if str(tweet.get('id')) in commented_tweet_ids:
                        continue
                        
                    # Map Kaito API response to our standard format
                    processed_tweet = {
                        'id': str(tweet.get('id')),
                        'text': tweet.get('text', ''),
                        'url': tweet.get('url', ''),
                        'twitterUrl': tweet.get('twitterUrl', ''),
                        'createdAt': tweet.get('createdAt'),
                        'retweetCount': tweet.get('retweetCount', 0),
                        'replyCount': tweet.get('replyCount', 0),
                        'likeCount': tweet.get('likeCount', 0),
                        'quoteCount': tweet.get('quoteCount', 0),
                        'bookmarkCount': tweet.get('bookmarkCount', 0),
                        'viewCount': tweet.get('viewCount', 0),
                        'author': {
                            'name': tweet.get('author', {}).get('name', ''),
                            'userName': tweet.get('author', {}).get('userName', ''),
                            'profilePicture': tweet.get('author', {}).get('profilePicture', ''),
                            'isVerified': tweet.get('author', {}).get('isBlueVerified', False),
                            'verifiedType': 'blue' if tweet.get('author', {}).get('isBlueVerified') else None
                        },
                        'extendedEntities': tweet.get('extendedEntities', {}),
                        'isReply': tweet.get('isReply', False),
                        'isPinned': tweet.get('isPinned', False),
                        'lang': tweet.get('lang', 'en')
                    }
                    
                    # Add quality score
                    processed_tweet['quality'] = evaluate_tweet_quality(processed_tweet['text'])
                    processed_tweets.append(processed_tweet)
                except Exception as e:
                    print(f"Error processing tweet: {str(e)}")
                    continue
            
            print(f"Returning {len(processed_tweets)} processed tweets")
            return jsonify(processed_tweets)
            
        except Exception as api_error:
            print(f"Kaito API error: {str(api_error)}")
            return jsonify({"error": f"Kaito API error: {str(api_error)}"}), 500

    except Exception as e:
        print(f"Error in search_tweets: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/update-persona/<int:account_id>', methods=['POST'])
def update_account_settings(account_id):
    try:
        data = request.json
        success = update_account_settings_directly(
            account_id,
            data['style'],
            data['length'],
            data['speech_settings'],
            data['use_emoji'],
            data['use_hashtags'],
            data.get('custom_prompt')
        )
        if success:
            flash('Account settings updated successfully!', 'success')
            return jsonify({'success': True})
        else:
            flash('Error updating account settings', 'error')
            return jsonify({'success': False, 'error': 'Database error'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def get_system_prompt(account_id):
    """Get the system prompt based on account's persona settings"""
    try:
        print(f"\n[GET_SYSTEM_PROMPT] Getting prompt for account: {account_id}")
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Get persona_id for manual comments
        c.execute('SELECT persona_id FROM twitter_accounts WHERE id = ?', (account_id,))
        result = c.fetchone()
        
        if not result or not result[0]:
            print("[GET_SYSTEM_PROMPT] No persona_id found for account")
            return None
            
        persona_id = result[0]
        print(f"[GET_SYSTEM_PROMPT] Using persona_id: {persona_id}")
            
        # Get the persona settings
        c.execute('''
            SELECT name, description, style, length, 
                   use_emoji, use_hashtags, custom_prompt, mention_user
            FROM personas 
            WHERE id = ?
        ''', (persona_id,))
        
        persona = c.fetchone()
        if not persona:
            print("[GET_SYSTEM_PROMPT] No persona found")
            return None
            
        name, description, style, length, use_emoji, use_hashtags, custom_prompt, mention_user = persona
        
        print(f"[GET_SYSTEM_PROMPT] Found persona settings:")
        print(f"name: {name}")
        print(f"style: {style}")
        print(f"length: {length}")
        print(f"use_emoji: {use_emoji}")
        print(f"use_hashtags: {use_hashtags}")
        print(f"mention_user: {mention_user}")
        
        # Define style mappings
        style_descriptions = {
            'neutral': 'maintain a balanced and objective tone',
            'casual': 'use a relaxed and informal tone of a young adult',
            'official': 'maintain a formal and professional tone',
            'ironic': 'use irony and wit',
            'sarcastic': 'employ heavy sarcasm and humor',
            'gen-z': 'use contemporary, youthful language similiar to gen-z language but do not overuse it'
        }
        
        # Define length constraints
        length_descriptions = {
            'short': 'keep responses very brief (30-50 characters)',
            'medium': 'write moderate length responses (50-120 characters)',
            'long': 'provide detailed responses (120-250 characters)'
        }
        
        # Build the base prompt
        base_prompt = f"""You are a knowledgeable commenter who specializes in engaging with content about {description}. 
Your responses should {style_descriptions.get(style, 'maintain a neutral tone')}, {length_descriptions.get(length, 'write moderate length responses')}.

CRITICAL RULES:
1. NEVER include any user mentions (like @username) in your response
2. NEVER use quotation marks in your response
3. Write the comment as plain text that will be used as a reply
4. Focus on the content and context of the tweet
5. Keep the tone consistent with the style specified
6. {"Include relevant emojis" if use_emoji else "DO NOT use emojis"}
7. {"Add relevant hashtags" if use_hashtags else "DO NOT use hashtags"}
8. NEVER truncate or add ellipsis (...)
"""
        
        # Add custom prompt if available
        if custom_prompt:
            base_prompt += f"\nAdditional instructions:\n{custom_prompt}"
            
        return base_prompt
        
    except Error as e:
        print(f"Database error getting system prompt: {e}")
        return None
    finally:
        if conn:
            conn.close()

@app.route('/generate-comment', methods=['POST'])
def generate_comment():
    try:
        print("\n[GENERATE_COMMENT] Starting comment generation")
        data = request.json
        tweet_text = data.get('tweet_text')
        username = data.get('username')
        
        print(f"[GENERATE_COMMENT] Input data:")
        print(f"tweet_text: {tweet_text}")
        print(f"username: {username}")
        
        # Get active account
        active_account = get_active_account()
        if not active_account:
            print("[GENERATE_COMMENT] No active account found")
            return jsonify({"error": "No active account found"}), 400
            
        print(f"[GENERATE_COMMENT] Active account: {active_account['id']} - {active_account.get('account_name')}")
            
        # Get system prompt based on account's persona
        system_prompt = get_system_prompt(active_account['id'])
        if not system_prompt:
            print("[GENERATE_COMMENT] No persona settings found")
            return jsonify({"error": "No persona settings found"}), 400
            
        print(f"[GENERATE_COMMENT] System prompt: {system_prompt}")
            
        # Create user prompt
        user_prompt = f"""Based on the following tweet: "{tweet_text}"

Generate a relevant, engaging reply that matches the context and tone of the original tweet.
CRITICAL RULES:
1. DO NOT include any usernames or @mentions
2. Keep your response STRICTLY under 100 characters
3. Make it a complete thought - no ellipsis or truncation
4. Focus on quality over length - shorter is better
5. Be concise but meaningful"""

        print(f"[GENERATE_COMMENT] User prompt: {user_prompt}")

        # Generate comment using OpenAI
        print("[GENERATE_COMMENT] Sending request to OpenAI")
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.7,
            max_tokens=100
        )
        
        comment = response.choices[0].message.content.strip()
        print(f"[GENERATE_COMMENT] Raw generated comment: {comment}")
        
        return jsonify({"comment": comment})
        
    except Exception as e:
        print(f"[GENERATE_COMMENT] Error: {str(e)}")
        print("[GENERATE_COMMENT] Full traceback:", format_exc())
        return jsonify({"error": "Error generating comment. Please try again."}), 500

def get_comment_history(account_id=None):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        if account_id:
            c.execute('''
                SELECT pc.*, ta.account_name 
                FROM posted_comments pc
                JOIN twitter_accounts ta ON pc.account_id = ta.id
                WHERE pc.account_id = ?
                ORDER BY pc.posted_at DESC
            ''', (account_id,))
        else:
            c.execute('''
                SELECT pc.*, ta.account_name 
                FROM posted_comments pc
                JOIN twitter_accounts ta ON pc.account_id = ta.id
                ORDER BY pc.posted_at DESC
            ''')
            
        columns = [description[0] for description in c.description]
        comments = []
        for row in c.fetchall():
            comment_dict = dict(zip(columns, row))
            comments.append(comment_dict)
        return comments
    except Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if conn:
            conn.close()

@app.route('/comment-history')
def comment_history():
    active_account = get_active_account()
    account_id = request.args.get('account_id', type=int)
    comments = get_comment_history(account_id)
    accounts = get_all_accounts()
    return render_template('comment_history.html', 
                         comments=comments, 
                         accounts=accounts,
                         active_account=active_account)

def get_all_personas():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('SELECT * FROM personas ORDER BY name')
        personas = c.fetchall()
        return [{
            'id': p[0],
            'name': p[1],
            'description': p[2],
            'style': p[3],
            'length': p[4],
            'speech_settings': p[5],
            'use_emoji': bool(p[6]),
            'use_hashtags': bool(p[7]),
            'custom_prompt': p[8]
        } for p in personas]
    except Error as e:
        print(f"Database error: {e}")
        return []
    finally:
        if conn:
            conn.close()

def get_persona(persona_id):
    try:
        print(f"\n[GET_PERSONA] Fetching persona with ID: {persona_id}")
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('''
            SELECT id, name, description, style, length, speech_settings, 
                   use_emoji, use_hashtags, custom_prompt, mention_user 
            FROM personas 
            WHERE id = ?
        ''', (persona_id,))
        columns = [description[0] for description in c.description]
        p = c.fetchone()
        if p:
            persona = dict(zip(columns, p))
            # Convert boolean fields
            persona['use_emoji'] = bool(persona['use_emoji'])
            persona['use_hashtags'] = bool(persona['use_hashtags'])
            persona['mention_user'] = bool(persona['mention_user'])
            print(f"[GET_PERSONA] Found persona:")
            for key, value in persona.items():
                print(f"{key}: {value}")
            return persona
        print("[GET_PERSONA] No persona found")
        return None
    except Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def create_persona(name, description, style, length, speech_settings, use_emoji, use_hashtags, custom_prompt, mention_user=False):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Set default values
        style = style or 'neutral'
        length = length or 'medium'
        speech_settings = speech_settings or 'text'
        
        print(f"\n[CREATE_PERSONA] Input data:")
        print(f"name: {name}")
        print(f"description: {description}")
        print(f"style: {style}")
        print(f"length: {length}")
        print(f"speech_settings: {speech_settings}")
        print(f"use_emoji: {use_emoji}")
        print(f"use_hashtags: {use_hashtags}")
        print(f"custom_prompt: {custom_prompt}")
        print(f"mention_user: {mention_user}")
        
        # Normalize values
        style = style.lower() if style else 'neutral'
        length = length.lower() if length else 'medium'
        speech_settings = speech_settings.lower() if speech_settings else 'text'
        if speech_settings == 'text only':
            speech_settings = 'text'
        
        # Validate values
        valid_styles = ['official', 'casual', 'neutral', 'ironic', 'sarcastic', 'gen-z']
        valid_lengths = ['short', 'medium', 'long']
        valid_speech = ['text', 'text with a question']
        
        if style not in valid_styles:
            print(f"[CREATE_PERSONA] Invalid style '{style}', using 'neutral'")
            style = 'neutral'
        if length not in valid_lengths:
            print(f"[CREATE_PERSONA] Invalid length '{length}', using 'medium'")
            length = 'medium'
        if speech_settings not in valid_speech:
            print(f"[CREATE_PERSONA] Invalid speech setting '{speech_settings}', using 'text'")
            speech_settings = 'text'
            
        # Convert boolean values to integers
        use_emoji = 1 if use_emoji else 0
        use_hashtags = 1 if use_hashtags else 0
        mention_user = 1 if mention_user else 0
        
        c.execute('''
            INSERT INTO personas (
                name, description, style, length, speech_settings,
                use_emoji, use_hashtags, custom_prompt, mention_user
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, description, style, length, speech_settings,
              use_emoji, use_hashtags, custom_prompt, mention_user))
        conn.commit()
        persona_id = c.lastrowid
        print(f"[CREATE_PERSONA] Created persona with ID: {persona_id}")
        return persona_id
    except Error as e:
        print(f"Database error: {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_persona(persona_id, name, description, style, length, speech_settings, use_emoji, use_hashtags, mention_user, custom_prompt):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('''
            UPDATE personas 
            SET name = ?, description = ?, style = ?, length = ?,
                speech_settings = ?, use_emoji = ?, use_hashtags = ?,
                custom_prompt = ?, mention_user = ?
            WHERE id = ?
        ''', (name, description, style, length, speech_settings,
              int(use_emoji), int(use_hashtags), custom_prompt, 
              int(mention_user), persona_id))
        conn.commit()
        return True
    except Error as e:
        print(f"Database error: {e}")
        return False
    finally:
        if conn:
            conn.close()

def delete_persona(persona_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        # First, set any accounts using this persona to null
        c.execute('UPDATE twitter_accounts SET persona_id = NULL WHERE persona_id = ?', (persona_id,))
        # Then delete the persona
        c.execute('DELETE FROM personas WHERE id = ?', (persona_id,))
        conn.commit()
        return True
    except Error as e:
        print(f"Database error: {e}")
        return False
    finally:
        if conn:
            conn.close()

@app.route('/personas', methods=['GET'])
def manage_personas():
    personas = get_all_personas()
    return render_template('personas.html', personas=personas)

@app.route('/personas/create', methods=['POST'])
def create_new_persona():
    try:
        data = request.json
        persona_id = create_persona(
            name=data['name'],
            description=data['description'],
            style=data['style'],
            length=data['length'],
            speech_settings=data['speech_settings'],
            use_emoji=data['use_emoji'],
            use_hashtags=data['use_hashtags'],
            custom_prompt=data.get('custom_prompt', ''),
            mention_user=data.get('mention_user', False)
        )
        if persona_id:
            return jsonify({'success': True, 'id': persona_id})
        else:
            return jsonify({'success': False, 'error': 'Failed to create persona'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/personas/<int:persona_id>', methods=['PUT'])
def update_existing_persona(persona_id):
    try:
        data = request.json
        success = update_persona(
            persona_id=persona_id,
            name=data['name'],
            description=data['description'],
            style=data['style'],
            length=data['length'],
            speech_settings=data['speech_settings'],
            use_emoji=data['use_emoji'],
            use_hashtags=data['use_hashtags'],
            mention_user=data['mention_user'],
            custom_prompt=data.get('custom_prompt')
        )
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to update persona'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/personas/<int:persona_id>', methods=['DELETE'])
def delete_existing_persona(persona_id):
    try:
        success = delete_persona(persona_id)
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to delete persona'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/accounts/<int:account_id>/persona', methods=['POST'])
def update_account_persona(account_id):
    try:
        data = request.json
        print(f"\n[UPDATE_ACCOUNT_PERSONA] Updating account {account_id} with data:")
        print(f"persona_id: {data.get('persona_id')}")
        
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('UPDATE twitter_accounts SET persona_id = ? WHERE id = ?', 
                 (data.get('persona_id'), account_id))
        conn.commit()
        
        # Verify the update
        c.execute('SELECT persona_id FROM twitter_accounts WHERE id = ?', (account_id,))
        result = c.fetchone()
        print(f"[UPDATE_ACCOUNT_PERSONA] Updated account. New persona_id: {result[0] if result else None}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/accounts/<int:account_id>/settings', methods=['POST'])
def update_account_individual_setting(account_id):
    try:
        data = request.json
        setting = data.get('setting')
        value = data.get('value')
        
        if not setting:
            return jsonify({'success': False, 'error': 'Setting name is required'}), 400
            
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Validate the setting name to prevent SQL injection
        valid_settings = [
            'custom_style', 'custom_length', 'custom_speech_settings',
            'custom_use_emoji', 'custom_use_hashtags', 'custom_prompt'
        ]
        
        if setting not in valid_settings:
            return jsonify({'success': False, 'error': 'Invalid setting name'}), 400
            
        c.execute(f'UPDATE twitter_accounts SET {setting} = ? WHERE id = ?', 
                 (value, account_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/export-comments')
def export_comments():
    try:
        account_id = request.args.get('account_id', type=int)
        comments = get_comment_history(account_id)
        
        # Create CSV in memory using StringIO for writing
        output = StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow([
            'Date', 'Account Name', 'Original Tweet Author', 
            'Original Tweet', 'Our Comment', 'Tweet URL', 'Comment URL'
        ])
        
        # Write data
        for comment in comments:
            writer.writerow([
                comment['posted_at'],
                comment['account_name'],
                comment['tweet_author'],
                comment['tweet_text'].replace('\n', ' '),  # Remove newlines in tweet text
                comment['comment_text'].replace('\n', ' '), # Remove newlines in comment text
                comment['tweet_url'],
                comment['comment_url'] or ''
            ])
        
        # Convert to bytes for binary mode
        output_str = output.getvalue()
        bytes_output = BytesIO()
        bytes_output.write(output_str.encode('utf-8-sig'))  # Use UTF-8 with BOM for Excel compatibility
        bytes_output.seek(0)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'comment_history_{timestamp}.csv'
        
        return send_file(
            bytes_output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=filename
        )
        
    except Exception as e:
        print(f"Export error: {str(e)}")  # Add logging
        flash(f'Error exporting comments: {str(e)}', 'error')
        return redirect(url_for('comment_history'))

@app.route('/accounts/<int:account_id>/automation/toggle', methods=['POST'])
def toggle_automation(account_id):
    try:
        data = request.json
        mode = data.get('mode')
        if mode not in ['manual', 'automatic']:
            return jsonify({'success': False, 'error': 'Invalid mode'}), 400
            
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('UPDATE twitter_accounts SET automation_mode = ? WHERE id = ?', 
                 (mode, account_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/accounts/<int:account_id>/automation', methods=['POST'])
def update_automation_settings(account_id):
    try:
        settings = request.json
        print("Received settings:", json.dumps(settings, indent=2))  # Debug log
        
        # Validate settings
        if not isinstance(settings.get('keywords', []), list):
            return jsonify({'success': False, 'error': 'Invalid keywords format'})
            
        if len(settings['keywords']) > 3:
            return jsonify({'success': False, 'error': 'Maximum 3 keywords allowed'})
            
        if len(settings['keywords']) == 0:
            return jsonify({'success': False, 'error': 'At least one keyword is required'})
            
        # Validate persona ID
        automation_persona_id = settings.get('automation_persona_id')
        if automation_persona_id:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            c.execute('SELECT id FROM personas WHERE id = ?', (automation_persona_id,))
            if not c.fetchone():
                return jsonify({'success': False, 'error': 'Invalid persona ID'})
        
        # Get the current automation settings
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('SELECT automation_settings FROM twitter_accounts WHERE id = ?', (account_id,))
        result = c.fetchone()
        current_settings = json.loads(result[0]) if result and result[0] else {}
        print("Current settings:", json.dumps(current_settings, indent=2))  # Debug log
        
        # Handle working schedule
        working_schedule = settings.get('working_schedule', {})
        print("Received working schedule:", json.dumps(working_schedule, indent=2))  # Debug log
        
        # Use the received schedule directly, only fill in missing days with defaults
        default_schedule = {
            'monday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'tuesday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'wednesday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'thursday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'friday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'saturday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'sunday': {'enabled': False, 'start': '09:00', 'end': '17:00'}
        }
        
        # Only use default values for missing days
        final_schedule = {}
        for day in default_schedule:
            if day in working_schedule and isinstance(working_schedule[day], dict):
                final_schedule[day] = {
                    'enabled': bool(working_schedule[day].get('enabled', False)),
                    'start': working_schedule[day].get('start', '09:00'),
                    'end': working_schedule[day].get('end', '17:00')
                }
            else:
                final_schedule[day] = default_schedule[day]
        
        print("Final schedule after merge:", json.dumps(final_schedule, indent=2))  # Debug log
        
        # Update automation settings
        automation_settings = {
            'keywords': settings['keywords'],
            'working_schedule': final_schedule,
            'daily_comment_limit': int(settings.get('daily_comment_limit', 1)),
            'min_likes': int(settings.get('min_likes', 900)),
            'min_retweets': int(settings.get('min_retweets', 0)),
            'last_search_time': current_settings.get('last_search_time'),
            'last_post_time': current_settings.get('last_post_time')
        }
        
        print("Final automation settings:", json.dumps(automation_settings, indent=2))
        
        # Update both automation settings and automation persona in a transaction
        try:
            c.execute('BEGIN TRANSACTION')
            
            # Update automation settings
            c.execute('''
                UPDATE twitter_accounts 
                SET automation_settings = ?
                WHERE id = ?
            ''', (json.dumps(automation_settings), account_id))
            
            # Update automation persona separately to avoid JSON serialization issues
            c.execute('''
                UPDATE twitter_accounts 
                SET automation_persona_id = ?
                WHERE id = ?
            ''', (automation_persona_id, account_id))
            
            c.execute('COMMIT')
            return jsonify({'success': True})
            
        except Exception as e:
            c.execute('ROLLBACK')
            raise e
            
    except Exception as e:
        print(f"Error updating automation settings: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})
    finally:
        if conn:
            conn.close()

def get_account_automation_settings(account_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('SELECT automation_settings FROM twitter_accounts WHERE id = ?', (account_id,))
        result = c.fetchone()
        if result and result[0]:
            return json.loads(result[0])
        return None
    except Exception as e:
        print(f"Error getting automation settings: {e}")
        return None
    finally:
        if conn:
            conn.close()

def is_within_schedule(account_id):
    settings = get_account_automation_settings(account_id)
    if not settings:
        logging.debug(f"No automation settings found for account {account_id}")
        return False
        
    now = datetime.now()
    day_name = now.strftime('%A').lower()
    schedule = settings.get('working_schedule', {}).get(day_name, {})
    
    logging.debug(f"""
    Checking schedule for account {account_id}:
    Current time: {now.strftime('%H:%M')}
    Day: {day_name}
    Schedule enabled: {schedule.get('enabled')}
    Start time: {schedule.get('start')}
    End time: {schedule.get('end')}
    """)
    
    if not schedule.get('enabled'):
        return False
        
    current_time = now.strftime('%H:%M')
    is_scheduled = schedule.get('start', '00:00') <= current_time <= schedule.get('end', '23:59')
    
    logging.debug(f"Is within schedule: {is_scheduled}")
    return is_scheduled

def create_notification(account_id, type, message, retry_time=None):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('''
            INSERT INTO automation_notifications 
            (account_id, type, message, retry_time) 
            VALUES (?, ?, ?, ?)
        ''', (account_id, type, message, retry_time))
        conn.commit()
    except Exception as e:
        print(f"Error creating notification: {e}")
    finally:
        if conn:
            conn.close()

@app.route('/accounts/<int:account_id>/notifications', methods=['GET'])
def get_notifications(account_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('''
            SELECT id, type, message, retry_time, created_at, is_read 
            FROM automation_notifications 
            WHERE account_id = ? 
            ORDER BY created_at DESC
        ''', (account_id,))
        notifications = [{
            'id': row[0],
            'type': row[1],
            'message': row[2],
            'retry_time': row[3],
            'created_at': row[4],
            'is_read': bool(row[5])
        } for row in c.fetchall()]
        return jsonify(notifications)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/accounts/<int:account_id>/notifications/<int:notification_id>/read', methods=['POST'])
def mark_notification_read(account_id, notification_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        c.execute('''
            UPDATE automation_notifications 
            SET is_read = 1 
            WHERE id = ? AND account_id = ?
        ''', (notification_id, account_id))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

def initialize_automation_settings(account_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Create default working schedule for all days
        default_schedule = {
            'monday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'tuesday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'wednesday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'thursday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'friday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'saturday': {'enabled': False, 'start': '09:00', 'end': '17:00'},
            'sunday': {'enabled': False, 'start': '09:00', 'end': '17:00'}
        }
        
        default_settings = {
            'keywords': [],
            'working_schedule': default_schedule,
            'daily_comment_limit': 1,
            'min_likes': 900,
            'min_retweets': 0,
            'last_search_time': None,
            'last_post_time': None
        }
        
        # Update the account with default settings
        c.execute('''
            UPDATE twitter_accounts 
            SET automation_settings = ?, 
                automation_mode = 'manual'
            WHERE id = ?
        ''', (json.dumps(default_settings), account_id))
        
        conn.commit()
        return True
    except Exception as e:
        print(f"Error initializing automation settings: {e}")
        return False
    finally:
        if conn:
            conn.close()

@app.route('/scheduled-posts')
def scheduled_posts():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Get all scheduled RSS posts
        c.execute('''
            SELECT ri.id, ri.title, ri.tweet_text, ri.published_at, ri.created_at,
                   ta.account_name, rf.name as feed_name, p.name as persona_name,
                   rc.schedule_settings
            FROM rss_items ri
            JOIN rss_posting_configs rc ON ri.config_id = rc.id
            JOIN twitter_accounts ta ON rc.account_id = ta.id
            JOIN rss_feeds rf ON rc.feed_id = rf.id
            JOIN personas p ON rc.persona_id = p.id
            WHERE ri.processed = 1 AND ri.posted = 0
            ORDER BY ri.published_at DESC
        ''')
        
        posts = []
        for row in c.fetchall():
            schedule = json.loads(row[8])
            posts.append({
                'id': row[0],
                'title': row[1],
                'tweet_text': row[2],
                'published_at': datetime.fromisoformat(row[3]),
                'created_at': datetime.fromisoformat(row[4]),
                'account': row[5],
                'feed': row[6],
                'persona': row[7],
                'schedule': schedule
            })
            
        return render_template('scheduled_posts.html', posts=posts)
        
    except Exception as e:
        return str(e), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/scheduled-post/<int:post_id>', methods=['DELETE'])
def delete_scheduled_post(post_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        c.execute('DELETE FROM rss_items WHERE id = ?', (post_id,))
        conn.commit()
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/automations')
def automations():
    return render_template('automations.html')

@app.route('/radar')
def radar():
    return render_template('radar.html')

@app.route('/messages')
def messages():
    return render_template('messages.html')

@app.route('/analytics')
def analytics():
    return render_template('analytics.html')

@app.route('/ai_comments')
def ai_comments():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Get all accounts with their AI status and selected personas
        c.execute('''
            SELECT 
                ta.id,
                ta.account_name,
                ta.automation_mode,
                ta.automation_persona_id,
                p.name as persona_name,
                p.description as persona_description
            FROM twitter_accounts ta
            LEFT JOIN personas p ON ta.automation_persona_id = p.id
        ''')
        
        accounts = []
        for row in c.fetchall():
            account = {
                'id': row[0],
                'account_name': row[1],
                'ai_enabled': row[2] == 'automatic',
                'automation_persona_id': row[3],
                'selected_persona': {
                    'name': row[4],
                    'description': row[5]
                } if row[4] else None,
                'stats': get_comments_count(row[0])
            }
            accounts.append(account)
        
        # Get all available personas
        c.execute('''
            SELECT id, name, custom_prompt, style, length, 
                   use_emoji, use_hashtags, mention_user
            FROM personas
        ''')
        personas = [
            {
                'id': row[0],
                'name': row[1],
                'custom_prompt': row[2],
                'style': row[3],
                'length': row[4],
                'use_emoji': row[5],
                'use_hashtags': row[6],
                'mention_user': row[7]
            }
            for row in c.fetchall()
        ]
        
        return render_template('ai_comments.html', accounts=accounts, personas=personas)
    
    except Error as e:
        flash(f"Database error: {e}", "error")
        return redirect(url_for('index'))
    finally:
        if conn:
            conn.close()

@app.route('/api/toggle_ai', methods=['POST'])
def toggle_ai():
    try:
        account_id = request.json.get('account_id')
        enabled = request.json.get('enabled')
        
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        c.execute('''
            UPDATE twitter_accounts 
            SET automation_mode = ?
            WHERE id = ?
        ''', ('automatic' if enabled else 'manual', account_id))
        
        conn.commit()
        return jsonify({'success': True})
        
    except Error as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/select_persona', methods=['POST'])
def select_persona():
    try:
        account_id = request.json.get('account_id')
        persona_id = request.json.get('persona_id')
        
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        c.execute('''
            UPDATE twitter_accounts 
            SET automation_persona_id = ?
            WHERE id = ?
        ''', (persona_id, account_id))
        
        conn.commit()
        return jsonify({'success': True})
        
    except Error as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/debug/db')
def debug_db():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Get all tables
        c.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = c.fetchall()
        
        result = {}
        
        # For each table, get its structure and some sample data
        for table in tables:
            table_name = table[0]
            c.execute(f"SELECT * FROM {table_name} LIMIT 1")
            columns = [description[0] for description in c.description]
            result[table_name] = {
                'columns': columns,
                'sample': c.fetchone()
            }
            
        return jsonify(result)
        
    except Error as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/get_full_prompt/<int:persona_id>')
def get_full_prompt(persona_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        c.execute('SELECT custom_prompt FROM personas WHERE id = ?', (persona_id,))
        result = c.fetchone()
        
        if result:
            return jsonify({
                'success': True,
                'custom_prompt': result[0]
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Persona not found'
            }), 404
            
    except Error as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
    finally:
        if conn:
            conn.close()

@app.route('/manual_comments')  # Change from manual-comments to manual_comments
def manual_comments():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Get active account and all accounts (same as before)
        c.execute('''
            SELECT 
                ta.id,
                ta.account_name,
                ta.is_active,
                (SELECT COUNT(*) FROM posted_comments pc WHERE pc.account_id = ta.id AND DATE(pc.posted_at) = DATE('now')) as today_count,
                (SELECT COUNT(*) FROM posted_comments pc WHERE pc.account_id = ta.id) as total_count
            FROM twitter_accounts ta
        ''')
        
        accounts = []
        active_account = None
        comments_count = {'today': 0, 'total': 0}
        
        for row in c.fetchall():
            account = {
                'id': row[0],
                'account_name': row[1],
                'is_active': bool(row[2]),
                'comment_counts': {
                    'today': row[3],
                    'total': row[4]
                }
            }
            accounts.append(account)
            
            if account['is_active']:
                active_account = account
                comments_count = account['comment_counts']
        
        return render_template('manual_comments.html', 
                             active_account=active_account,
                             accounts=accounts,
                             comments_count=comments_count)
    
    except Error as e:
        flash(f"Database error: {e}", "error")
        return redirect(url_for('index'))
    finally:
        if conn:
            conn.close()

@app.route('/feed')
def feed():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Get accounts
        c.execute('SELECT id, account_name FROM twitter_accounts')
        accounts = [{'id': row[0], 'account_name': row[1]} for row in c.fetchall()]
        
        # Get feeds
        c.execute('SELECT id, name, url FROM rss_feeds')
        feeds = [{'id': row[0], 'name': row[1], 'url': row[2]} for row in c.fetchall()]
        
        # Get complete persona data
        c.execute('''
            SELECT id, name, style, length, use_emoji, use_hashtags, 
                   mention_user, custom_prompt 
            FROM personas
        ''')
        personas = []
        for row in c.fetchall():
            personas.append({
                'id': row[0],
                'name': row[1],
                'style': row[2],
                'length': row[3],
                'use_emoji': bool(row[4]),
                'use_hashtags': bool(row[5]),
                'mention_user': bool(row[6]),
                'custom_prompt': row[7]
            })
        
        # Get RSS configs with debug logging
        c.execute('''
            SELECT rc.id, rc.account_id, rc.feed_id, rc.persona_id, 
                   rc.is_active, rc.schedule_settings,
                   ta.account_name, rf.name as feed_name, rf.url as feed_url,
                   p.name as persona_name
            FROM rss_posting_configs rc
            JOIN twitter_accounts ta ON rc.account_id = ta.id
            JOIN rss_feeds rf ON rc.feed_id = rf.id
            JOIN personas p ON rc.persona_id = p.id
        ''')
        configs = []
        for row in c.fetchall():
            config = {
                'id': row[0],
                'account_id': row[1],
                'feed_id': row[2],
                'persona_id': row[3],
                'is_active': row[4],
                'schedule_settings': row[5],
                'account_name': row[6],
                'feed_name': row[7],
                'feed_url': row[8],
                'persona_name': row[9]
            }
            configs.append(config)
            
        print("Loaded configs:", configs)  # Debug log
        
        return render_template('feed.html', 
                             accounts=accounts, 
                             feeds=feeds, 
                             personas=personas,
                             configs=configs)
    except Exception as e:
        return str(e), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/rss/feeds', methods=['GET', 'POST'])
def manage_rss_feeds():
    if request.method == 'POST':
        try:
            data = request.json
            url = data.get('url')
            name = data.get('name')
            
            # Validate RSS feed
            feed = feedparser.parse(url)
            if feed.bozo:
                return jsonify({'error': 'Invalid RSS feed URL'}), 400
                
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            
            c.execute('''
                INSERT INTO rss_feeds (name, url, description)
                VALUES (?, ?, ?)
            ''', (name, url, data.get('description')))
            
            feed_id = c.lastrowid
            conn.commit()
            
            return jsonify({
                'id': feed_id,
                'name': name,
                'url': url
            })
            
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            if conn:
                conn.close()
    else:
        # GET method - return all feeds
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            c.execute('SELECT id, name, url FROM rss_feeds')
            feeds = [{'id': row[0], 'name': row[1], 'url': row[2]} 
                    for row in c.fetchall()]
            return jsonify(feeds)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
        finally:
            if conn:
                conn.close()

@app.route('/api/rss/config', methods=['POST'])
def create_rss_config():
    try:
        data = request.json
        
        # Ensure schedule_settings has the correct structure
        if 'schedule_settings' not in data:
            data['schedule_settings'] = {
                'days': {
                    'Mon': {'enabled': False, 'start': '09:00', 'end': '17:00'},
                    'Tue': {'enabled': False, 'start': '09:00', 'end': '17:00'},
                    'Wed': {'enabled': False, 'start': '09:00', 'end': '17:00'},
                    'Thu': {'enabled': False, 'start': '09:00', 'end': '17:00'},
                    'Fri': {'enabled': False, 'start': '09:00', 'end': '17:00'},
                    'Sat': {'enabled': False, 'start': '09:00', 'end': '17:00'},
                    'Sun': {'enabled': False, 'start': '09:00', 'end': '17:00'}
                },
                'posts_per_day': data.get('posts_per_day', 1)
            }
        
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Insert the configuration
        c.execute('''
            INSERT INTO rss_posting_configs 
            (account_id, feed_id, persona_id, posts_per_day, schedule_settings, is_active)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            data['account_id'],
            data['feed_id'],
            data['persona_id'],
            data['posts_per_day'],
            json.dumps(data['schedule_settings']),
            True  # Default to active
        ))
        
        config_id = c.lastrowid
        conn.commit()
        
        # Verify the insertion
        c.execute('''
            SELECT rc.*, ta.account_name, rf.name as feed_name, p.name as persona_name
            FROM rss_posting_configs rc
            JOIN twitter_accounts ta ON rc.account_id = ta.id
            JOIN rss_feeds rf ON rc.feed_id = rf.id
            JOIN personas p ON rc.persona_id = p.id
            WHERE rc.id = ?
        ''', (config_id,))
        
        row = c.fetchone()
        print("Inserted config:", row)  # Debug log
        
        return jsonify({
            'success': True,
            'id': config_id,
            'message': 'Configuration created successfully'
        })
        
    except Exception as e:
        print("Error creating config:", str(e))  # Debug log
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500
        
    finally:
        if conn:
            conn.close()

def format_schedule_summary(schedule):
    if not schedule:
        return "Not configured"
    
    days = len(schedule.get('days', []))
    times = len(schedule.get('times', []))
    posts = schedule.get('posts_per_day', 0)
    
    return f"{posts} posts/day on {days} days at {times} different times"

@app.route('/api/rss/config/<int:config_id>/toggle', methods=['POST'])
def toggle_rss_config(config_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Toggle is_active
        c.execute('''
            UPDATE rss_posting_configs 
            SET is_active = NOT is_active 
            WHERE id = ?
        ''', (config_id,))
        
        conn.commit()
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/rss/config/<int:config_id>', methods=['DELETE'])
def delete_rss_config(config_id):
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        c.execute('DELETE FROM rss_posting_configs WHERE id = ?', (config_id,))
        conn.commit()
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

with app.app_context():
    @app.before_request
    def init_scheduler():
        if not hasattr(app, '_scheduler_started'):
            worker.schedule_all()
            app._scheduler_started = True

@app.route('/api/account/<int:account_id>/rss-settings', methods=['PUT'])
def update_rss_settings(account_id):
    try:
        data = request.json
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        c.execute('''
            UPDATE twitter_accounts 
            SET rss_settings = ? 
            WHERE id = ?
        ''', (json.dumps(data), account_id))
        
        conn.commit()
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/rss/process/<int:config_id>', methods=['POST'])
def trigger_rss_processing(config_id):
    """Manually trigger RSS processing for testing"""
    try:
        worker.process_config(config_id)
        return jsonify({'success': True, 'message': 'Processing triggered'})
    except Exception as e:
        logger.error(f"Error triggering RSS process: {str(e)}")
        return jsonify({'error': str(e)}), 500

# After creating the scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Add this to check scheduler status
@app.route('/api/scheduler/status')
def scheduler_status():
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            'id': job.id,
            'next_run': job.next_run_time,
            'function': job.func.__name__,
            'args': job.args
        })
    
    return jsonify({
        'running': scheduler.running,
        'jobs': jobs
    })

@app.route('/api/rss/test/<int:config_id>', methods=['POST'])
def test_rss_processing(config_id):
    """Test RSS processing immediately"""
    conn = None
    try:
        logger.info(f"Manual test triggered for config {config_id}")
        
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Updated SQL query to use dictionary factory
        c.row_factory = sqlite3.Row
        
        # Get full config details
        c.execute('''
            SELECT 
                rc.*,
                rf.url as feed_url,
                rf.name as feed_name,
                p.name as persona_name,
                ta.account_name,
                ta.rss_settings,
                ta.id as account_id
            FROM rss_posting_configs rc
            JOIN twitter_accounts ta ON rc.account_id = ta.id
            JOIN rss_feeds rf ON rc.feed_id = rf.id
            JOIN personas p ON rc.persona_id = p.id
            WHERE rc.id = ?
        ''', (config_id,))
        
        config = c.fetchone()
        if not config:
            logger.warning(f"Config {config_id} not found")
            return jsonify({'error': 'Config not found'}), 404
            
        logger.info(f"Testing config: {dict(config)}")  # Convert Row to dict for logging
        
        # Process immediately
        worker.process_config(config_id)
        
        return jsonify({
            'success': True,
            'message': 'RSS processing test triggered',
            'config': {
                'id': config['id'],
                'feed': config['feed_name'],
                'account': config['account_name'],
                'persona': config['persona_name'],
                'schedule': json.loads(config['schedule_settings'])
            }
        })
        
    except Exception as e:
        logger.error(f"Error in test processing: {str(e)}")
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    app.run(debug=True, port=5001) 