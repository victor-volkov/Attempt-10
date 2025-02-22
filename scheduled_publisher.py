import sqlite3
from sqlite3 import Error
import time
from datetime import datetime
import logging
from twitter.twitter_client import TwitterClient
import os
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('automation.log'),
        logging.StreamHandler()
    ]
)

class ScheduledPublisher:
    def __init__(self):
        self.check_interval = 30  # Check for posts every 30 seconds
        
    def get_pending_posts(self):
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # Get posts that are scheduled for now or earlier and still pending
            c.execute('''
                SELECT sp.id, sp.account_id, sp.tweet_id, sp.comment_text,
                       a.consumer_key, a.consumer_secret, 
                       a.access_token, a.access_token_secret
                FROM scheduled_posts sp
                JOIN twitter_accounts a ON sp.account_id = a.id
                WHERE sp.status = 'pending'
                AND sp.scheduled_time <= ?
                ORDER BY sp.scheduled_time ASC
            ''', (now,))
            
            return c.fetchall()
        except Error as e:
            logging.error(f"Database error: {e}")
            return []
        finally:
            if conn:
                conn.close()
                
    def update_post_status(self, post_id, status, comment_url=None):
        try:
            conn = sqlite3.connect('twitter_accounts.db')
            c = conn.cursor()
            
            if comment_url:
                c.execute('''
                    UPDATE scheduled_posts 
                    SET status = ?, comment_url = ?
                    WHERE id = ?
                ''', (status, comment_url, post_id))
            else:
                c.execute('''
                    UPDATE scheduled_posts 
                    SET status = ?
                    WHERE id = ?
                ''', (status, post_id))
                
            conn.commit()
        except Error as e:
            logging.error(f"Error updating post status: {e}")
        finally:
            if conn:
                conn.close()
    
    def publish_post(self, post):
        post_id, account_id, tweet_id, comment_text, consumer_key, consumer_secret, access_token, access_token_secret = post
        
        try:
            twitter_client = TwitterClient(
                consumer_key=consumer_key,
                consumer_secret=consumer_secret,
                access_token=access_token,
                access_token_secret=access_token_secret
            )
            
            # Post the comment as a reply
            response = twitter_client.create_tweet(
                text=comment_text,
                reply={"in_reply_to_tweet_id": tweet_id}
            )
            
            if response and response.get('data', {}).get('id'):
                comment_url = f"https://twitter.com/i/web/status/{response['data']['id']}"
                self.update_post_status(post_id, 'published', comment_url)
                logging.info(f"Successfully published scheduled post {post_id}")
                return True
            else:
                self.update_post_status(post_id, 'failed')
                logging.error(f"Failed to publish post {post_id}: No response from Twitter API")
                return False
                
        except Exception as e:
            self.update_post_status(post_id, 'failed')
            logging.error(f"Error publishing post {post_id}: {e}")
            return False
    
    def run(self):
        logging.info("Starting scheduled publisher...")
        
        while True:
            try:
                pending_posts = self.get_pending_posts()
                
                for post in pending_posts:
                    self.publish_post(post)
                    time.sleep(2)  # Small delay between posts
                    
                time.sleep(self.check_interval)
                
            except Exception as e:
                logging.error(f"Error in scheduled publisher: {e}")
                time.sleep(60)  # Longer delay on error

if __name__ == '__main__':
    publisher = ScheduledPublisher()
    publisher.run() 