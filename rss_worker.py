import feedparser
import tweepy
import sqlite3
import json
import random
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from openai import OpenAI
import os
import logging
import time

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.start()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('rss_worker.log'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('RSSWorker')

class RSSWorker:
    def __init__(self, db_path='twitter_accounts.db'):
        self.db_path = db_path
        self.client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        
    def fetch_rss_items(self, feed_url, max_age=None, min_age=None):
        """Fetch items from RSS feed with age filters"""
        try:
            feed = feedparser.parse(feed_url)
            items = []
            now = datetime.now()
            
            for entry in feed.entries:
                pub_date = datetime(*entry.published_parsed[:6])
                
                # Apply age filters
                if max_age:
                    max_delta = self.parse_time_duration(max_age)
                    if now - pub_date > max_delta:
                        continue
                        
                if min_age:
                    min_delta = self.parse_time_duration(min_age)
                    if now - pub_date < min_delta:
                        continue
                        
                items.append({
                    'title': entry.title,
                    'link': entry.link,
                    'description': entry.get('description', ''),
                    'published': pub_date
                })
                
            return items
        except Exception as e:
            print(f"Error fetching RSS feed {feed_url}: {str(e)}")
            return []

    def get_system_prompt(self, persona_id):
        """Get system prompt based on persona settings"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            c.execute('''
                SELECT name, description, style, length, 
                       use_emoji, use_hashtags, custom_prompt
                FROM personas 
                WHERE id = ?
            ''', (persona_id,))
            
            persona = c.fetchone()
            if not persona:
                return None
                
            name, description, style, length, use_emoji, use_hashtags, custom_prompt = persona
            
            style_descriptions = {
                'neutral': 'maintain a balanced and objective tone',
                'casual': 'use a relaxed and informal tone',
                'official': 'maintain a formal and professional tone',
                'ironic': 'use irony and wit',
                'sarcastic': 'employ heavy sarcasm and humor',
                'gen-z': 'use contemporary, youthful language'
            }
            
            length_descriptions = {
                'short': 'keep tweets very brief (30-50 characters)',
                'medium': 'write moderate length tweets (50-120 characters)',
                'long': 'provide detailed tweets (120-250 characters)'
            }
            
            prompt = f"""You are a social media manager who specializes in sharing news about {description}.
Your tweets should {style_descriptions.get(style, 'maintain a neutral tone')}, {length_descriptions.get(length, 'write moderate length tweets')}.

CRITICAL RULES:
1. Write tweets that are engaging and informative
2. Keep the tone consistent with the specified style
3. {"Include relevant emojis" if use_emoji else "DO NOT use emojis"}
4. {"Add relevant hashtags" if use_hashtags else "DO NOT use hashtags"}
5. NEVER truncate or add ellipsis (...)
6. Leave room for the URL at the end
"""
            
            if custom_prompt:
                prompt += f"\nAdditional instructions:\n{custom_prompt}"
                
            return prompt
            
        except Exception as e:
            print(f"Error getting system prompt: {str(e)}")
            return None
        finally:
            if conn:
                conn.close()

    def generate_tweet(self, item, persona_id):
        """Generate tweet using GPT-4"""
        try:
            system_prompt = self.get_system_prompt(persona_id)
            if not system_prompt:
                raise Exception("No persona settings found")
                
            user_prompt = f"""Based on this news article:
Title: {item['title']}
Description: {item['description']}

Generate an engaging tweet that summarizes the key information.
Remember to:
1. Be concise but informative
2. Make it engaging and shareable
3. Leave room for the URL
4. Keep it under 250 characters"""

            response = self.client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=100
            )
            
            tweet = response.choices[0].message.content.strip()
            
            # Add the URL
            tweet = f"{tweet}\n{item['link']}"
            
            return tweet
            
        except Exception as e:
            print(f"Error generating tweet: {str(e)}")
            return None

    def post_tweet(self, account_id, tweet_text):
        """Post tweet using account credentials"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            # Get account credentials
            c.execute('''
                SELECT consumer_key, consumer_secret, 
                       access_token, access_token_secret 
                FROM twitter_accounts 
                WHERE id = ?
            ''', (account_id,))
            
            creds = c.fetchone()
            if not creds:
                raise Exception("Account not found")
                
            # Initialize Twitter client
            auth = tweepy.OAuthHandler(creds[0], creds[1])
            auth.set_access_token(creds[2], creds[3])
            api = tweepy.API(auth)
            
            # Post tweet
            api.update_status(tweet_text)
            return True
            
        except Exception as e:
            logger.error(f"Error posting tweet: {str(e)}")
            return False
        finally:
            if conn:
                conn.close()

    def post_with_retry(self, account_id, tweet_text, max_retries=3):
        """Post tweet with retry logic"""
        for attempt in range(max_retries):
            try:
                success = self.post_tweet(account_id, tweet_text)
                if success:
                    logger.info(f"Tweet posted successfully for account {account_id}")
                    return True
                
                logger.warning(f"Failed to post tweet (attempt {attempt + 1}/{max_retries})")
                time.sleep(60)  # Wait 1 minute before retry
                
            except Exception as e:
                logger.error(f"Error posting tweet (attempt {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(60)
                else:
                    raise
        
        return False

    def update_post_status(self, item_id, status, error=None):
        """Update RSS item status in database"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            c.execute('''
                UPDATE rss_items 
                SET posted = ?, 
                    error = ?,
                    posted_at = CURRENT_TIMESTAMP 
                WHERE id = ?
            ''', (status, error, item_id))
            
            conn.commit()
        except Exception as e:
            logger.error(f"Error updating post status: {str(e)}")
        finally:
            if conn:
                conn.close()

    def process_config(self, config_id):
        """Process RSS feed and generate/post tweets"""
        try:
            logger.info(f"Starting to process config {config_id}")
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            # Get config details
            c.execute('''
                SELECT rc.*, rf.url as feed_url, p.id as persona_id,
                       ta.rss_settings, rc.posts_per_day
                FROM rss_posting_configs rc
                JOIN rss_feeds rf ON rc.feed_id = rf.id
                JOIN personas p ON rc.persona_id = p.id
                JOIN twitter_accounts ta ON rc.account_id = ta.id
                WHERE rc.id = ? AND rc.is_active = 1
            ''', (config_id,))
            
            config = c.fetchone()
            logger.info(f"Config found: {config}")
            
            if not config:
                logger.warning(f"No active config found for id {config_id}")
                return
                
            # Fetch RSS items
            items = self.fetch_rss_items(config['feed_url'])
            logger.info(f"Fetched {len(items)} items from feed")
            
            # Process items...
            for item in items:
                logger.info(f"Processing item: {item['title']}")
                # Rest of the processing...
            
            # Apply account-specific rules and limits
            if config['avoid_duplicates']:
                # Filter duplicates...
                items = self.filter_duplicates(items, config_id)
            
            # Limit items per feed (buffer limit)
            items = items[:config['max_posts_per_feed']]
            
            # Store unprocessed items
            stored_count = 0
            for item in items:
                # Check if we've stored enough items for future posts
                if stored_count >= config['posts_per_day'] * 3:  # Store 3 days worth of posts
                    break
                    
                # Check if item already exists
                c.execute('SELECT id FROM rss_items WHERE link = ? AND config_id = ?', 
                         (item['link'], config_id))
                if c.fetchone():
                    continue
                    
                # Store new item
                c.execute('''
                    INSERT INTO rss_items (config_id, title, link, description, published_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (config_id, item['title'], item['link'], item['description'], 
                      item['published'].isoformat()))
                conn.commit()
                stored_count += 1
            
            # Generate tweets for today's unprocessed items
            c.execute('''
                SELECT id, title, link, description 
                FROM rss_items 
                WHERE config_id = ? AND processed = 0
                LIMIT ?
            ''', (config_id, config['posts_per_day']))  # Only process today's quota
            
            unprocessed = c.fetchall()
            for item in unprocessed:
                tweet_text = self.generate_tweet(item, config['persona_id'])  # Not async anymore
                if tweet_text:
                    c.execute('''
                        UPDATE rss_items 
                        SET processed = 1, tweet_text = ? 
                        WHERE id = ?
                    ''', (tweet_text, item[0]))
                    conn.commit()
            
            # Post scheduled tweets
            schedule = json.loads(config['schedule_settings'])
            current_time = datetime.now()
            
            # Check if current time is within scheduled window
            day_name = current_time.strftime('%a')
            if day_name in schedule['days']:
                day_schedule = schedule['days'][day_name]
                if day_schedule['enabled']:
                    start_time = datetime.strptime(day_schedule['start'], '%H:%M').time()
                    end_time = datetime.strptime(day_schedule['end'], '%H:%M').time()
                    current_time = current_time.time()
                    
                    if start_time <= current_time <= end_time:
                        # Get unposted tweets
                        c.execute('''
                            SELECT id, tweet_text 
                            FROM rss_items 
                            WHERE config_id = ? AND processed = 1 AND posted = 0
                            ORDER BY published_at ASC
                            LIMIT 1
                        ''', (config_id,))
                        
                        tweet = c.fetchone()
                        if tweet:
                            success = self.post_with_retry(config['account_id'], tweet[1])
                            if success:
                                c.execute('UPDATE rss_items SET posted = 1 WHERE id = ?', 
                                        (tweet[0],))
                                conn.commit()
            
            logger.info(f"Processing config {config_id}")
        except Exception as e:
            print(f"Error processing RSS config {config_id}: {str(e)}")
            logger.error(f"Error in process_config: {str(e)}")
        finally:
            if conn:
                conn.close()

    def schedule_all(self):
        """Schedule RSS processing for all active configurations"""
        try:
            logger.info("Starting to schedule RSS jobs")
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            # Get all active configs
            c.execute('''
                SELECT id, schedule_settings 
                FROM rss_posting_configs 
                WHERE is_active = 1
            ''')
            configs = c.fetchall()
            logger.info(f"Found {len(configs)} active configs")
            
            # Clear existing jobs
            scheduler.remove_all_jobs()
            
            for config_id, schedule_settings in configs:
                settings = json.loads(schedule_settings)
                logger.info(f"Scheduling config {config_id} with settings: {settings}")
                
                # Schedule for each enabled day
                for day, times in settings['days'].items():
                    if times['enabled']:
                        day_num = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].index(day)
                        start_time = datetime.strptime(times['start'], '%H:%M').time()
                        end_time = datetime.strptime(times['end'], '%H:%M').time()
                        
                        # Schedule job
                        job = scheduler.add_job(
                            self.process_config,
                            'cron',
                            args=[config_id],
                            day_of_week=day_num,
                            hour=start_time.hour,
                            minute=start_time.minute
                        )
                        logger.info(f"Added job {job.id} for config {config_id} on {day}")
            
            logger.info("Finished scheduling RSS jobs")
            
        except Exception as e:
            logger.error(f"Error scheduling RSS jobs: {str(e)}")
        finally:
            if conn:
                conn.close()

    def is_similar_content(self, text1, text2):
        """Check if two pieces of content are similar"""
        # Simple similarity check based on common words
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        common_words = words1.intersection(words2)
        return len(common_words) / max(len(words1), len(words2)) > 0.6

    def parse_time_duration(self, duration):
        """Convert time duration string to timedelta"""
        unit = duration[-1].lower()
        value = int(duration[:-1])
        if unit == 'h':
            return timedelta(hours=value)
        elif unit == 'd':
            return timedelta(days=value)
        elif unit == 'm':
            return timedelta(minutes=value)
        raise ValueError(f"Invalid time duration: {duration}")

    def filter_duplicates(self, items, config_id):
        """Filter out items similar to recently posted ones"""
        try:
            conn = sqlite3.connect(self.db_path)
            c = conn.cursor()
            
            # Get recent posts
            c.execute('''
                SELECT title, tweet_text 
                FROM rss_items 
                WHERE config_id = ? AND posted = 1 
                AND created_at > datetime('now', '-7 days')
            ''', (config_id,))
            
            recent_posts = c.fetchall()
            recent_titles = [post[0] for post in recent_posts]
            
            # Filter items
            filtered_items = []
            for item in items:
                if not any(self.is_similar_content(item['title'], title) 
                          for title in recent_titles):
                    filtered_items.append(item)
                    
            return filtered_items
            
        except Exception as e:
            logger.error(f"Error filtering duplicates: {str(e)}")
            return items
        finally:
            if conn:
                conn.close()

# Create a global instance
worker = RSSWorker() 