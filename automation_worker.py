import sqlite3
from sqlite3 import Error
import json
import time
import random
from datetime import datetime, timedelta
from apify_client import ApifyClient
from openai import OpenAI
import os
from dotenv import load_dotenv
from twitter.twitter_client import TwitterClient
import logging
import concurrent.futures

load_dotenv()

class AutomationWorker:
    def __init__(self):
        self.apify_client = ApifyClient(os.getenv('APIFY_API_TOKEN'))
        self.openai_client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
        self.min_delay = 120  # 2 minutes
        self.max_delay = 180  # 3 minutes
        self.batch_size = 5
        self.min_search_interval = 55  # minutes
        self.max_search_interval = 90  # minutes
        self.min_quality_score = 4.0
        self.retry_attempts = 3
        
    def get_active_automated_accounts(self):
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            c.execute('''
                SELECT id, account_name, consumer_key, consumer_secret, 
                       access_token, access_token_secret, automation_settings,
                       automation_persona_id
                FROM twitter_accounts 
                WHERE automation_mode = 'automatic'
            ''')
            accounts = []
            for row in c.fetchall():
                account = {
                    'id': row[0],
                    'account_name': row[1],
                    'consumer_key': row[2],
                    'consumer_secret': row[3],
                    'access_token': row[4],
                    'access_token_secret': row[5],
                    'automation_settings': json.loads(row[6]) if row[6] else {},
                    'automation_persona_id': row[7]
                }
                accounts.append(account)
            return accounts
        except Error as e:
            print(f"Database error: {e}")
            return []
        finally:
            if conn:
                conn.close()
                
    def is_within_working_hours(self, account):
        settings = account['automation_settings']
        logging.debug(f"Checking working hours for account {account['account_name']} with settings: {settings}")
        
        if 'working_schedule' not in settings:
            logging.error(f"Missing 'working_schedule' in automation_settings for account {account['account_name']}")
            return False
        
        now = datetime.now()
        day_name = now.strftime('%A').lower()
        schedule = settings['working_schedule'].get(day_name, {})
        
        if not schedule.get('enabled'):
            return False
            
        current_time = now.strftime('%H:%M')
        return schedule['start'] <= current_time <= schedule['end']
        
    def get_daily_comment_count(self, account_id):
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            today = datetime.now().strftime('%Y-%m-%d')
            c.execute('''
                SELECT COUNT(*) FROM posted_comments 
                WHERE account_id = ? AND date(posted_at) = ?
            ''', (account_id, today))
            return c.fetchone()[0]
        except Error as e:
            print(f"Database error: {e}")
            return 0
        finally:
            if conn:
                conn.close()
                
    def reached_daily_limit(self, account):
        daily_count = self.get_daily_comment_count(account['id'])
        return daily_count >= account['automation_settings']['daily_comment_limit']
        
    def get_commented_tweet_ids(self, account_id, days=7):
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            c.execute('''
                SELECT tweet_id FROM commented_tweets 
                WHERE account_id = ? AND date(commented_at) > ?
            ''', (account_id, cutoff_date))
            return {row[0] for row in c.fetchall()}
        except Error as e:
            print(f"Database error: {e}")
            return set()
        finally:
            if conn:
                conn.close()
                
    def get_tweets(self, keywords, min_likes=900):
        """
        Fetch tweets from Apify with specified keywords and minimum likes.
        Limited to 10 tweets per request.
        """
        try:
            # Calculate date range - last 48 hours from current time
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=48)
            
            # Format dates in the correct format
            since_time = start_time.strftime('%Y-%m-%d_%H:%M:%S_UTC')
            until_time = end_time.strftime('%Y-%m-%d_%H:%M:%S_UTC')
            
            # Create the Apify actor run
            run_input = {
                "searchTerms": keywords,
                "maxItems": 3,
                "queryType": "Top",
                "lang": "en",
                "since": since_time,
                "until": until_time,
                "filter:verified": False,
                "filter:blue_verified": False,
                "filter:nativeretweets": False,
                "include:nativeretweets": False,
                "filter:replies": False,
                "filter:quote": False,
                "filter:has_engagement": True,
                "filter:media": False,
                "filter:twimg": False,
                "filter:images": False,
                "filter:videos": False,
                "filter:native_video": False,
                "filter:vine": False,
                "filter:consumer_video": False,
                "filter:pro_video": False,
                "filter:spaces": False,
                "filter:links": False,
                "filter:mentions": False,
                "filter:news": False,
                "filter:safe": False,
                "filter:hashtags": False,
                "min_faves": min_likes,
                "min_retweets": 0,
                "min_replies": 0,
                "-min_faves": 0,
                "-min_retweets": 0,
                "-min_replies": 0
            }
            
            logging.debug(f"Initial run_input: {json.dumps(run_input, indent=2)}")
            
            # Use the correct actor ID
            run = self.apify_client.actor("CJdippxWmn9uRfooo").call(run_input=run_input)
            
            # Fetch the results
            dataset_items = self.apify_client.dataset(run["defaultDatasetId"]).list_items().items
            
            logging.info(f"Retrieved {len(dataset_items)} tweets from Apify")
            
            # Transform and validate tweet structure
            valid_tweets = []
            for tweet in dataset_items:
                try:
                    # Skip mock tweets
                    if tweet.get('type') == 'mock_tweet':
                        logging.debug(f"Skipping mock tweet")
                        continue
                        
                    # Log the raw tweet for debugging
                    logging.debug(f"Processing tweet: {json.dumps(tweet, indent=2)}")
                    
                    # Basic structure validation
                    if not isinstance(tweet, dict):
                        logging.error(f"Invalid tweet format: {tweet}")
                        continue

                    # Check for required fields
                    required_fields = ['id', 'text', 'author', 'retweetCount', 'likeCount']
                    missing_fields = [field for field in required_fields if field not in tweet]
                    if missing_fields:
                        logging.error(f"Tweet {tweet.get('id', 'unknown')} missing required fields: {missing_fields}")
                        continue
                        
                    # Validate author structure
                    if not isinstance(tweet['author'], dict):
                        logging.error(f"Invalid author format for tweet {tweet['id']}")
                        continue

                    # Check engagement metrics with detailed logging
                    like_count = int(tweet.get('likeCount', 0))
                    if like_count < min_likes:
                        logging.debug(f"Tweet {tweet['id']} has insufficient likes: {like_count} < {min_likes}")
                        continue
                    
                    # Parse and validate tweet date
                    try:
                        tweet_date = datetime.strptime(tweet['createdAt'], '%a %b %d %H:%M:%S +0000 %Y')
                        if not (start_time <= tweet_date <= end_time):
                            logging.debug(f"Tweet {tweet['id']} outside time range: {tweet_date}")
                            continue
                    except (ValueError, KeyError) as e:
                        logging.error(f"Error parsing date for tweet {tweet['id']}: {e}")
                        continue

                    # Create a cleaned tweet object with only the fields we need
                    cleaned_tweet = {
                        'id': tweet['id'],
                        'text': tweet['text'],
                        'author': {
                            'name': tweet['author'].get('name', ''),
                            'userName': tweet['author'].get('userName', ''),
                            'profilePicture': tweet['author'].get('profilePicture', ''),
                            'isVerified': tweet['author'].get('isVerified', False),
                            'isBlueVerified': tweet['author'].get('isBlueVerified', False)
                        },
                        'likeCount': like_count,
                        'retweetCount': int(tweet.get('retweetCount', 0)),
                        'replyCount': int(tweet.get('replyCount', 0)),
                        'bookmarkCount': int(tweet.get('bookmarkCount', 0)),
                        'createdAt': tweet['createdAt'],
                        'url': tweet.get('url', f"https://twitter.com/i/web/status/{tweet['id']}"),
                        'extendedEntities': tweet.get('extendedEntities', {}),
                        'isReply': tweet.get('isReply', False),
                        'lang': tweet.get('lang', 'en')
                    }
                    
                    valid_tweets.append(cleaned_tweet)
                    logging.info(f"Valid tweet found: ID={tweet['id']}, Likes={like_count}, Lang={tweet.get('lang')}, IsReply={tweet.get('isReply')}, Text={tweet['text'][:100]}...")
                    
                except Exception as e:
                    logging.error(f"Error processing tweet: {e}")
                    logging.error(f"Problem tweet data: {json.dumps(tweet, indent=2)}")
                    continue
            
            # Sort tweets by creation date (most recent first)
            valid_tweets.sort(key=lambda x: datetime.strptime(x['createdAt'], '%a %b %d %H:%M:%S +0000 %Y'), reverse=True)
            
            logging.info(f"Found {len(valid_tweets)} valid tweets out of {len(dataset_items)} total tweets")
            return valid_tweets
            
        except Exception as e:
            logging.error(f"Error fetching tweets from Apify: {e}")
            logging.exception("Full error trace:")
            return []
            
    def evaluate_tweet_quality(self, tweet_text):
        try:
            messages = [
                {"role": "system", "content": "You are a tweet quality evaluator. Rate tweets on a scale of 1-5 based on their potential for meaningful engagement. Consider factors like content quality, topic relevance, and discussion potential. Provide a brief reason for your rating."},
                {"role": "user", "content": f"""Please evaluate the following tweet and assign a score from 1 to 5 based on these criteria:

1 - Illegal, abusive, advertisements, meaningless, or too short to understand context
2 - Likely AI generated or spam
3 - Legitimate but generic or lacking full context
4 - Good quality, human-written, legal, with clear context
5 - Excellent quality, definitely human, no spam, fully understandable context

Tweet: "{tweet_text}"

Respond with just the score number (1-5) followed by a brief explanation in this format:
SCORE: [number]
REASON: [brief explanation]"""}
            ]
            
            completion = self.openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages,
                temperature=0.7,
                max_tokens=100
            )
            
            response = completion.choices[0].message.content.strip()
            logging.debug(f"Raw quality evaluation response: {response}")
            
            # Parse the response
            lines = response.split('\n')
            score_line = next((line for line in lines if line.startswith('SCORE:')), None)
            reason_line = next((line for line in lines if line.startswith('REASON:')), None)
            
            if not score_line or not reason_line:
                logging.error(f"Invalid response format: {response}")
                return {"score": 1, "reason": "Failed to parse response format"}
                
            try:
                score = float(score_line.split(':')[1].strip())
                reason = reason_line.split(':')[1].strip()
                return {"score": score, "reason": reason}
            except (ValueError, IndexError) as e:
                logging.error(f"Error parsing score/reason: {e}")
                return {"score": 1, "reason": f"Error parsing response: {e}"}
                
        except Exception as e:
            logging.error(f"Error evaluating tweet quality: {e}")
            return {"score": 1, "reason": f"Error: {str(e)}"}
            
    def get_system_prompt(self, account):
        """Generate a system prompt for OpenAI based on account settings."""
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            
            # Get persona settings for the account
            c.execute('''
                SELECT name, description, style, length, speech_settings, 
                       use_emoji, use_hashtags, custom_prompt
                FROM personas 
                WHERE id = ?
            ''', (account.get('automation_persona_id'),))
            
            persona = c.fetchone()
            if not persona:
                logging.error(f"No persona found for account {account.get('id')}")
                return None
            
            # Extract and log persona settings
            settings = {
                'name': persona[0],
                'description': persona[1] or '',
                'style': persona[2] or 'neutral',
                'length': persona[3] or 'medium',
                'speech_settings': persona[4] or 'text',
                'use_emoji': bool(persona[5]),
                'use_hashtags': bool(persona[6]),
                'custom_prompt': persona[7] or ''
            }
            logging.info(f"Building system prompt with settings: {json.dumps(settings, indent=2)}")
            
            # Define and log style descriptions
            style_descriptions = {
                'neutral': 'maintain a balanced and objective tone',
                'casual': 'use a relaxed and informal tone of a young adult',
                'official': 'maintain a formal and professional tone',
                'ironic': 'use irony and wit',
                'sarcastic': 'employ heavy sarcasm and humor',
                'gen-z': 'use contemporary, youthful language similiar to gen-z language but do not overuse it'
            }
            logging.info(f"Style description for '{settings['style']}': {style_descriptions.get(settings['style'], style_descriptions['neutral'])}")
            
            # Define and log length descriptions
            length_descriptions = {
                'short': 'keep responses very brief (30-50 characters)',
                'medium': 'write moderate length responses (50-120 characters)',
                'long': 'provide detailed responses (120-250 characters)'
            }
            logging.info(f"Length description for '{settings['length']}': {length_descriptions.get(settings['length'], length_descriptions['medium'])}")
            
            # Define and log speech setting descriptions
            speech_settings_descriptions = {
                'text': 'write in standard text format',
                'text with a question': 'include a relevant question in your response',
                'slang': 'incorporate common slang terms appropriately',
                'formal': 'use proper grammar and sophisticated vocabulary'
            }
            logging.info(f"Speech setting description for '{settings['speech_settings']}': {speech_settings_descriptions.get(settings['speech_settings'], speech_settings_descriptions['text'])}")
            
            # Get topics from automation settings
            topics = account.get('automation_settings', {}).get('keywords', ['general topics'])
            topics_str = ', '.join(topics)
            logging.info(f"Topics: {topics_str}")
            
            # Build base prompt
            base_prompt = f"""You are a knowledgeable commenter who specializes in engaging with content about {topics_str}. 
Your responses should {style_descriptions.get(settings['style'], style_descriptions['neutral'])}, {length_descriptions.get(settings['length'], length_descriptions['medium'])}, and {speech_settings_descriptions.get(settings['speech_settings'], speech_settings_descriptions['text'])}. 
It is CRITICAL that you strictly adhere to the character count limits specified.

CRITICAL RULES:
1. NEVER include any user mentions (like @username) in your response
2. NEVER use quotation marks in your response
3. Write the comment as plain text that will be used as a reply
4. Focus on the content and context of the tweet you're replying to
5. Keep the tone consistent with the style specified
6. {"Include relevant emojis to enhance your message" if settings['use_emoji'] else "DO NOT use any emojis in your response"}
7. {"Add relevant hashtags when appropriate, but keep them concise and meaningful" if settings['use_hashtags'] else "DO NOT use any hashtags in your response"}
8. NEVER truncate or add ellipsis (...) to your response - instead, generate a complete thought within the length limit"""
            
            if settings['description']:
                base_prompt += f"\n\nPersona description: {settings['description']}"
                
            if settings['custom_prompt']:
                base_prompt += f"\n\nAdditional instructions: {settings['custom_prompt']}"
            
            logging.info(f"Generated system prompt:\n{base_prompt}")
            return base_prompt
            
        except sqlite3.Error as e:
            logging.error(f"Database error in get_system_prompt: {e}")
            return None
        finally:
            if conn:
                conn.close()

    def get_user_prompt(self, tweet):
        """Generate a user prompt for OpenAI based on the tweet."""
        return f"""Based on the following tweet: "{tweet['text']}"

Generate a relevant, engaging reply that matches the context and tone of the original tweet.
CRITICAL RULES:
1. DO NOT include any usernames or @mentions in your response
2. Keep your response STRICTLY under 100 characters (this is critical)
3. Make it a complete thought - no ellipsis or truncation
4. Focus on quality over length - shorter is better
5. Be concise but meaningful"""

    def generate_comment(self, tweet, account):
        """Generate a comment for a tweet using OpenAI."""
        try:
            # Get persona settings with detailed logging
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            c.execute('''
                SELECT p.name, p.description, p.style, p.length, p.speech_settings,
                       p.use_emoji, p.use_hashtags, p.mention_user, p.custom_prompt
                FROM personas p
                JOIN twitter_accounts ta ON ta.automation_persona_id = p.id
                WHERE ta.id = ?
            ''', (account['id'],))
            persona = c.fetchone()
            conn.close()

            if not persona:
                logging.error(f"No persona found for account {account['id']}")
                return None

            # Log all persona settings
            persona_settings = {
                'name': persona[0],
                'description': persona[1],
                'style': persona[2],
                'length': persona[3],
                'speech_settings': persona[4],
                'use_emoji': bool(persona[5]),
                'use_hashtags': bool(persona[6]),
                'mention_user': bool(persona[7]),
                'custom_prompt': persona[8]
            }
            logging.info(f"Using persona settings: {json.dumps(persona_settings, indent=2)}")

            system_prompt = self.get_system_prompt(account)
            if system_prompt is None:
                logging.error("Failed to generate system prompt")
                return None
            
            user_prompt = self.get_user_prompt(tweet)
            
            # Log complete GPT request
            gpt_request = {
                'model': "gpt-4",
                'messages': [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                'temperature': 0.7,
                'max_tokens': 200
            }
            logging.info(f"Sending GPT request: {json.dumps(gpt_request, indent=2)}")
            
            completion = self.openai_client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7,
                max_tokens=200
            )
            
            comment = completion.choices[0].message.content.strip()
            logging.info(f"Generated comment: {comment}")
            
            # Get length limits based on persona settings
            length_setting = persona_settings['length'] or 'medium'
            mention_user = persona_settings['mention_user']
            
            # Define length limits
            length_limits = {
                'short': 50,
                'medium': 120,
                'long': 250
            }
            max_length = length_limits.get(length_setting, 120)
            min_length = 50 if length_setting != 'short' else 0
            
            # Calculate available length for comment
            username_length = len(f"@{tweet['author']['userName']} ") if mention_user else 0
            effective_max_length = max_length - username_length
            
            # Log length constraints
            logging.info(f"Length constraints: min={min_length}, max={effective_max_length} (with username: {username_length})")
            
            # Enforce length limits
            comment_length = len(comment)
            if comment_length > effective_max_length or comment_length < min_length:
                logging.warning(f"Generated comment length ({comment_length} chars) outside limits ({min_length}-{effective_max_length}). Regenerating...")
                return self.generate_comment(tweet, account)
            
            # Add user mention if enabled
            if mention_user and 'author' in tweet and 'userName' in tweet['author']:
                original_comment = comment
                comment = f"@{tweet['author']['userName']} {comment}"
                logging.info(f"Added user mention. Original: '{original_comment}' -> Final: '{comment}'")
                
            return comment
            
        except Exception as e:
            logging.error(f"Error generating comment: {e}")
            logging.error(f"Full error trace:", exc_info=True)
            return None

    def post_comment(self, tweet, comment, account):
        try:
            twitter_client = TwitterClient(
                consumer_key=account['consumer_key'],
                consumer_secret=account['consumer_secret'],
                access_token=account['access_token'],
                access_token_secret=account['access_token_secret']
            )
            
            response = twitter_client.create_tweet(
                text=comment,
                reply={"in_reply_to_tweet_id": tweet['id']}
            )
            
            if not response or not response.get('data', {}).get('id'):
                raise Exception("Failed to post comment to Twitter")
                
            # Save the comment to database
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            
            # Save to posted_comments
            c.execute('''
                INSERT INTO posted_comments (
                    account_id, tweet_id, tweet_text, tweet_author, 
                    tweet_url, comment_text, comment_url, quality_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                account['id'],
                tweet['id'],
                tweet['text'],
                tweet['author']['userName'],
                tweet['url'],
                comment,
                f"https://twitter.com/i/web/status/{response['data']['id']}",
                tweet.get('quality', {}).get('score', 0)
            ))
            
            # Save to commented_tweets
            c.execute('''
                INSERT INTO commented_tweets (
                    tweet_id, account_id, quality_score
                ) VALUES (?, ?, ?)
            ''', (
                tweet['id'],
                account['id'],
                tweet.get('quality', {}).get('score', 0)
            ))
            
            conn.commit()
            return True
            
        except Exception as e:
            print(f"Error posting comment: {e}")
            return False
        finally:
            if conn:
                conn.close()
                
    def store_scheduled_post(self, account_id, tweet_id, scheduled_time, tweet_text, comment_text):
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            c.execute('''
                INSERT INTO scheduled_posts 
                (account_id, tweet_id, scheduled_time, tweet_text, comment_text) 
                VALUES (?, ?, ?, ?, ?)
            ''', (account_id, tweet_id, scheduled_time, tweet_text, comment_text))
            conn.commit()
            logging.info(f"Stored scheduled post for tweet {tweet_id} at {scheduled_time}")
        except Exception as e:
            logging.error(f"Error storing scheduled post: {e}")
        finally:
            if conn:
                conn.close()

    def process_account(self, account):
        logging.info(f"Starting to process account: {account.get('account_name')}")
        
        try:
            # Validate account settings
            if not account.get('automation_settings'):
                logging.error("No automation settings found for account")
                return
            
            settings = account['automation_settings']
            if not isinstance(settings, dict):
                try:
                    settings = json.loads(settings)
                except:
                    logging.error("Failed to parse automation settings")
                    return
            
            # Log account settings
            logging.info(f"Account settings: {json.dumps(settings, indent=2)}")
            
            # Get tweets
            keywords = settings.get('keywords', [])
            if not keywords:
                logging.error("No keywords found in settings")
                return
            
            logging.info(f"Fetching tweets for keywords: {keywords}")
            tweets = self.get_tweets(keywords, settings.get('min_likes', 900))
            
            if not tweets:
                logging.info("No tweets found")
                return
            
            # Get already commented and scheduled tweet IDs
            commented_ids = self.get_commented_tweet_ids(account['id'])
            
            # Filter out tweets that have already been commented on or scheduled
            filtered_tweets = []
            for tweet in tweets:
                tweet_id = tweet.get('id')
                if tweet_id not in commented_ids and not self.has_scheduled_post(account['id'], tweet_id):
                    filtered_tweets.append(tweet)
            
            if not filtered_tweets:
                logging.info("No new tweets to process after filtering duplicates")
                return
                
            logging.info(f"Processing {len(filtered_tweets)} new tweets")
            
            # Process tweets in parallel
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = []
                # Set initial scheduled time to be at least 1 minute from now
                current_time = datetime.now() + timedelta(minutes=1)
                
                for tweet in filtered_tweets:
                    futures.append(executor.submit(self._process_single_tweet, tweet, account, settings, current_time))
                    current_time += timedelta(seconds=random.randint(self.min_delay, self.max_delay))
                
                # Wait for all futures to complete
                concurrent.futures.wait(futures)
        except Exception as e:
            logging.error(f"Error in process_account: {e}")
            logging.exception("Full error trace:")

    def has_scheduled_post(self, account_id, tweet_id):
        """Check if there's already a scheduled post for this tweet."""
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            c.execute('''
                SELECT COUNT(*) FROM scheduled_posts 
                WHERE account_id = ? AND tweet_id = ?
            ''', (account_id, tweet_id))
            count = c.fetchone()[0]
            return count > 0
        except Error as e:
            logging.error(f"Database error checking scheduled posts: {e}")
            return True  # Return True on error to prevent duplicate scheduling
        finally:
            if conn:
                conn.close()

    def _process_single_tweet(self, tweet, account, settings, scheduled_time):
        try:
            logging.info(f"Processing tweet: {tweet.get('id')}")
            logging.info(f"Tweet text: {tweet.get('text')}")
            logging.info(f"Tweet metrics: likes={tweet.get('likeCount')}, retweets={tweet.get('retweetCount')}")
            
            # Check if already scheduled
            if self.has_scheduled_post(account['id'], tweet['id']):
                logging.debug(f"Tweet {tweet.get('id')} rejected: Already scheduled")
                return
            
            # Check engagement criteria
            likes = tweet.get('likeCount', 0)
            retweets = tweet.get('retweetCount', 0)
            min_likes = settings.get('min_likes', 900)
            min_retweets = settings.get('min_retweets', 0)

            if likes < min_likes:
                logging.debug(f"Tweet {tweet.get('id')} rejected: Insufficient likes ({likes} < {min_likes})")
                return

            if retweets < min_retweets:
                logging.debug(f"Tweet {tweet.get('id')} rejected: Insufficient retweets ({retweets} < {min_retweets})")
                return

            # Check if already commented
            if tweet.get('id') in self.get_commented_tweet_ids(account['id']):
                logging.debug(f"Tweet {tweet.get('id')} rejected: Already commented")
                return

            # Evaluate tweet quality
            quality_result = self.evaluate_tweet_quality(tweet.get('text', ''))
            quality_score = quality_result.get('score', 0)
            logging.info(f"Tweet {tweet.get('id')} quality evaluation: Score={quality_score}, Reason={quality_result.get('reason', 'No reason provided')}")

            if quality_score < self.min_quality_score:
                logging.debug(f"Tweet {tweet.get('id')} rejected: Low quality score ({quality_score} < {self.min_quality_score})")
                return

            logging.info(f"Tweet {tweet.get('id')} accepted with quality score: {quality_score}")
            
            # Generate comment
            comment = self.generate_comment(tweet, account)
            if comment:
                logging.info(f"Generated comment for tweet {tweet.get('id')}: {comment}")
                # Store the scheduled post
                self.store_scheduled_post(
                    account_id=account['id'],
                    tweet_id=tweet['id'],
                    scheduled_time=scheduled_time.strftime('%Y-%m-%d %H:%M:%S'),
                    tweet_text=tweet['text'],
                    comment_text=comment
                )
                logging.info(f"Successfully scheduled post for tweet {tweet.get('id')} at {scheduled_time}")
            else:
                logging.error(f"Failed to generate comment for tweet {tweet.get('id')}")
                
        except Exception as e:
            logging.error(f"Error processing tweet {tweet.get('id')}: {e}")
            logging.exception("Full error trace:")

    def create_notification(self, account_id, type, message, retry_time=None):
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
                
    def run(self):
        while True:
            try:
                accounts = self.get_active_automated_accounts()
                for account in accounts:
                    self.process_account(account)
                    
                # Random delay between account processing cycles
                delay = random.randint(self.min_search_interval * 60, self.max_search_interval * 60)
                time.sleep(delay)
                
            except Exception as e:
                print(f"Error in automation worker: {e}")
                time.sleep(300)  # 5 minutes delay on error
                
if __name__ == "__main__":
    worker = AutomationWorker()
    worker.run() 