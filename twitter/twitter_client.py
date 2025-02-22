import requests
from requests_oauthlib import OAuth1
import logging

class TwitterClient:
    def __init__(self, consumer_key, consumer_secret, access_token, access_token_secret):
        self.auth = OAuth1(
            consumer_key,
            consumer_secret,
            access_token,
            access_token_secret
        )
        self.base_url = "https://api.twitter.com/2"
    
    def create_tweet(self, text, reply=None):
        endpoint = f"{self.base_url}/tweets"
        data = {"text": text}
        if reply and isinstance(reply, dict) and 'in_reply_to_tweet_id' in reply:
            data["reply"] = {
                "in_reply_to_tweet_id": reply["in_reply_to_tweet_id"]
            }
            
        logging.info(f"Making Twitter API request to {endpoint}")
        logging.info(f"Request data: {data}")
        
        try:
            response = requests.post(
                endpoint,
                auth=self.auth,
                json=data
            )
            logging.info(f"Twitter API Response Status: {response.status_code}")
            logging.info(f"Twitter API Response Headers: {dict(response.headers)}")
            logging.info(f"Twitter API Response Body: {response.text}")
            
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logging.error(f"Twitter API Error: {str(e)}")
            if hasattr(e.response, 'text'):
                logging.error(f"Error Response Body: {e.response.text}")
            raise 